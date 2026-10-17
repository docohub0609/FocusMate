from datetime import datetime, timedelta, timezone
from hashlib import sha256
from dataclasses import replace
import json
import sqlite3
import subprocess
import sys

from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.public_samples import PublicTestBundle, PublicTestCase
from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary


class Runner:
    def __init__(self, output=b'5\n'):
        self.calls = 0
        self.output = output

    def run(self, request):
        self.calls += 1
        return PythonRunResult(b'' if request.operation == 'compile' else self.output,
            b'', 0, 'exited', False, False, False, ResourceSummary(1, 100, None, 1, 1, 1, 1))


def materials():
    return ({'loopId': 'loop1', 'goalContext': {'goalId': 'g', 'scope': 'goal'},
             'problemRef': {'problemId': 'p', 'problemVersion': 1},
             'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()},
            {'sourceCode': 'print(5)', 'codeVersion': 2,
             'contentHash': sha256(b'print(5)').hexdigest()},
            PublicTestBundle('a', 1, 'p', 1, (PublicTestCase('t', b'', b'5\n'),)))


def test_comparison_conditions_persist_across_runs_without_loop_identity(tmp_path):
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Runner())
    ctx, code, bundle = materials()
    first = service.run(ctx, code, bundle)
    second = service.run({**ctx, 'loopId': 'loop2'}, code, bundle)
    changed = service.run({**ctx, 'loopId': 'loop3'}, code,
                          replace(bundle, cases=(PublicTestCase('t', b'', b'6\n'),)))
    with sqlite3.connect(service.path) as db:
        rows = dict(db.execute('SELECT run_id, conditions_hash FROM tool_public_comparison'))
    assert rows[first['runId']] == rows[second['runId']]
    assert rows[first['runId']] != rows[changed['runId']]
    assert len(rows) == 3


def test_comparison_registration_failure_rolls_back_task(tmp_path):
    runner = Runner()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='tool_public_comparison'").fetchone()
        db.execute("CREATE TRIGGER reject_comparison BEFORE INSERT ON tool_public_comparison BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    import pytest
    with pytest.raises(sqlite3.IntegrityError, match='disk failure'):
        service.run(*materials())
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT COUNT(*) FROM tool_public_tasks').fetchone()[0] == 0
    assert runner.calls == 0


def test_actual_output_and_dedup(tmp_path):
    runner = Runner()
    service = PublicRunService(tmp_path / 'business.db', tmp_path / 'runs', runner)
    args = materials()
    first = service.run(*args)
    assert first['status'] == 'succeeded'
    assert first['caseResults'][0]['actualOutput'] == '5\n'
    assert service.run(*args)['runId'] == first['runId']
    assert runner.calls == 2
    assert not (tmp_path / 'business.db').exists()


def test_wrong_output_is_completed_not_infrastructure_failure(tmp_path):
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Runner(b'0'))
    result = service.run(*materials())
    assert result['status'] == 'succeeded'
    assert result['sampleStatus'] == 'failed'
    assert result['caseResults'][0]['outcome'] == 'outputMismatch'


def test_expired_and_invalid_hash_do_not_execute(tmp_path):
    runner = Runner()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    ctx, code, bundle = materials()
    code['contentHash'] = 'wrong'
    assert service.run(ctx, code, bundle)['status'] == 'failed'
    assert runner.calls == 0


def test_truncated_execution_never_passes(tmp_path):
    class Truncated(Runner):
        def run(self, request):
            result = super().run(request)
            return replace(result, stdout_truncated=request.operation == 'execute')
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Truncated())
    result = service.run(*materials())
    assert result['sampleStatus'] == 'failed'
    assert result['caseResults'][0]['terminationReason'] == 'outputLimitExceeded'


def test_display_limit_does_not_change_comparison(tmp_path):
    ctx, code, bundle = materials()
    output = b'x' * 5000
    bundle = replace(bundle, cases=tuple(PublicTestCase(str(i), output, output) for i in range(8)))
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Runner(output))
    result = service.run(ctx, code, bundle)
    assert result['sampleStatus'] == 'passed'
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 16384
    assert all(c['actualOutputTruncated'] for c in result['caseResults'])


def test_cancel_prevents_late_write_and_dedup_restart(tmp_path):
    class Cancelling(Runner):
        def run(self, request):
            with sqlite3.connect(service.path) as db:
                run_id = db.execute('SELECT run_id FROM tool_public_tasks').fetchone()[0]
            service.cancel(run_id)
            return super().run(request)
    runner = Cancelling()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    args = materials()
    result = service.run(*args)
    assert result['status'] == 'cancelled'
    assert service.run(*args)['status'] == 'cancelled'
    assert runner.calls == 1


def test_recover_expired_claim_does_not_rerun(tmp_path):
    runner = Runner()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    args = materials()
    first = service.run(*args)
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE tool_public_tasks SET status='running', owner='old', deadline=0, result_json=NULL")
    assert service.recover()[0]['status'] == 'interrupted'
    assert service.run(*args)['status'] == 'interrupted'
    assert runner.calls == 2


def test_new_loop_runs_again_and_too_many_samples_rejected(tmp_path):
    runner = Runner()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    ctx, code, bundle = materials()
    first = service.run(ctx, code, bundle)
    ctx['loopId'] = 'new'
    assert service.run(ctx, code, bundle)['runId'] != first['runId']
    bundle = replace(bundle, cases=bundle.cases * 9)
    assert service.run(ctx, code, bundle)['status'] == 'failed'
    assert runner.calls == 4


def test_missing_business_database_not_created(tmp_path):
    service = PublicRunService(tmp_path / 'absent.db', tmp_path / 'runs', Runner())
    ctx, code, _ = materials()
    assert service.run(ctx, code)['status'] == 'failed'
    assert not (tmp_path / 'absent.db').exists()


def test_cleanup_failure_still_persists_terminal_result(tmp_path, monkeypatch):
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Runner())
    original_cleanup = service._cleanup
    def fail(row):
        raise OSError('locked workspace')
    monkeypatch.setattr(service, '_cleanup', fail)
    args = materials()
    result = service.run(*args)
    assert result['status'] == 'failed'
    assert result['cleanupPending'] is True
    assert service.run(*args)['runId'] == result['runId']
    monkeypatch.setattr(service, '_cleanup', original_cleanup)
    recovered = service.recover()
    assert recovered[0]['cleanupPending'] is False
    assert recovered[0]['status'] == 'failed'


def test_runner_finishing_after_deadline_cannot_pass(tmp_path):
    import time
    class Slow(Runner):
        def run(self, request):
            time.sleep(.08)
            return super().run(request)
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Slow())
    ctx, code, bundle = materials()
    ctx['deadlineAt'] = (datetime.now(timezone.utc) + timedelta(seconds=.05)).isoformat()
    result = service.run(ctx, code, bundle)
    assert result['status'] == 'interrupted'
    assert result.get('sampleStatus') != 'passed'


def test_compile_timeout_not_reported_as_user_syntax_error(tmp_path):
    class Timeout(Runner):
        def run(self, request):
            return replace(super().run(request), termination_reason='wallTimeExceeded', exit_code=137)
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Timeout())
    result = service.run(*materials())
    assert result['status'] == 'failed'
    assert result['caseResults'][0]['outcome'] == 'notExecuted'


def test_same_loop_changed_bundle_cannot_start_second_run(tmp_path):
    runner = Runner()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    ctx, code, bundle = materials()
    service.run(ctx, code, bundle)
    result = service.run(ctx, code, replace(bundle, asset_version=2))
    assert result['failure'] == 'PUBLIC_RUN_LIMIT_REACHED'
    assert result['goalContext'] == ctx['goalContext']
    assert runner.calls == 2


def test_real_process_exit_leaves_task_recoverable_without_replay(tmp_path):
    """Real OS process exit; fake runner, no claim of Docker crash coverage."""
    script = r'''
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os, sys
from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.public_samples import PublicTestBundle, PublicTestCase
class Crash:
    def run(self, request):
        os._exit(17)
context = {'loopId':'crashed-loop','goalContext':{'goalId':'g','scope':'goal'},
 'problemRef':{'problemId':'p','problemVersion':1},
 'deadlineAt':(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()}
code = {'sourceCode':'print(5)','codeVersion':2,'contentHash':sha256(b'print(5)').hexdigest()}
bundle = PublicTestBundle('a',1,'p',1,(PublicTestCase('t',b'',b'5\n'),))
PublicRunService(sys.argv[1],sys.argv[2],Crash()).run(context,code,bundle)
'''
    completed = subprocess.run([sys.executable, '-c', script,
                                str(tmp_path / 'b.db'), str(tmp_path / 'runs')],
                               capture_output=True, timeout=10)
    assert completed.returncode == 17, completed.stderr.decode(errors='replace')
    runner = Runner()
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', runner)
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT status FROM tool_public_tasks').fetchone()[0] == 'running'
        # First prove the real child exited during execution, then simulate the
        # lease expiring. A 150ms startup deadline tests disk speed instead.
        db.execute('UPDATE tool_public_tasks SET deadline=0')
    recovered = service.recover()
    assert recovered[0]['status'] == 'interrupted'
    assert runner.calls == 0
    assert not [p for p in service.root.iterdir() if p.is_dir()]


def test_failed_container_remove_keeps_name_and_never_starts_next_step(tmp_path, monkeypatch):
    import services.oj_worker.tool_public_runs as module
    executions = []
    removals = []
    allow_removal = False

    class FakeDocker:
        def control(self, command):
            removals.append(command[-1])
            return subprocess.CompletedProcess(command, 0 if allow_removal else 1, '', 'daemon unavailable')

    class FakeDockerRunner:
        def __init__(self, container_name_factory=None):
            self.name_factory = container_name_factory
            self.docker = FakeDocker()

        def run(self, request):
            executions.append((request.operation, self.name_factory()))
            return Runner().run(request)

    monkeypatch.setattr(module, 'DockerPythonRunner', FakeDockerRunner)
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs')
    result = service.run(*materials())
    assert result['status'] == 'failed'
    assert result['cleanupPending'] is True
    assert len(executions) == 1  # compile only; no sample can hide its container name
    with sqlite3.connect(service.path) as db:
        saved_name = db.execute('SELECT container_name FROM tool_public_tasks').fetchone()[0]
    assert saved_name == executions[0][1]
    assert all(name == saved_name for name in removals)
    allow_removal = True
    assert service.recover()[0]['cleanupPending'] is False
    assert len(executions) == 1  # recovery only retries removal
    assert not [p for p in service.root.iterdir() if p.is_dir()]


def test_cleanup_crossing_deadline_cannot_commit_success(tmp_path, monkeypatch):
    import time
    service = PublicRunService(tmp_path / 'b.db', tmp_path / 'runs', Runner())
    original = service._cleanup
    def slow_cleanup(row):
        time.sleep(.2)
        original(row)
    monkeypatch.setattr(service, '_cleanup', slow_cleanup)
    ctx, code, bundle = materials()
    ctx['deadlineAt'] = (datetime.now(timezone.utc) + timedelta(seconds=.1)).isoformat()
    result = service.run(ctx, code, bundle)
    assert result['status'] == 'interrupted'
    assert result['failure'] == 'PUBLIC_RUN_DEADLINE_EXCEEDED'
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT status FROM tool_public_tasks').fetchone()[0] == 'interrupted'

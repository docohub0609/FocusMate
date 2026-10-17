from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import pytest
from tests.python.test_editor_workspace import editor
from tests.python.test_oj_judge import FakeRunner, run_result


def setup_run(tmp_path, mode='customInput'):
    from services.oj_worker.editor_runs import RunRepository
    repo = editor(tmp_path)
    goal = repo.workspace('problem-submit', 1, create=True)['goal']
    saved = repo.save_draft(goal['goalId'], dict(sourceCode='print(input())', expectedRevision=0, operationId='save'))
    runs = RunRepository(repo.path)
    request = dict(goalContext=saved['goalContext'], problemRef=goal['problemRef'], codeVersion=1,
                   operationId='click', runMode=mode)
    if mode == 'customInput': request['stdin'] = '你好\n'
    return runs, request


def test_run_persists_sandbox_failure_and_does_not_run_remaining_cases(tmp_path):
    from services.oj_worker.editor_runs import RunWorker
    runs, request = setup_run(tmp_path, 'publicExample')
    task = runs.enqueue(request)
    failure = {'stage':'connect','errorType':'Timeout','elapsedMs':3000,'retryCount':1}
    runner = FakeRunner([replace(run_result(reason='infrastructureError'), failure=failure)])
    RunWorker(runs,tmp_path/'work',runner=runner).process_once()
    result = runs.get(task['runId'])
    assert result['result']['failure'] == failure
    assert len(runner.requests) == 1 and result['status'] == 'failed'


def test_enqueue_snapshots_inputs_and_idempotency(tmp_path):
    from services.oj_worker.editor_runs import RunFailure
    runs, request = setup_run(tmp_path)
    task = runs.enqueue(request)
    assert task['status'] == 'queued' and task['codeVersion'] == 1
    assert runs.enqueue(request) == task
    with pytest.raises(RunFailure): runs.enqueue(dict(request, stdin='changed'))
    with sqlite3.connect(runs.path) as db:
        assert db.execute('select count(*) from raw_events where event_name=\'runRequested\'').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['customInput','publicExample'])
def test_worker_keeps_run_separate_from_oj_and_goal(tmp_path, mode):
    from services.oj_worker.editor_runs import RunWorker
    runs, request = setup_run(tmp_path, mode)
    task = runs.enqueue(request)
    runner = FakeRunner([run_result(), run_result(b'1 2\n')] * 4)
    worker = RunWorker(runs, tmp_path / 'work', runner=runner)
    assert worker.process_once() == task['runId']
    result = runs.get(task['runId'])
    assert result['status'] == 'finished'
    assert result['result']['cases'][0]['outcome'] == ('executed' if mode == 'customInput' else 'passed')
    assert 'verdict' not in json.dumps(result)
    assert result['result']['cases'][0]['memoryLimitBytes'] == 256 * 1024 * 1024
    assert result['result']['cases'][0]['peakMemoryBytes'] == 1024
    assert len(runner.requests) >= 2
    with sqlite3.connect(runs.path) as db:
        assert db.execute('select count(*) from submissions').fetchone()[0] == 0
        assert db.execute('select status from goal_records').fetchone()[0] == 'active'
        assert db.execute('select count(*) from raw_events where event_name=\'runFinished\'').fetchone()[0] == 1
    assert worker.process_once() is None


def test_exclusive_claim_expiry_blocks_late_result(tmp_path):
    from services.oj_worker.editor_runs import RunFailure
    runs, request = setup_run(tmp_path)
    task = runs.enqueue(request)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: runs.claim(), range(2)))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    with sqlite3.connect(runs.path) as db:
        db.execute('update editor_runs set lease_until=0')
    with pytest.raises(RunFailure): runs.finish(claim, 'finished', {'cases': []})
    runs.expire()
    assert runs.get(task['runId'])['status'] == 'interrupted'
    assert runs.claim() is None


@pytest.mark.parametrize('result,status,outcome', [
    (run_result(stderr=b'SyntaxError: missing colon', exit_code=1), 'finished', 'compileError'),
    (run_result(stderr=b'private docker path', reason='infrastructureError'), 'failed', 'infrastructureError'),
    (run_result(stderr=b'private docker path', reason='startupTimeExceeded'), 'failed', 'infrastructureError'),
])
def test_compile_and_infrastructure_are_not_learning_failures(tmp_path, result, status, outcome):
    from services.oj_worker.editor_runs import RunWorker
    runs, request = setup_run(tmp_path)
    task = runs.enqueue(request)
    RunWorker(runs, tmp_path/'work', runner=FakeRunner([result])).process_once()
    result = runs.get(task['runId'])
    assert result['status'] == status
    assert result['result']['cases'][0]['outcome'] == outcome
    assert 'private docker path' not in json.dumps(result)


def test_t061_organizes_editor_run_without_inventing_learning_progress(tmp_path):
    from services.oj_worker.editor_runs import RunWorker
    from src.focusmate.adapters.observations import SQLiteObservationStore
    from src.focusmate.contracts.observations import OrganizationPolicy
    from datetime import datetime, timezone
    runs, request = setup_run(tmp_path)
    runs.enqueue(request)
    RunWorker(runs,tmp_path/'work',runner=FakeRunner([run_result(),run_result(b'ok')])).process_once()
    store = SQLiteObservationStore(runs.path)
    store.process(request['goalContext']['goalId'],now=datetime.now(timezone.utc).timestamp(),policy=OrganizationPolicy())
    assert store.list_failures(request['goalContext']['goalId']) == []
    result = next(o for o in store.list_observations(request['goalContext']['goalId']) if o['kind']=='runFinished')
    assert result['evidence']['codeVersion'] == 1
    assert result['evidence']['status'] == 'finished'


def test_forged_run_reference_rejected_and_unknown_params(tmp_path):
    from services.oj_worker.editor_runs import RunFailure
    runs, request = setup_run(tmp_path)
    for change in [dict(codeVersion=999),dict(codeVersion=True),dict(goalContext={'goalId':'missing','scope':'goal'}),dict(resourcePolicyRef='unlimited')]:
        with pytest.raises(RunFailure): runs.enqueue(dict(request,**change))


def test_total_budget_does_not_execute_remaining_case(tmp_path,monkeypatch):
    from services.oj_worker.editor_runs import RunWorker
    import services.oj_worker.editor_runs as module
    runs, request = setup_run(tmp_path)
    task=runs.enqueue(request)
    elapsed=[0.0]
    class SlowRunner:
        def run(self, request):
            elapsed[0]=31.0
            return run_result()
    monkeypatch.setattr(module.time,'monotonic',lambda:elapsed[0])
    RunWorker(runs,tmp_path/'work',runner=SlowRunner()).process_once()
    assert runs.get(task['runId'])['result']['cases'][0]['outcome']=='notExecuted'


def test_run_result_and_event_rollback_together(tmp_path,monkeypatch):
    from src.focusmate.adapters.raw_events import SQLiteEventStore
    runs,request=setup_run(tmp_path);task=runs.enqueue(request);claim=runs.claim()
    def fail(*args,**kwargs): raise RuntimeError('event failed')
    monkeypatch.setattr(SQLiteEventStore,'append_on_connection',fail)
    with pytest.raises(RuntimeError):runs.finish(claim,'finished',{'cases':[]})
    assert runs.get(task['runId'])['status']=='running'


@pytest.mark.parametrize('reason', ['outputLimitExceeded','memoryLimitExceeded'])
def test_run_preserves_resource_reason_when_container_was_killed(reason):
    from services.oj_worker.editor_runs import RunWorker
    assert RunWorker.outcome(run_result(reason=reason,exit_code=137)) == reason


def test_unexpected_runner_error_finishes_task_and_next_task_can_run(tmp_path):
    from services.oj_worker.editor_runs import RunWorker
    runs, request = setup_run(tmp_path)
    first = runs.enqueue(request)
    second = runs.enqueue(dict(request, operationId='next'))
    class UnexpectedRunner:
        def run(self, request):
            raise RuntimeError('private transport detail')
    worker = RunWorker(runs, tmp_path/'work', runner=UnexpectedRunner())
    assert worker.process_once() == first['runId']
    failed = runs.get(first['runId'])
    assert failed['status'] == 'failed'
    assert failed['result']['failure']['errorType'] == 'RuntimeError'
    assert failed['result']['failure']['stage'] == 'compile'
    assert 'private transport detail' not in json.dumps(failed)
    worker.runner = FakeRunner([run_result(), run_result(b'ok\n')])
    assert worker.process_once() == second['runId']
    assert runs.get(second['runId'])['status'] == 'finished'
    assert runs.get(first['runId']) == failed


def test_cleanup_exception_keeps_obligation_without_killing_worker(tmp_path, monkeypatch):
    from services.oj_worker.editor_runs import RunWorker
    runs, request = setup_run(tmp_path)
    task = runs.enqueue(request)
    worker = RunWorker(runs, tmp_path/'work', runner=FakeRunner([run_result(), run_result()]))
    original = worker.cleanup
    def broken_cleanup(row):
        raise RuntimeError('cleanup failed')
    monkeypatch.setattr(worker, 'cleanup', broken_cleanup)
    assert worker.process_once() == task['runId']
    assert runs.get(task['runId'])['status'] == 'finished'
    assert worker.process_once() is None  # A stale cleanup fault also cannot kill polling.
    with sqlite3.connect(runs.path) as db:
        assert db.execute('SELECT cleanup_pending FROM editor_runs').fetchone()[0] == 1
    monkeypatch.setattr(worker, 'cleanup', original)
    assert worker.process_once() is None
    with sqlite3.connect(runs.path) as db:
        assert db.execute('SELECT cleanup_pending FROM editor_runs').fetchone()[0] == 0


def test_run_polling_survives_claim_failure_without_replaying_task(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from services.oj_worker import __main__ as cli, editor_runs
    calls = []
    class FaultyWorker:
        def __init__(self, *args): pass
        def process_once(self):
            calls.append('poll')
            if len(calls) == 1: raise sqlite3.OperationalError('private database path')
            raise KeyboardInterrupt()
    monkeypatch.setenv('FOCUSMATE_DB_PATH', str(tmp_path/'queue.db'))
    monkeypatch.setenv('FOCUSMATE_OJ_WORKSPACE_ROOT', str(tmp_path/'work'))
    monkeypatch.setattr(editor_runs, 'RunWorker', FaultyWorker)
    monkeypatch.setattr(cli, 'parse_args', lambda: SimpleNamespace(
        health=False, task_kind='runs', once=False, poll_seconds=.01, lease_seconds=60))
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    assert cli.main() == 0
    assert len(calls) == 2
    error = capsys.readouterr().err
    assert 'OperationalError' in error and 'private database path' not in error


def test_submission_poll_failure_is_recorded_without_replaying_task(tmp_path,monkeypatch,capsys):
    from types import SimpleNamespace
    from services.oj_worker import __main__ as cli
    calls=[]
    class FaultyWorker:
        def __init__(self,*args,**kwargs):pass
        def process_once(self):
            calls.append('poll')
            if len(calls)==1:raise sqlite3.OperationalError('private submission input')
            raise KeyboardInterrupt()
    monkeypatch.setenv('FOCUSMATE_DB_PATH',str(tmp_path/'oj.db'))
    monkeypatch.setenv('FOCUSMATE_OJ_WORKSPACE_ROOT',str(tmp_path/'work'))
    monkeypatch.setattr(cli,'Worker',FaultyWorker)
    monkeypatch.setattr(cli,'parse_args',lambda:SimpleNamespace(health=False,task_kind='submissions',once=False,poll_seconds=.01,lease_seconds=60))
    monkeypatch.setattr(cli.time,'sleep',lambda _:None)
    assert cli.main()==0 and len(calls)==2
    error=capsys.readouterr().err
    assert 'focusmate-oj-worker' in error and 'OperationalError' in error
    assert 'private submission input' not in error

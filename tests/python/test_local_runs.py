from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from threading import Event, Thread
import time

import pytest

from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary
from services.oj_worker.local_runs import LocalRunService, build_local_program


def args(**overrides):
    value = {
        'codeVersion': 2,
        'purpose': '确认这一行是否会更新 x',
        'observationIntent': '观察运行后的 x',
        'snippet': {'kind': 'lines', 'startLine': 1, 'endLine': 1},
        'initialVariables': {'x': 2},
        'observe': {'identifiers': ['x'], 'returnValue': False},
    }
    value.update(overrides)
    return value


def context(loop='loop-1'):
    return {'loopId': loop, 'goalContext': {'goalId': 'g', 'scope': 'subgoal', 'subgoalId': 'sg'},
            'problemRef': {'problemId': 'p', 'problemVersion': 1},
            'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}


def code(source):
    return {'sourceCode': source, 'codeVersion': 2,
            'contentHash': sha256(source.encode()).hexdigest(), 'codeSnapshotId': 'snapshot-2'}


class Runner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or PythonRunResult(
            b'__FOCUSMATE_OBSERVATION_V1__{"observations":{"x":3}}\n', b'', 0, 'exited', False, False, False,
            ResourceSummary(1, 256 * 1024 * 1024, None, 1, 2, 32, 16 * 1024 * 1024))

    def run(self, request):
        self.calls.append(request)
        return self.result


def test_template_extracts_original_complete_statement_and_fixed_json_setup():
    source = 'x += 1\nprint("outside")\n'
    built = build_local_program(code(source), args())
    assert built['sourceRef']['startLine'] == 1 and built['sourceRef']['endLine'] == 1
    assert built['sourceRef']['source'] == 'x += 1'
    assert 'json.loads(' in built['program']
    assert 'exec(' not in built['program']
    assert built['setup'] == {'x': 2}


def test_function_selector_extracts_full_top_level_function_and_calls_json_args():
    source = 'def add(a, b=0):\n    return a + b\n\nprint("outside")\n'
    request = args(snippet={'kind': 'function', 'name': 'add'}, initialVariables={},
                   call={'function': 'add', 'args': [2, 3]},
                   observe={'identifiers': [], 'returnValue': True})
    built = build_local_program(code(source), request)
    assert built['sourceRef']['source'] == 'def add(a, b=0):\n    return a + b'
    assert built['call'] == {'function': 'add', 'args': [2, 3]}


def test_function_can_be_extracted_when_unrelated_later_code_is_incomplete():
    source = 'def add(a):\n    return a + 1\n\nif True:\n'
    request = args(snippet={'kind': 'function', 'name': 'add'}, initialVariables={},
                   call={'function': 'add', 'args': [2]},
                   observe={'identifiers': [], 'returnValue': True})
    assert build_local_program(code(source), request)['sourceRef']['endLine'] == 2


def test_indented_complete_fragment_is_dedented_without_changing_recorded_original():
    source='for item in values:\n    total += item\n'
    request=args(snippet={'kind':'lines','startLine':2,'endLine':2},
                 initialVariables={'total':0,'item':3},
                 observe={'identifiers':['total'],'returnValue':False})
    built=build_local_program(code(source),request)
    assert built['sourceRef']['source']=='    total += item'
    assert built['sourceRef']['executedSource']=='total += item'
    assert built['sourceRef']['normalization']=='textwrap.dedent'


def test_dedent_rejects_multiline_string_whose_value_would_change():
    source='if True:\n    message = """first\n    second"""\n'
    with pytest.raises(ValueError,match='multiline string'):
        build_local_program(code(source),args(snippet={'kind':'lines','startLine':2,'endLine':3},
            initialVariables={},observe={'identifiers':['message'],'returnValue':False}))


def test_complete_function_with_column_zero_text_inside_multiline_string_is_not_truncated():
    source='def f():\n    return """first\nsecond"""\n'
    request=args(snippet={'kind':'function','name':'f'},initialVariables={},
                 call={'function':'f','args':[]},observe={'identifiers':[],'returnValue':True})
    assert build_local_program(code(source),request)['sourceRef']['endLine']==3


def test_line_range_cannot_turn_string_contents_into_executable_code():
    source='text = """\nx = 99\n"""\n'
    with pytest.raises(ValueError,match='original statement'):
        build_local_program(code(source),args(snippet={'kind':'lines','startLine':2,'endLine':2},
            initialVariables={},observe={'identifiers':['x'],'returnValue':False}))


def test_partial_file_fallback_does_not_end_function_at_top_level_comment():
    source='def f():\n    x = 1\n# explanation\n    return x\nif True:\n'
    request=args(snippet={'kind':'function','name':'f'},initialVariables={},
                 call={'function':'f','args':[]},observe={'identifiers':[],'returnValue':True})
    built=build_local_program(code(source),request)
    assert 'return x' in built['sourceRef']['source'] and built['sourceRef']['endLine']==4


def test_line_range_accepts_multiple_consecutive_complete_statements():
    source='x = 1\nx += 1\n'
    built=build_local_program(code(source),args(snippet={'kind':'lines','startLine':1,'endLine':2},
        initialVariables={},observe={'identifiers':['x'],'returnValue':False}))
    assert built['sourceRef']['executedSource']==source.rstrip()


def test_partial_file_keeps_string_ranges_seen_before_later_token_error():
    source='text = """\nx = 99\n"""\nif (\n'
    with pytest.raises(ValueError,match='string literal'):
        build_local_program(code(source),args(snippet={'kind':'lines','startLine':2,'endLine':2},
            initialVariables={},observe={'identifiers':['x'],'returnValue':False}))


def test_unclosed_multiline_string_contents_cannot_be_selected_as_code():
    source='text = """\nx = 99\n'
    with pytest.raises(ValueError,match='string literal'):
        build_local_program(code(source),args(snippet={'kind':'lines','startLine':2,'endLine':2},
            initialVariables={},observe={'identifiers':['x'],'returnValue':False}))


def test_partial_file_fallback_rejects_multiline_decorator():
    source='@deco(\n    1\n)\ndef f():\n    return 1\nif True:\n'
    request=args(snippet={'kind':'function','name':'f'},initialVariables={},
                 call={'function':'f','args':[]},observe={'identifiers':[],'returnValue':True})
    with pytest.raises(ValueError,match='unsupported function'):
        build_local_program(code(source),request)


def test_lines_cannot_strip_a_function_decorator():
    source='@deco\ndef f():\n    return 1\n'
    with pytest.raises(ValueError,match='complete original statements'):
        build_local_program(code(source),args(snippet={'kind':'lines','startLine':2,'endLine':3},
            initialVariables={},observe={'identifiers':[],'returnValue':False}))


def test_lines_fallback_cannot_strip_decorator_when_later_source_is_incomplete():
    source='@deco\ndef f():\n    return 1\nif True:\n'
    with pytest.raises(ValueError):
        build_local_program(code(source),args(snippet={'kind':'lines','startLine':2,'endLine':3},
            initialVariables={},observe={'identifiers':[],'returnValue':False}))


@pytest.mark.parametrize('request_args', [
    args(snippet={'kind': 'lines', 'startLine': 1, 'endLine': 1}),
    args(initialVariables={'x': object()}),
    args(observe={'identifiers': ['x + 1'], 'returnValue': False}),
    args(call={'function': '__import__', 'args': []}),
])
def test_template_rejects_partial_syntax_non_json_and_expressions(request_args):
    with pytest.raises(ValueError):
        build_local_program(code('if True:\n    x = 1\n'), request_args)


def test_service_persists_exact_inputs_outputs_and_context_without_correctness_verdict(tmp_path):
    runner = Runner()
    service = LocalRunService(tmp_path / 'runs', runner)
    result = service.run(context(), code('x += 1\n'), args())
    assert result['status'] == 'succeeded'
    assert result['goalContext'] == context()['goalContext']
    assert result['codeVersion'] == 2 and result['codeSnapshotId'] == 'snapshot-2'
    assert result['sourceRef']['source'] == 'x += 1'
    assert result['setup'] == {'x': 2}
    assert result['observations'] == {'x': 3}
    assert result['purpose'] == '确认这一行是否会更新 x'
    assert result['observationIntent'] == '观察运行后的 x'
    assert result['rawOutput'] == ''
    assert result['resourcePolicy']['policy_ref']=='python-local-v1'
    assert result['resourcePolicy']['wall_time_seconds']<=10
    assert not ({'verdict', 'expectedOutput', 'correct'} & result.keys())
    with sqlite3.connect(service.path) as db:
        saved = json.loads(db.execute('SELECT result_json FROM tool_local_tasks').fetchone()[0])
    assert saved == result


def test_dedup_includes_setup_call_and_observe_and_loop_allows_only_identical_retry(tmp_path):
    runner = Runner(); service = LocalRunService(tmp_path / 'runs', runner)
    first = service.run(context(), code('x += 1\n'), args())
    assert service.run(context(), code('x += 1\n'), args())['runId'] == first['runId']
    changed = service.run(context(), code('x += 1\n'), args(initialVariables={'x': 8}))
    assert changed['failure'] == 'LOCAL_RUN_LIMIT_REACHED'
    assert len(runner.calls) == 1


@pytest.mark.parametrize(('raw','failure'), [
    (PythonRunResult(b'', b'NameError: missing\n', 1, 'exited', False, False, False,
                     ResourceSummary(1, 1, None, 1, 1, 1, 1)), 'LOCAL_RUN_RUNTIME_ERROR'),
    (PythonRunResult(b'', b'', 137, 'wallTimeExceeded', False, False, False,
                     ResourceSummary(3000, 1, None, 1, 1, 1, 1)), 'LOCAL_RUN_RESOURCE_LIMIT'),
])
def test_runtime_missing_dependency_and_loop_are_clear_failures(tmp_path, raw, failure):
    result = LocalRunService(tmp_path / failure, Runner(raw)).run(context(), code('x += missing\n'), args())
    assert result['status'] == 'failed' and result['failure'] == failure
    assert 'verdict' not in result


def test_output_limit_is_failure_and_not_parsed_as_observation(tmp_path):
    raw = replace(Runner().result, stdout=b'x' * 1024, stdout_truncated=True,
                  termination_reason='outputLimitExceeded')
    result = LocalRunService(tmp_path / 'runs', Runner(raw)).run(context(), code('x += 1\n'), args())
    assert result['failure'] == 'LOCAL_RUN_RESOURCE_LIMIT'
    assert 'observations' not in result


def test_runner_exception_records_only_safe_category(tmp_path):
    class Broken(Runner):
        def run(self,request):raise RuntimeError('secret host path C:/private')
    result=LocalRunService(tmp_path/'runs',Broken()).run(context(),code('x += 1\n'),args())
    assert result['infrastructureFailureCategory']=='RuntimeError'
    assert 'secret' not in json.dumps(result)


def test_cancelled_owner_cannot_be_overwritten_by_late_runner(tmp_path):
    entered=Event();release=Event()
    class Slow(Runner):
        def run(self,request):
            entered.set();release.wait(3);return super().run(request)
    service=LocalRunService(tmp_path/'runs',Slow());box={}
    thread=Thread(target=lambda:box.setdefault('result',service.run(context(),code('x += 1\n'),args())))
    thread.start();assert entered.wait(2)
    with sqlite3.connect(service.path) as db:run_id=db.execute('SELECT run_id FROM tool_local_tasks').fetchone()[0]
    assert service.cancel(run_id)['status']=='cancelled'
    release.set();thread.join(5)
    assert box['result']['status']=='cancelled'


def test_deadline_after_runner_cannot_persist_success(tmp_path, monkeypatch):
    # Advance time only after execution starts; filesystem setup must not consume
    # the tiny wall-clock budget before the branch under test is reached.
    now = datetime.now(timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr('services.oj_worker.local_runs.datetime', Clock)
    class Slow(Runner):
        def run(self,request):
            nonlocal now
            now += timedelta(seconds=2)
            return super().run(request)
    ctx=context();ctx['deadlineAt']=(now+timedelta(seconds=1)).isoformat()
    result=LocalRunService(tmp_path/'runs',Slow()).run(ctx,code('x += 1\n'),args())
    assert result['status']=='interrupted'


def test_recover_marks_expired_owner_interrupted_and_keeps_request_audit(tmp_path):
    service=LocalRunService(tmp_path/'runs',Runner())
    workspace=service.root/'tool-local-old';workspace.mkdir();(workspace/'main.py').write_text('x=1')
    stored={'loopId':'old','goalContext':context()['goalContext'],'problemRef':context()['problemRef'],
            'codeVersion':2,'request':{'purpose':'audit'}}
    with sqlite3.connect(service.path) as db:
        db.execute('INSERT INTO tool_local_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('tool-local-old','key','running','old-owner',0,'old',json.dumps(stored),None,str(workspace),None))
    result=service.recover()[0]
    assert result['status']=='interrupted' and result['request']=={'purpose':'audit'} and not workspace.exists()


def test_cleanup_failure_is_persisted_and_recover_retries(tmp_path,monkeypatch):
    service=LocalRunService(tmp_path/'runs',Runner());original=service._cleanup;attempts={'count':0}
    def flaky(row):
        attempts['count']+=1
        if attempts['count']==1:raise OSError('locked')
        return original(row)
    monkeypatch.setattr(service,'_cleanup',flaky)
    result=service.run(context(),code('x += 1\n'),args())
    assert result['cleanupPending'] is True
    recovered=service.recover()
    assert recovered[-1]['cleanupPending'] is False
    assert service._view(service._row(result['runId'])).get('failure')!='LOCAL_RUN_CLEANUP_PENDING'


def test_late_cleanup_failure_is_saved_without_overwriting_cancelled_status(tmp_path,monkeypatch):
    entered=Event();release=Event()
    class Slow(Runner):
        def run(self,request):entered.set();release.wait(3);return super().run(request)
    service=LocalRunService(tmp_path/'runs',Slow());original=service._cleanup;calls={'n':0};box={}
    def fail_second(row):
        calls['n']+=1
        if calls['n']==2:raise OSError('late resource locked')
        return original(row)
    monkeypatch.setattr(service,'_cleanup',fail_second)
    thread=Thread(target=lambda:box.setdefault('result',service.run(context(),code('x += 1\n'),args())))
    thread.start();assert entered.wait(2)
    with sqlite3.connect(service.path) as db:run_id=db.execute('SELECT run_id FROM tool_local_tasks').fetchone()[0]
    service.cancel(run_id);release.set();thread.join(5)
    assert box['result']['status']=='cancelled' and box['result']['cleanupPending'] is True
    service.recover()
    assert service._view(service._row(run_id))['cleanupPending'] is False

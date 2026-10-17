"""An AC stops automatic follow-up, not a user's request to correct the coach."""
import pytest

from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import materials
from tests.python.test_coach_v3_flow import establish


def fail_current(m):
    owned = m.claim('s', 'worker', materials=materials(m))
    assert owned is not None
    m.complete('s', owned['stepId'], owned['token'],
        {'kind': 'failure', 'code': 'MODEL_CONFIGURATION_REQUIRED'})
    return owned


@pytest.mark.parametrize('completion_before_restart', [True, False])
def test_explicit_restart_after_failure_can_run_despite_final_ac(tmp_path, completion_before_restart):
    m = manager(tmp_path, workflow_version=3)
    m.command('s', 'ask', 'ask', {'text': '请重新核对刚才的判断。'})
    failed = fail_current(m)
    if completion_before_restart:
        m.command('s', 'ac', 'final_completed', {'resultId': 'trusted-ac'})
    reply = m.command('s', 'restart', 'restart', {})
    assert reply == m.command('s', 'restart', 'restart', {})
    if not completion_before_restart:
        m.command('s', 'ac', 'final_completed', {'resultId': 'trusted-ac'})
    owned = m.claim('s', 'worker', materials=materials(m))
    assert owned is not None and owned['roundId'] == reply['roundId']
    assert owned['roundId'] != failed['roundId']
    state = m.read('s')
    assert state['completed']
    assert state['rounds'][reply['roundId']]['allowedTools'] == []
    assert state['rounds'][reply['roundId']]['counts'] == {'model': 1, 'tool': 0, 'execution': 0}


@pytest.mark.parametrize('completion_before_retry', [True, False])
def test_user_retry_of_failed_automatic_round_is_explicit_but_keeps_budget(tmp_path, completion_before_retry):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'run', 'follow_up', {'eventId': 'run-1', 'codeVersion': 1,
        'targets': [{'subgoalId': key, 'version': 1}], 'evidence': {'id': 'run-1'}})
    failed = fail_current(m)
    if completion_before_retry:
        m.command('s', 'ac', 'final_completed', {'resultId': 'trusted-ac'})
    reply = m.command('s', 'retry', 'retry', {})
    assert reply == m.command('s', 'retry', 'retry', {})
    if not completion_before_retry:
        m.command('s', 'ac', 'final_completed', {'resultId': 'trusted-ac'})
    owned = m.claim('s', 'worker', materials=materials(m))
    assert owned is not None and owned['roundId'] == failed['roundId']
    assert owned['input'] == failed['input'] and owned['deadline'] == failed['deadline']
    state = m.read('s')
    assert state['completed']
    assert state['rounds'][reply['roundId']]['counts'] == {'model': 2, 'tool': 0, 'execution': 0}
    assert state['rounds'][reply['roundId']]['allowedTools'] == []


def test_final_ac_still_cancels_automatic_round_and_ignores_late_run(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    event = {'eventId': 'run-1', 'codeVersion': 1,
        'targets': [{'subgoalId': key, 'version': 1}], 'evidence': {'id': 'run-1'}}
    reply = m.command('s', 'run', 'follow_up', event)
    m.command('s', 'ac', 'final_completed', {'resultId': 'trusted-ac'})
    assert m.claim('s', 'worker', materials=materials(m)) is None
    state = m.read('s')
    assert state['rounds'][reply['roundId']]['failure'] == 'GOAL_COMPLETED'
    assert m.command('s', 'late-run', 'follow_up', {**event, 'eventId': 'run-2'})['status'] == 'ignored'


def permitted_materials(m):
    from copy import deepcopy
    from focusmate.core.action_planning import REQUEST_POLICY
    from focusmate.core.context_builder import hash_content
    data = materials(m)
    contract = data['records'][0]
    contract['content']['coachPolicy'] = deepcopy(REQUEST_POLICY)
    contract['contentHash'] = hash_content(contract['content'])
    for record in data['records']:
        if record['kind'] == 'code':
            record['content']['text'] = 'numbers = [1, 2, 3]\nfor value in numbers:\n    print(value)\n'
            record['contentHash'] = hash_content(record['content'])
    return data


def test_user_full_answer_retry_keeps_current_authorization_and_required_review(tmp_path, monkeypatch):
    from tests.python.test_coach_compact_flow import compact, run_step
    monkeypatch.setattr('tests.python.test_coach_compact_flow.materials', permitted_materials)
    m = manager(tmp_path, workflow_version=3)
    m.command('s', 'ask', 'ask', {'text': '请给完整代码'})
    failed = m.claim('s', 'worker', materials=permitted_materials(m))
    m.complete('s', failed['stepId'], failed['token'],
        {'kind': 'failure', 'code': 'MODEL_CONFIGURATION_REQUIRED'})
    m.command('s', 'retry', 'retry', {})
    reply = compact()
    reply['help'].update(answerScope='fullSolution',
        requestQuotes=[{'ref': 'message:1', 'text': '请给完整代码'}])
    run_step(m, reply)
    state = m.read('s')
    current = state['rounds'][state['currentRoundId']]
    assert current['roundId'] == failed['roundId'] and current['trigger'] == 'user'
    assert current['deadline'] == failed['deadline'] and current['counts']['model'] == 2
    assert current['phase'] == 'review' and not state['outbox']


def test_retry_of_automatic_round_cannot_inherit_old_full_answer_request(tmp_path, monkeypatch):
    from tests.python.test_coach_compact_flow import compact, run_step
    monkeypatch.setattr('tests.python.test_coach_compact_flow.materials', permitted_materials)
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'full-answer', 'message', {'text': '请给完整代码'})
    run_step(m, compact())
    m.command('s', 'run', 'follow_up', {'eventId': 'run-1', 'codeVersion': 1,
        'targets': [{'subgoalId': key, 'version': 1}], 'evidence': {'id': 'run-1'}})
    failed = m.claim('s', 'worker', materials=permitted_materials(m))
    m.complete('s', failed['stepId'], failed['token'],
        {'kind': 'failure', 'code': 'MODEL_CONFIGURATION_REQUIRED'})
    before = len(m.read('s')['outbox'])
    m.command('s', 'retry', 'retry', {})
    reply = compact()
    reply['help'].update(answerScope='fullSolution',
        requestQuotes=[{'ref': 'message:2', 'text': '请给完整代码'}])
    with pytest.raises(ValueError, match='FULL_SOLUTION_NOT_AUTHORIZED'):
        run_step(m, reply)
    state = m.read('s')
    assert state['rounds'][state['currentRoundId']]['trigger'] == 'retry'
    assert len(state['outbox']) == before

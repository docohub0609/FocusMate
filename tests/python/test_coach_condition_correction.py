"""Saved model assessments are revisable; this tests flow, not model intelligence."""
from copy import deepcopy

import pytest

from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_coach_compact_flow import compact, run_step
from tests.python.test_coach_v3_flow import establish, result


def invalid_condition(m, key):
    m.command('s', 'check', 'check', {'subgoalIds': [key]})
    run_step(m, result('inconclusive', 'invalid'))
    return deepcopy(m.read('s')['checks'])


def correction(key, kind):
    value = compact(content='刚才的检查条件不适合你的写法；需要纠正的是教练的要求。')
    value['understanding']['finding'] = '原条件不能作为要求用户修改代码的依据。'
    change = {'kind': kind, 'key': key, 'reason': '正式检查指出原条件不适用。'}
    if kind == 'revise':
        change.update(objective='处理实际输入的全部元素',
            checks=['输入中的每个元素恰好被处理一次'],
            acceptableEvidence=['codeStructureCheck'], evidenceRefs=['message:1'])
        value['targets'] = [{'key': key, 'followUp': True}]
    value['goalChanges'] = [change]
    return value


def test_invalid_report_reaches_planner_as_condition_concern(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    reports = invalid_condition(m, key)
    payload, _ = run_step(m, correction(key, 'withdraw'))
    concerns = payload['conditionConcerns']
    assert len(concerns) == 1
    assert concerns[0]['subgoalId'] == key and concerns[0]['workGoalVersion'] == 1
    assert concerns[0]['reportRef'] in reports
    assert concerns[0]['checks'][0]['conditionStatus'] == 'invalid'
    assert concerns[0]['checks'][0]['requirement'] == '遍历包含末元素而不越界'


@pytest.mark.parametrize('kind', ['revise', 'withdraw'])
def test_normal_planner_can_correct_wrong_condition_without_erasing_evidence(tmp_path, kind):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    old_version = deepcopy(m.read('s')['workGoals'][key]['versions'][0])
    reports = invalid_condition(m, key)
    run_step(m, correction(key, kind))
    state = m.read('s')
    goal = state['workGoals'][key]
    assert goal['versions'][0] == old_version and state['checks'] == reports
    assert goal['status'] == ('active' if kind == 'revise' else 'withdrawn')
    assert not state['completed']
    assert state['rounds'][state['currentRoundId']]['counts']['model'] == 2
    assert len(goal['versions']) == (2 if kind == 'revise' else 1)
    assert m.targets('s') == ([{'subgoalId': key, 'version': 2}] if kind == 'revise' else [])
    if kind == 'revise':
        assert state['outbox'][-1]['learningAction']['followUp']['criterionIds'] == [key]
        m.command('s', 'new-message', 'message', {'text': '继续说明现在的条件'})
        payload, _ = run_step(m, compact())
        assert payload['conditionConcerns'] == []


def test_invalid_unchanged_goal_cannot_keep_automatic_modification_followup(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    reports = invalid_condition(m, key)
    # The report exists before planning finishes: even a concurrent run must not
    # pin the rejected condition as a problem that the user still has to fix.
    assert m.targets('s') == []
    value = compact(content='原条件需要重新核对，暂时不要求你照旧建议修改。')
    value['targets'] = [{'key': key, 'followUp': True}]
    run_step(m, value)
    state = m.read('s')
    assert state['rounds'][state['currentRoundId']]['status'] == 'finished'
    assert state['rounds'][state['currentRoundId']]['counts']['model'] == 2
    assert state['outbox'][-1]['learningAction']['followUp']['kind'] == 'none'
    assert state['currentWorkGoalIds'] == [] and m.targets('s') == []
    assert state['workGoals'][key]['status'] == 'active'
    assert state['checks'] == reports and not state['completed']


def test_run_registered_before_invalid_report_cannot_reactivate_wrong_condition(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    pinned = m.targets('s')
    invalid_condition(m, key)
    state = m.read('s')
    counts = deepcopy(state['rounds'][state['currentRoundId']]['counts'])
    reply = m.command('s', 'late-run', 'follow_up', {'eventId': 'run-finished',
        'codeVersion': 1, 'targets': pinned,
        'evidence': {'id': 'run-1', 'completed': True, 'sampleStatus': 'passed'}})
    assert reply['status'] == 'ignored'
    current = m.read('s')
    assert current['currentRoundId'] == state['currentRoundId']
    assert current['rounds'][current['currentRoundId']]['counts'] == counts
    assert current['checks'] == state['checks']


def test_revised_condition_can_receive_new_run_but_not_old_version_run(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    pinned = m.targets('s')
    invalid_condition(m, key)
    run_step(m, correction(key, 'revise'))
    payload = {'eventId': 'run-old', 'codeVersion': 1, 'targets': pinned,
        'evidence': {'id': 'run-old', 'completed': True, 'sampleStatus': 'passed'}}
    assert m.command('s', 'old-run', 'follow_up', payload)['status'] == 'ignored'
    payload.update(eventId='run-new', targets=m.targets('s'),
        evidence={'id': 'run-new', 'completed': True, 'sampleStatus': 'passed'})
    assert m.command('s', 'new-run', 'follow_up', payload)['status'] == 'active'
    state = m.read('s')
    round_state = state['rounds'][state['currentRoundId']]
    assert round_state['trigger'] == 'run' and round_state['phase'] == 'verification'
    assert round_state['targets'] == [{'subgoalId': key, 'version': 2}]


def test_user_can_explicitly_recheck_a_condition_without_immutable_invalid_verdict(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    old_reports = invalid_condition(m, key)
    run_step(m, compact(content='这个条件仍需核对，暂不要求修改。'))
    m.command('s', 'recheck', 'check', {'subgoalIds': [key]})
    run_step(m, result('passed', 'valid'))
    state = m.read('s')
    assert len(state['checks']) == len(old_reports) + 1
    assert all(state['checks'][key] == value for key, value in old_reports.items())
    assert state['workGoals'][key]['status'] == 'verified' and not state['completed']
    assert m.targets('s') == [{'subgoalId': key, 'version': 1}]


def test_uncertain_condition_can_collect_new_evidence_and_never_fails_user_code(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'check', 'check', {'subgoalIds': [key]})
    run_step(m, result('inconclusive', 'uncertain'))
    assert m.targets('s') == [{'subgoalId': key, 'version': 1}]
    value = compact(content='目前还不能判断原条件是否成立，需要补足依据。')
    value['targets'] = [{'key': key, 'followUp': True}]
    payload, _ = run_step(m, value)
    assert payload['conditionConcerns'][0]['checks'][0]['conditionStatus'] == 'uncertain'
    assert m.read('s')['outbox'][-1]['learningAction']['followUp']['kind'] == 'awaitEvidence'
    assert not m.read('s')['completed']


@pytest.mark.parametrize('condition', ['invalid', 'uncertain'])
def test_wrong_or_uncertain_condition_cannot_be_reported_as_user_failure(tmp_path, condition):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'check', 'check', {'subgoalIds': [key]})
    run_step(m, result('failed', condition))
    state = m.read('s')
    report = next(iter(state['checks'].values()))
    assert report['checks'][0]['status'] == 'inconclusive'
    assert report['checks'][0]['conditionStatus'] == condition
    assert report['verifierResult']['status'] == 'inconclusive'
    current = state['rounds'][state['currentRoundId']]
    assert current['phase'] == 'plan' and current['counts']['model'] == 1
    assert not current.get('repairRecord') and not state['completed']


@pytest.mark.parametrize('condition', ['invalid', 'uncertain'])
def test_unsuitable_condition_needs_no_duplicate_code_verdict(tmp_path, condition):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'check', 'check', {'subgoalIds': [key]})
    def answer(payload):
        value = result('failed', condition)(payload)
        del value['checks'][0]['status']
        return value
    run_step(m, answer)
    state = m.read('s')
    report = next(iter(state['checks'].values()))
    assert report['checks'][0]['status'] == 'inconclusive'
    assert state['workGoals'][key]['status'] == 'active'
    # This must continue into the usual correction planner, not content repair.
    payload, _ = run_step(m, correction(key, 'withdraw'))
    assert payload['conditionConcerns'][0]['checks'][0]['conditionStatus'] == condition
    state = m.read('s')
    assert state['workGoals'][key]['status'] == 'withdrawn'
    assert state['rounds'][state['currentRoundId']]['counts']['model'] == 2


def test_valid_condition_still_requires_code_verdict(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'check', 'check', {'subgoalIds': [key]})
    def answer(payload):
        value = result('passed', 'valid')(payload)
        del value['checks'][0]['status']
        return value
    with pytest.raises(ValueError, match='INVALID_VERIFICATION_OUTPUT'):
        run_step(m, answer)
    assert not m.read('s')['checks']


def test_invalid_condition_still_rejects_invented_code_reference(tmp_path):
    m = manager(tmp_path, workflow_version=3)
    key = establish(m)
    m.command('s', 'check', 'check', {'subgoalIds': [key]})
    def answer(payload):
        value = result('failed', 'invalid')(payload)
        value['checks'][0]['quotes'] = [{'codeRef': 'code:nonexistent', 'startLine': 1, 'endLine': 1}]
        return value
    with pytest.raises(ValueError, match='INVALID_CODE_RANGE'):
        run_step(m, answer)
    assert not m.read('s')['checks']

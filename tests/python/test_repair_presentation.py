import pytest

from apps.api.repair_presentation import safe_repair


@pytest.mark.parametrize('status', ['pending', 'running', 'submitted', 'validating', 'succeeded',
                                   'failed', 'abstained', 'interrupted', 'exhausted'])
def test_repair_query_never_exposes_frozen_materials_or_model_prose(status):
    record = {'status': status, 'reason': 'PRIVATE MODEL TEXT', 'failure': {
        'code': 'PRIVATE UNKNOWN CODE', 'reason': 'PRIVATE INPUT'},
        'validation': {'reason': 'PRIVATE OUTPUT'}, 'materials': {'secret': 'PRIVATE'},
        'candidate': {'source': 'PRIVATE REFERENCE'}, 'original': 'PRIVATE RAW RESPONSE'}
    shown = safe_repair(record)
    assert set(shown) == {'status', 'reason'}
    assert shown['status'] == status
    assert 'PRIVATE' not in str(shown)


def test_repair_failure_describes_checked_fault_without_repeating_evidence():
    shown = safe_repair({'status': 'failed', 'failure': {
        'code': 'INVALID_EVIDENCE_QUOTE', 'reason': 'PRIVATE CODE'}})
    assert '代码引用' in shown['reason'] and 'PRIVATE' not in str(shown)
    assert safe_repair({'status': 'failed', 'validation': {
        'code': 'REFERENCE_DISAGREEMENT', 'reason': 'PRIVATE ANSWER'}})['reason'] == (
        '两个参考程序的答案不一致，不能靠改测试输入消除。')
    assert safe_repair(None) is None
    assert safe_repair({'status': 'PRIVATE UNKNOWN STATUS'}) is None


def test_repair_uses_final_failure_instead_of_original_fault():
    record = {'status': 'failed', 'failure': {'code': 'INVALID_EVIDENCE_QUOTE'},
              'reason': 'REPAIR_PROVIDER_FAILED'}
    assert '模型服务调用失败' in safe_repair(record)['reason']
    record['validation'] = {'reason': 'REPAIR_SCOPE_VIOLATION', 'result': 'PRIVATE RAW'}
    assert '超出了允许修改' in safe_repair(record)['reason']
    record['validation']['reason'] = 'PRIVATE RAW FAILURE'
    assert safe_repair(record)['reason'] == '修正内容未通过检查，本次不再自动修复。'


def test_coach_session_query_only_publishes_safe_repair_status(tmp_path):
    from apps.api.coach_service import CoachService
    from tests.python.test_submission_api import running_api, request
    with running_api(tmp_path) as base:
        _, editor = request(base, 'POST', '/api/v1/problems/problem-submit/versions/1/editor-workspace', {})
        goal = editor['goal']['goalId']
        _, workspace = request(base, 'POST', f'/api/v1/goals/{goal}/coach-workspace', {})
        sid = workspace['session']['sessionId']
        url = '/api/v1/coach-sessions/' + sid
        request(base, 'POST', url + '/commands', {'operationId': 'ask', 'kind': 'ask',
            'codeVersion': None, 'payload': {'text': '输入格式是什么？'}})
        service = CoachService(tmp_path / 'submission-api.db')

        def seed(state):
            state['rounds'][state['currentRoundId']]['repairRecord'] = {
                'status': 'running', 'materials': {'source': 'PRIVATE HIDDEN INPUT'},
                'candidate': 'PRIVATE RAW', 'reason': 'PRIVATE DIAGNOSTIC'}
            return {}
        service.manager.store.change(sid, 'fixture-repair', {'fixture': True}, seed)
        status, shown = request(base, 'GET', url)
        assert status == 200
        assert shown['round']['repair'] == safe_repair({'status': 'running'})
        assert 'PRIVATE' not in str(shown)
        assert 'repairRecord' not in shown['round']


def test_coach_failure_retains_saved_reports_and_safe_repair_reason():
    from apps.api.coach_presentation import round_failure
    result = round_failure({'status': 'failed', 'phase': 'repair',
        'failure': 'REPAIR_FAILED', 'reportRefs': ['already-saved'], 'counts': {'model': 4},
        'repairRecord': {'status': 'failed', 'failure': {'code': 'INVALID_FOLLOWUP_TARGET',
            'reason': 'PRIVATE OLD GOAL'}}}, 7)
    assert result['savedReportCount'] == 1
    assert '仍未正确关联当前问题' in result['message']
    assert '已保存的检查报告仍可查看' in result['message']
    assert 'PRIVATE' not in str(result)

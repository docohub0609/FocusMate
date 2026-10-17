"""Public command/claim recovery contract; synthetic failures, no real AI verdicts."""
import pytest
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.core.dynamic_coach import DynamicCoachManager
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import materials


def test_new_round_starts_combined_plan_and_old_round_keeps_its_flow(tmp_path):
    old = manager(tmp_path)
    old.command('s', 'old', 'ask', {'text': '这行什么意思'})
    new = DynamicCoachManager(SQLiteManagerStore(tmp_path/'coach.sqlite3'))
    assert new.claim('s', 'w', materials=materials(new))['kind'] == 'diagnosis'
    new.command('s', 'new', 'message', {'text': '换一个问题'})
    st = new.claim('s', 'w', materials=materials(new))
    assert st['kind'] == 'plan'
    assert new.read('s')['rounds'][st['roundId']]['workflowVersion'] == 3


def test_retry_preserves_round_input_deadline_counts_and_is_idempotent(tmp_path):
    now = [10]
    m = manager(tmp_path, lambda: now[0], workflow_version=2)
    original = m.command('s', 'ask', 'ask', {'text': '这里为什么错'})
    st = m.claim('s', 'w', materials=materials(m))
    m.complete('s', st['stepId'], st['token'], {'kind': 'failure', 'code': 'MODEL_CONFIGURATION_REQUIRED'})
    now[0] = 20
    retry = m.command('s', 'retry', 'retry', {})
    assert retry == m.command('s', 'retry', 'retry', {})
    assert retry['roundId'] == original['roundId']
    resumed = m.claim('s', 'w2', materials=materials(m))
    assert resumed['input'] == st['input']
    assert resumed['kind'] == 'plan' and resumed['deadline'] == 610
    assert m.read('s')['rounds'][resumed['roundId']]['counts']['model'] == 2
    with pytest.raises(ValueError, match='STALE_OWNER'):
        m.complete('s', st['stepId'], st['token'], {'kind': 'failure', 'code': 'LATE'})


def test_changed_code_cannot_resume_old_candidate(tmp_path):
    m = manager(tmp_path, workflow_version=2)
    m.command('s', 'ask', 'ask', {'text': '看下代码'})
    st = m.claim('s', 'w', materials=materials(m))
    m.complete('s', st['stepId'], st['token'], {'kind': 'failure', 'code': 'MODEL_CONFIGURATION_REQUIRED'})
    m.command('s', 'edit', 'code_changed', {'codeVersion': 1})
    with pytest.raises(ValueError, match='RETRY_INPUT_CHANGED'):
        m.command('s', 'retry', 'retry', {})
    restarted = m.command('s', 'restart', 'restart', {})
    assert restarted['roundId'] != st['roundId']
    assert m.read('s')['rounds'][restarted['roundId']]['trigger'] == 'retry'


def test_expired_retry_never_renews_deadline(tmp_path):
    now = [0]
    m = manager(tmp_path, lambda: now[0], workflow_version=2)
    m.command('s', 'ask', 'ask', {'text': '问题'})
    st = m.claim('s', 'w', materials=materials(m))
    m.complete('s', st['stepId'], st['token'], {'kind': 'failure', 'code': 'MODEL_CONFIGURATION_REQUIRED'})
    now[0] = 601
    with pytest.raises(ValueError, match='RETRY_EXPIRED'):
        m.command('s', 'retry', 'retry', {})
    assert m.read('s')['rounds'][st['roundId']]['deadline'] == 600


def test_transient_model_failure_retries_once_without_resetting_budget(tmp_path):
    m = manager(tmp_path, workflow_version=2,repair_policy_version=0)
    m.command('s','ask','ask',{'text':'问题'})
    original=m.claim('s','w',materials=materials(m))
    failure={'kind':'failure','code':'PROVIDER_UNAVAILABLE','retryable':True}
    m.complete('s',original['stepId'],original['token'],failure)
    retry=m.claim('s','w',materials=materials(m))
    assert retry['input']==original['input'] and retry['deadline']==original['deadline']
    m.complete('s',retry['stepId'],retry['token'],failure)
    assert m.claim('s','w',materials=materials(m)) is None
    r=m.read('s')['rounds'][original['roundId']]
    assert r['status']=='failed' and r['counts']['model']==2


def test_recovered_execution_must_be_cleaned_before_explicit_restart(tmp_path):
    now=[0];m=manager(tmp_path,lambda:now[0],workflow_version=2)
    m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','ask','ask',{'text':'运行公开样例'})
    st=m.claim('s','w',materials=materials(m))
    m.complete('s',st['stepId'],st['token'],{'kind':'toolRequest','call':{
        'callId':'run','toolName':'run_public_samples','arguments':{'codeVersion':1}}})
    state=m.read('s');tool=next(x for x in state['steps'].values() if x['kind']=='tool')
    m.command('s','permit','grant_permission',{'tools':['run_public_samples'],'stepId':tool['stepId']})
    owned=m.claim('s','w',materials=materials(m));now[0]=121;m.recover('s')
    assert any(x['stepId']==owned['stepId'] and x['status']=='pending' for x in m.read('s')['cancellations'])
    for kind in ('retry','restart'):
        with pytest.raises(ValueError,match='EXECUTION_OUTCOME_UNKNOWN'):
            m.command('s',kind,kind,{})
    m.record_cancellation('s',owned['stepId'],{'status':'cancelled'})
    with pytest.raises(ValueError,match='NOT_RETRYABLE'):
        m.command('s','retry-cleaned','retry',{})
    restarted=m.command('s','restart-cleaned','restart',{})
    state=m.read('s');new=state['rounds'][restarted['roundId']]
    assert new['allowedTools']==[] and new['roundId']!=owned['roundId']


def test_same_code_version_with_different_material_never_reuses_failed_step(tmp_path):
    m=manager(tmp_path,workflow_version=2);m.command('s','ask','ask',{'text':'问题'})
    st=m.claim('s','w',materials=materials(m))
    m.complete('s',st['stepId'],st['token'],{'kind':'failure','code':'MODEL_CONFIGURATION_REQUIRED'})
    m.command('s','retry','retry',{})
    changed=materials(m);changed['records'][0]['content']['changed']=True
    assert m.claim('s','w',materials=changed) is None
    assert m.read('s')['rounds'][st['roundId']]['failure']=='RETRY_INPUT_CHANGED'


def test_successful_report_is_not_lost_when_following_help_is_retried(tmp_path, monkeypatch):
    from tests.python.test_coach_compact_flow import established, run_step, checked
    m=manager(tmp_path,workflow_version=2);key=established(m)
    from focusmate.core.session_manager import identity
    ids=iter(['step-z-verification','step-a-plan','step-b-resume'])
    monkeypatch.setattr('focusmate.core.dynamic_coach.identity',lambda prefix:next(ids) if prefix=='step' else identity(prefix))
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,checked('failed'))
    r=m.read('s')['rounds'][m.read('s')['currentRoundId']];refs=list(r['reportRefs'])
    owned=m.claim('s','w',materials=materials(m));assert owned['kind']=='plan'
    m.complete('s',owned['stepId'],owned['token'],{'kind':'failure','code':'MODEL_CONFIGURATION_REQUIRED'})
    m.command('s','retry-help','retry',{})
    resumed=m.claim('s','w',materials=materials(m))
    state=m.read('s');r=state['rounds'][resumed['roundId']]
    assert resumed['kind']=='plan' and r['reportRefs']==refs
    assert all(ref in state['checks'] for ref in refs)
    assert len([st for st in state['steps'].values() if st['roundId']==r['roundId'] and st['kind']=='verification'])==1


def test_unknown_execution_reply_registers_cleanup_before_allowing_restart(tmp_path):
    m=manager(tmp_path,workflow_version=2)
    m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','ask','ask',{'text':'运行公开样例'})
    st=m.claim('s','w',materials=materials(m))
    m.complete('s',st['stepId'],st['token'],{'kind':'toolRequest','call':{
        'callId':'run','toolName':'run_public_samples','arguments':{'codeVersion':1}}})
    tool=next(x for x in m.read('s')['steps'].values() if x['kind']=='tool')
    m.command('s','permit','grant_permission',{'tools':['run_public_samples'],'stepId':tool['stepId']})
    owned=m.claim('s','w',materials=materials(m))
    m.complete('s',owned['stepId'],owned['token'],{'kind':'failure','code':'RUNNER_UNAVAILABLE'})
    assert any(x['stepId']==owned['stepId'] and x['status']=='pending' for x in m.read('s')['cancellations'])
    with pytest.raises(ValueError,match='EXECUTION_OUTCOME_UNKNOWN'):
        m.command('s','restart','restart',{})
    m.record_cancellation('s',owned['stepId'],{'status':'cancelled'})
    receipt=m.command('s','restart-cleaned','restart',{})
    assert m.read('s')['rounds'][receipt['roundId']]['allowedTools']==[]

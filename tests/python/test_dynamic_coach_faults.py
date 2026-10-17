"""Simulated races/failures, explicitly separate from real model and Docker evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import step,diagnosis,plan,review,materials
from tests.python.test_runtime_subgoals import mutation


def active(m):
    m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','ask','ask',{'text':'帮我修好后继续检查'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[mutation()]})
    step(m,{'kind':'plan','value':plan(changes=[mutation()],targets=['new:boundary'])})
    step(m,review())
    return m.targets('s')


def test_two_workers_atomically_claim_one_step(tmp_path):
    from focusmate.core.dynamic_coach import DynamicCoachManager
    from focusmate.adapters.manager_storage import SQLiteManagerStore
    m=manager(tmp_path);other=DynamicCoachManager(SQLiteManagerStore(tmp_path/'coach.sqlite3'))
    m.command('s','a','ask',{'text':'原因'})
    with ThreadPoolExecutor(2) as pool:
        calls=list(pool.map(lambda x:x.claim('s','worker',materials=materials(m)),[m,other]))
    assert sum(x is not None for x in calls)==1
    assert next(iter(m.read('s')['rounds'].values()))['counts']['model']==1


def test_pause_retains_finished_review_and_resume_publishes_once(tmp_path):
    m=manager(tmp_path);m.command('s','a','ask',{'text':'原因'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[]})
    step(m,{'kind':'plan','value':plan()})
    owned=m.claim('s','w',materials=materials(m))
    m.command('s','pause','pause',{})
    m.complete('s',owned['stepId'],owned['token'],review())
    assert not m.read('s')['outbox']
    m.command('s','resume','resume',{});m.command('s','resume','resume',{})
    assert len(m.read('s')['outbox'])==1


@pytest.mark.parametrize('change',[None,2])
def test_late_review_never_publishes_after_edit_or_clear(tmp_path,change):
    m=manager(tmp_path);m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','a','ask',{'text':'原因'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[]})
    step(m,{'kind':'plan','value':plan()})
    owned=m.claim('s','w',materials=materials(m))
    m.command('s','edit','code_changed',{'codeVersion':change})
    m.complete('s',owned['stepId'],owned['token'],review())
    assert not m.read('s')['outbox']


def test_same_run_receipt_survives_reopen_and_reply_race(tmp_path):
    m=manager(tmp_path);targets=active(m)
    payload={'eventId':'run1','codeVersion':1,'targets':targets,'evidence':{'id':'run1','completed':True}}
    first=m.command('s','run:1','follow_up',payload)
    m.command('s','reply','message',{'text':'我已经改好了'})
    assert m.command('s','run:1','follow_up',payload)==first
    state=m.read('s')
    assert len([r for r in state['rounds'].values() if r['status']=='queued'])==1
    assert state['rounds'][state['currentRoundId']]['trigger']=='user'


def test_final_completion_blocks_pending_automatic_help_even_while_paused(tmp_path):
    m=manager(tmp_path);targets=active(m)
    m.command('s','run','follow_up',{'eventId':'run1','codeVersion':1,'targets':targets,'evidence':{'id':'run1'}})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[]})
    step(m,{'kind':'plan','value':plan()})
    owned=m.claim('s','w',materials=materials(m));m.command('s','pause','pause',{})
    m.command('s','ac','final_completed',{'resultId':'trusted-ac'})
    m.complete('s',owned['stepId'],owned['token'],review())
    m.command('s','resume','resume',{})
    assert len(m.read('s')['outbox'])==1
    assert m.read('s')['completed']


def test_heartbeat_does_not_extend_round_deadline(tmp_path):
    now=[0];m=manager(tmp_path,lambda:now[0]);m.command('s','a','ask',{'text':'检查'})
    st=m.claim('s','w',materials=materials(m))
    for value in (100,200,300,400,500,599):
        now[0]=value
        assert m.heartbeat('s',st['stepId'],st['token'])<=600
    now[0]=600
    with pytest.raises(ValueError,match='STALE'):m.heartbeat('s',st['stepId'],st['token'])
    m.recover('s');assert m.read('s')['rounds'][st['roundId']]['status']=='expired'


def test_withdrawn_target_and_old_code_cannot_receive_run(tmp_path):
    m=manager(tmp_path);targets=active(m)
    m.command('s','edit','code_changed',{'codeVersion':2})
    result=m.command('s','run','follow_up',{'eventId':'r','codeVersion':1,'targets':targets,'evidence':{}})
    assert result['status']=='ignored'
    assert m.read('s')['rounds'][m.read('s')['currentRoundId']]['status']=='finished'


def test_verified_code_goal_becomes_active_on_new_code_without_losing_proof(tmp_path):
    m=manager(tmp_path);targets=active(m);key=targets[0]['subgoalId']
    def verified(s):
        s['workGoals'][key]['status']='verified';s['workGoals'][key]['verificationRefs']=['report1']
        s['checks']['report1']={'verifierResult':{'inputVersionRefs':{'codeVersion':1}}}
    m._change('s',{'kind':'fixture'},verified)
    m.command('s','edit','code_changed',{'codeVersion':2})
    assert m.targets('s')==targets
    assert m.read('s')['workGoals'][key]['verificationRefs']==['report1']

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.core.session_manager import SessionManager, ManagerError


class Clock:
    value = 1000.0
    def __call__(self): return self.value


def setup(tmp_path):
    clock = Clock()
    manager = SessionManager(SQLiteManagerStore(tmp_path / 'manager.sqlite3'), clock=clock)
    manager.create('s1', {'goalId':'g1','scope':'goal'}, {'problemId':'p1','problemVersion':1},
                   contract_version=1, code_version=3, allowed_tools=['run_public_samples'])
    return manager, clock


def ask(m, key='q1'):
    return m.command('s1', key, 'ask', {'text':'为什么不对？'})['roundId']


def candidate(m):
    job = m.claim('s1', 'worker')
    m.complete('s1', job['stepId'], job['token'], {'kind':'candidate','content':'检查当前更新顺序。'})
    gate = m.claim('s1', 'checker')
    m.complete('s1', gate['stepId'], gate['token'], {'kind':'checked','accepted':True,'source':'developerFixture'})
    return job


def test_same_request_returns_original_and_changed_body_rejected(tmp_path):
    m,c = setup(tmp_path); r=ask(m)
    assert ask(m)==r
    with pytest.raises(ManagerError,match='REQUEST_CONFLICT'):
        m.command('s1','q1','ask',{'text':'另一题'})
    assert len(m.read('s1')['rounds'])==1


def test_new_message_blocks_stale_response_then_uses_same_round_budget(tmp_path):
    m,c=setup(tmp_path); r=ask(m); job=m.claim('s1','w')
    m.command('s1','m2','message',{'text':'还有负数'})
    m.complete('s1',job['stepId'],job['token'],{'kind':'candidate','content':'旧答案'})
    state=m.read('s1'); assert state['outbox']==[]
    assert state['rounds'][r]['counts']['model']==1
    next_job=m.claim('s1','w')
    assert next_job['kind']=='model' and next_job['messageSeq']==2
    assert next_job['deadline']==1120


def test_pause_late_answer_resume_and_publish_once(tmp_path):
    m,c=setup(tmp_path); ask(m); job=m.claim('s1','w')
    m.command('s1','p','pause',{})
    m.complete('s1',job['stepId'],job['token'],{'kind':'candidate','content':'检查顺序'})
    assert m.claim('s1','w') is None
    m.command('s1','resume','resume',{})
    gate=m.claim('s1','w'); assert gate['kind']=='check'
    m.complete('s1',gate['stepId'],gate['token'],{'kind':'checked','accepted':True,'source':'developerFixture'})
    a=m.publish('s1'); assert a['source']=='developerFixture'
    assert m.publish('s1') is None
    reopened=SessionManager(SQLiteManagerStore(m.store.path),clock=c)
    assert reopened.read('s1')['status']=='waiting_user'
    assert reopened.claim('s1','w') is None


def test_version_change_retains_tool_result_without_followup(tmp_path):
    m,c=setup(tmp_path); r=ask(m); j=m.claim('s1','w')
    m.complete('s1',j['stepId'],j['token'],{'kind':'tool','call':{'callId':'call1','toolName':'run_public_samples','arguments':{'codeVersion':3}}})
    t=m.claim('s1','w')
    m.command('s1','v4','code_changed',{'codeVersion':4})
    result={'kind':'tool_result','result':{'callId':'call1','loopId':r,'toolName':'run_public_samples','goalContext':{'goalId':'g1','scope':'goal'},'problemRef':{'problemId':'p1','problemVersion':1},'codeVersion':3,'status':'succeeded','data':{'status':'succeeded'}}}
    m.complete('s1',t['stepId'],t['token'],result)
    assert m.read('s1')['rounds'][r]['status']=='superseded'
    assert m.claim('s1','w') is None
    assert m.read('s1')['steps'][t['stepId']]['result']==result


def test_two_workers_only_one_claim_and_duplicate_completion(tmp_path):
    m,c=setup(tmp_path); ask(m); barrier=Barrier(2)
    def get():
        other=SessionManager(SQLiteManagerStore(m.store.path),clock=c)
        barrier.wait(); return other.claim('s1','w')
    with ThreadPoolExecutor(2) as pool: jobs=list(pool.map(lambda _:get(),range(2)))
    assert sum(j is not None for j in jobs)==1
    j=next(j for j in jobs if j)
    result={'kind':'candidate','content':'候选'}
    m.complete('s1',j['stepId'],j['token'],result)
    m.complete('s1',j['stepId'],j['token'],result)
    assert len(m.read('s1')['steps'])==2


def test_unknown_model_execution_not_replayed_and_late_owner_rejected(tmp_path):
    m,c=setup(tmp_path); r=ask(m); j=m.claim('s1','w',lease_seconds=10)
    c.value+=11; m.recover('s1')
    assert m.read('s1')['rounds'][r]['status']=='interrupted'
    assert m.claim('s1','new') is None
    with pytest.raises(ManagerError,match='STALE_OWNER'):
        m.complete('s1',j['stepId'],j['token'],{'kind':'candidate','content':'迟到'})
    retry=m.command('s1','retry1','retry',{})['roundId']
    assert retry!=r and m.read('s1')['rounds'][retry]['previousRoundId']==r


def test_expiry_not_reset_by_pause_or_new_message(tmp_path):
    m,c=setup(tmp_path); r=ask(m); m.command('s1','p','pause',{})
    c.value+=121; m.command('s1','r','resume',{})
    assert m.read('s1')['rounds'][r]['status']=='expired'
    assert m.claim('s1','w') is None


def test_pause_before_publish_blocks_outbox(tmp_path):
    m,c=setup(tmp_path); ask(m); candidate(m)
    m.command('s1','p','pause',{})
    assert m.publish('s1') is None
    assert m.read('s1')['outbox']==[]


def test_message_before_tool_started_reconsiders_old_tool_request(tmp_path):
    m,c=setup(tmp_path);ask(m);j=m.claim('s1','w')
    m.complete('s1',j['stepId'],j['token'],{'kind':'tool','call':{'callId':'c','toolName':'run_public_samples','arguments':{'codeVersion':3}}})
    m.command('s1','new','message',{'text':'先解释这一行'})
    assert m.claim('s1','w')['kind']=='model'


def test_permission_grant_does_not_reset_budget_or_deadline(tmp_path):
    m,c=setup(tmp_path);r=ask(m);candidate(m)
    m.command('s1','perm','request_permission',{'tools':['read_code_excerpt']})
    c.value+=20
    new=m.command('s1','grant','grant_permission',{'tools':['read_code_excerpt']})['roundId']
    assert m.read('s1')['rounds'][new]['deadline']==1120
    assert m.read('s1')['rounds'][new]['counts']==m.read('s1')['rounds'][r]['counts']


def test_forged_verification_is_not_a_trusted_result(tmp_path):
    m,c=setup(tmp_path);ask(m);candidate(m);a=m.publish('s1')
    m.command('s1','ev','verification',{'actionId':a['actionId'],'materialRefs':['explain1'],'codeVersion':3})
    j=m.claim('s1','w');assert j['kind']=='verify'
    with pytest.raises(ManagerError,match='ROLE_NOT_INTEGRATED'):
        m.complete('s1',j['stepId'],j['token'],{'kind':'verified','actionId':a['actionId'],'codeVersion':3,'status':'passed','source':'user'})
    m.complete('s1',j['stepId'],j['token'],{'kind':'verified','actionId':a['actionId'],'codeVersion':3,'status':'passed','source':'developerFixture'})
    assert 'goalState' not in m.read('s1')


def test_stop_keeps_cancellation_to_be_executed_after_recovery(tmp_path):
    m,c=setup(tmp_path);ask(m);j=m.claim('s1','w')
    m.complete('s1',j['stepId'],j['token'],{'kind':'tool','call':{'callId':'c','toolName':'run_public_samples','arguments':{'codeVersion':3}}})
    t=m.claim('s1','w',lease_seconds=10);c.value+=11;m.recover('s1')
    assert m.read('s1')['cancellations'][0]['stepId']==t['stepId']


@pytest.mark.parametrize('inflight', ['model', 'check', 'tool', 'new_message'])
def test_permission_wait_survives_late_work_and_new_messages(tmp_path, inflight):
    m,c=setup(tmp_path);r=ask(m);job=m.claim('s1','w')
    if inflight=='check':
        m.complete('s1',job['stepId'],job['token'],{'kind':'candidate','content':'检查顺序'})
        job=m.claim('s1','checker')
    elif inflight=='tool':
        m.complete('s1',job['stepId'],job['token'],{'kind':'tool','call':{'callId':'c','toolName':'run_public_samples','arguments':{'codeVersion':3}}})
        job=m.claim('s1','runner')
    elif inflight=='new_message':
        m.complete('s1',job['stepId'],job['token'],{'kind':'candidate','content':'检查顺序'})
    m.command('s1','permission','request_permission',{'tools':['read_code_excerpt']})
    if inflight=='new_message':
        m.command('s1','followup','message',{'text':'先检查这一行'})
    else:
        result={'kind':'candidate','content':'迟到的回答'}
        if inflight=='check':result={'kind':'checked','accepted':True,'source':'developerFixture'}
        elif inflight=='tool':
            result={'kind':'tool_result','result':{'callId':'c','loopId':r,'toolName':'run_public_samples',
                'goalContext':{'goalId':'g1','scope':'goal'},'problemRef':{'problemId':'p1','problemVersion':1},
                'codeVersion':3,'status':'succeeded','data':{}}}
        m.complete('s1',job['stepId'],job['token'],result)
    assert m.read('s1')['rounds'][r]['status']=='waiting_permission'
    assert m.claim('s1','other') is None
    assert m.publish('s1') is None
    new=m.command('s1','grant','grant_permission',{'tools':['read_code_excerpt']})['roundId']
    assert new!=r and m.claim('s1','authorized')['kind']=='model'

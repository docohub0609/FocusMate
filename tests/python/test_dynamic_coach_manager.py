from copy import deepcopy
import pytest
from focusmate.adapters.manager_storage import SQLiteManagerStore


def manager(tmp_path, clock=None, *, workflow_version=1,repair_policy_version=1):
    from focusmate.core.dynamic_coach import DynamicCoachManager
    m = DynamicCoachManager(SQLiteManagerStore(tmp_path/'coach.sqlite3'), workflow_version=workflow_version,
                            repair_policy_version=repair_policy_version,
                            **({'clock': clock} if clock else {}))
    m.create('s', {'goalId':'g','scope':'goal'}, {'problemId':'p','problemVersion':1},
             contract_version=1, code_version=None, allowed_tools=[])
    return m


@pytest.mark.parametrize('field',['seconds','model','tool','execution'])
def test_round_limits_cannot_exceed_approved_ceiling(tmp_path,field):
    from focusmate.core.dynamic_coach import DynamicCoachManager,LIMITS
    limits={**LIMITS,field:LIMITS[field]+1}
    m=DynamicCoachManager(SQLiteManagerStore(tmp_path/'cap.sqlite3'))
    with pytest.raises(ValueError,match='INVALID_LIMITS'):
        m.create('s',{'goalId':'g','scope':'goal'},{'problemId':'p','problemVersion':1},
                 contract_version=1,code_version=None,limits=limits)


def test_queued_time_does_not_consume_budget_and_claim_is_exclusive(tmp_path):
    now = [10]
    m = manager(tmp_path, lambda: now[0])
    first = m.command('s','a','ask',{'text':'这是什么？'})
    assert m.command('s','a','ask',{'text':'这是什么？'}) == first
    with pytest.raises(ValueError, match='CONFLICT'):
        m.command('s','a','ask',{'text':'另一个问题'})
    now[0] = 10000
    st = m.claim('s','worker',materials={'records':[]})
    assert st['deadline'] == 10600
    assert m.claim('s','other',materials={'records':[]}) is None
    assert m.read('s')['rounds'][first['roundId']]['counts']['model'] == 1


def test_save_clears_old_round_but_never_starts_a_model(tmp_path):
    m = manager(tmp_path)
    m.command('s','a','ask',{'text':'为何出错？'})
    step = m.claim('s','worker',materials={'records':[]})
    m.command('s','save','code_changed',{'codeVersion':1})
    assert m.read('s')['rounds'][step['roundId']]['status'] == 'superseded'
    assert m.claim('s','worker',materials={'records':[]}) is None
    m.command('s','empty','code_changed',{'codeVersion':None})
    assert m.read('s')['codeVersion'] is None
    assert m.claim('s','worker',materials={'records':[]}) is None


def test_expired_step_cannot_write_or_be_automatically_replayed(tmp_path):
    now = [0]
    m = manager(tmp_path, lambda: now[0])
    m.command('s','a','ask',{'text':'问题'})
    st = m.claim('s','w',materials={'records':[]})
    now[0] = 121
    with pytest.raises(ValueError, match='STALE'):
        m.complete('s',st['stepId'],st['token'],{'kind':'failure','code':'TEST'})
    m.recover('s')
    assert m.read('s')['rounds'][st['roundId']]['status'] == 'interrupted'
    assert m.claim('s','w',materials={'records':[]}) is None


def test_execution_requires_current_round_permission_and_shared_allowance(tmp_path):
    m=manager(tmp_path);m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','a','ask',{'text':'检查公开样例'})
    st=m.claim('s','w',materials={'records':[]})
    call={'callId':'call1','toolName':'run_public_samples','arguments':{'codeVersion':1}}
    m.complete('s',st['stepId'],st['token'],{'kind':'toolRequest','call':call})
    assert m.claim('s','w',materials={'records':[]}) is None
    pending=next(x for x in m.read('s')['steps'].values() if x['kind']=='tool')
    m.command('s','permit','grant_permission',{'tools':['run_public_samples'],'stepId':pending['stepId']})
    tool=m.claim('s','w',materials={'records':[]})
    assert tool['kind']=='tool'
    assert m.read('s')['rounds'][tool['roundId']]['counts']=={'model':1,'tool':1,'execution':1}
    m.command('s','edit','code_changed',{'codeVersion':2})
    assert m.read('s')['cancellations'][0]['stepId']==tool['stepId']

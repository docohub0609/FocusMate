"""Synthetic provider replies exercise ownership; they do not prove AI accuracy."""
from copy import deepcopy
import json

import pytest
from focusmate.core.dynamic_coach_execution import DynamicCoachExecutor, request_payload
from focusmate.core.dynamic_coach_repair import execute_repair
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import materials
from tests.python.test_coach_compact_flow import compact, approved
from tests.python.test_coach_v3_flow import establish, result as check_reply


class RepairProvider:
    public_config={}

    def __init__(self,candidate,*,read_first=False,callback=None):
        self.candidate=candidate;self.calls=[];self.read_first=read_first;self.callback=callback

    def complete(self,*,messages,tools,deadline):
        self.calls.append(deepcopy({'messages':messages,'tools':tools}))
        if self.callback:self.callback()
        if self.read_first and len(self.calls)==1:
            call={'toolName':'read_material','arguments':{'materialIds':['request']}}
        else:call={'toolName':'submit_repair','arguments':{'replacement':self.candidate}}
        return {'content':'','toolCalls':[{'callId':'repair-call',**call}]}


def new_manager(tmp_path,clock=None,**kwargs):
    return manager(tmp_path,clock,workflow_version=kwargs.pop('workflow_version',3),repair_policy_version=1,**kwargs)


def claim_failure(m,*,kind='MODEL_RESPONSE_INVALID',correctable=True):
    st=m.claim('s','w',materials=materials(m))
    m.complete('s',st['stepId'],st['token'],{'kind':'failure','code':kind,'correctable':correctable,
        'responseText':'{"choice":"help",','formatError':'JSON ends before help is complete'})
    return st


def repair_step(m,provider,review_provider=None):
    st=m.claim('s','repair-worker',materials=materials(m))
    assert st['kind']=='repair'
    response=execute_repair(m,'s',st,provider,review_provider=review_provider)
    m.complete('s',st['stepId'],st['token'],response)
    return st,response


def test_failed_help_one_candidate_reconnects_and_shadow_does_not_publish(tmp_path):
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    original=claim_failure(m)
    seen=[]
    p=RepairProvider(compact(),callback=lambda:seen.append(deepcopy(m.read('s')['outbox'])))
    st,response=repair_step(m,p)
    m.complete('s',st['stepId'],st['token'],response)
    with pytest.raises(ValueError,match='CONFLICT'):
        m.complete('s',st['stepId'],st['token'],{**response,'repairId':'different'})
    s=m.read('s');r=s['rounds'][original['roundId']]
    assert seen==[[]] and len(s['outbox'])==1
    assert r['status']=='finished' and r['counts']['model']==2
    assert r['repairRecord']['modelCalls']==1 and r['repairRecord']['status']=='succeeded'
    assert r['repairRecord']['owner']['sourceStepId']==original['stepId']
    assert {x['function']['name'] for x in p.calls[0]['tools']}=={'submit_repair'}


def test_one_repair_for_entire_round_even_if_later_step_fails(tmp_path):
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'检查末元素'})
    claim_failure(m)
    value=compact(choice='check',create=True,targets=['new:last'])
    repair_step(m,RepairProvider(value))
    r=m.read('s')['rounds'][m.read('s')['currentRoundId']]
    assert r['phase']=='verification'
    claim_failure(m)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='failed' and len([x for x in s['steps'].values() if x['kind']=='repair'])==1
    assert r['counts']['model']==3


@pytest.mark.parametrize('code',['FULL_SOLUTION_NOT_AUTHORIZED','STALE_WORK_GOAL','COACH_STEP_UNAVAILABLE','PROVIDER_UNAVAILABLE'])
def test_internal_permissions_and_transport_do_not_start_content_repair(tmp_path,code):
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m,kind=code)
    r=m.read('s')['rounds'][m.read('s')['currentRoundId']]
    assert r['status']=='failed' and 'repairRecord' not in r and r['counts']['model']==1


@pytest.mark.parametrize('status',['failed','notReady','inconclusive'])
def test_normal_check_conclusion_never_repairs(tmp_path,status):
    from tests.python.test_coach_compact_flow import run_step
    m=new_manager(tmp_path);key=establish(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,check_reply(status))
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert 'repairRecord' not in r and len(s['checks'])==1


def test_second_worker_cannot_claim_same_repair_and_rejected_candidate_stops(tmp_path):
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m);st=m.claim('s','w1',materials=materials(m))
    assert m.claim('s','w2',materials=materials(m)) is None
    p=RepairProvider({'unexpected':'not a reply'})
    response=execute_repair(m,'s',st,p)
    m.complete('s',st['stepId'],st['token'],response)
    r=m.read('s')['rounds'][st['roundId']]
    assert r['status']=='failed' and r['repairRecord']['modelCalls']==1
    assert not m.read('s')['outbox'] and m.claim('s','w2',materials=materials(m)) is None


@pytest.mark.parametrize('command,payload',[('pause',{}),('code_changed',{'codeVersion':1})])
def test_late_candidate_after_pause_or_edit_cannot_publish(tmp_path,command,payload):
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m);st=m.claim('s','w',materials=materials(m))
    p=RepairProvider(compact(),callback=lambda:m.command('s','changed',command,payload))
    with pytest.raises(ValueError):execute_repair(m,'s',st,p)
    assert not m.read('s')['outbox']


def test_unknown_repair_after_crash_never_replays(tmp_path):
    now=[0];m=new_manager(tmp_path,lambda:now[0]);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m);st=m.claim('s','w',materials=materials(m))
    record=m.read('s')['rounds'][st['roundId']]['repairRecord'];record['status']='running'
    m.repair_checkpoint('s',st['stepId'],st['token'],record)
    now[0]=121;m.recover('s')
    r=m.read('s')['rounds'][st['roundId']]
    assert r['repairRecord']['status']=='interrupted'
    with pytest.raises(ValueError,match='REPAIR_ALREADY_ATTEMPTED'):m.command('s','retry','retry',{})


def test_saved_success_after_crash_is_received_without_another_model_call(tmp_path):
    now=[0];m=new_manager(tmp_path,lambda:now[0]);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m);st=m.claim('s','w',materials=materials(m));p=RepairProvider(compact())
    execute_repair(m,'s',st,p)
    assert not m.read('s')['outbox']
    now[0]=121;m.recover('s');m.command('s','retry','retry',{})
    repair_step(m,p)
    assert len(p.calls)==1 and len(m.read('s')['outbox'])==1


def test_existing_success_condition_cannot_be_rewritten_by_repair(tmp_path):
    m=new_manager(tmp_path);key=establish(m)
    original=deepcopy(m.read('s')['workGoals'])
    m.command('s','ask2','message',{'text':'这里怎么看'})
    claim_failure(m)
    value=compact(create=True);value['understanding']['evidenceRefs']=['message:2']
    value['help']['evidenceRefs']=['message:2']
    value['goalChanges'][0].update(kind='revise',key=key,checks=['只要写了循环就算通过'])
    repair_step(m,RepairProvider(value))
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['repairRecord']['validation']['reason']=='REPAIR_SCOPE_VIOLATION'
    assert s['workGoals']==original


def test_candidate_contradiction_can_be_retracted_without_rewriting_goal_or_history(tmp_path):
    from tests.python.test_coach_compact_flow import run_step
    m=new_manager(tmp_path);key=establish(m)
    m.command('s','old-check','check',{'subgoalIds':[key]})
    run_step(m,check_reply('failed'))
    run_step(m,compact())
    before=deepcopy(m.read('s'))
    m.command('s','check','check',{'subgoalIds':[key]})
    source=m.claim('s','w',materials=materials(m))
    bad=check_reply('failed','invalid')(request_payload(m.read('s'),source))
    for item in bad['checks']:
        item['quotes']=[{'codeRef':'code:1','startLine':1,'endLine':1}]
    # Replay a recorded, already rejected legacy candidate, without changing
    # today's parser (which derives inconclusive before this error can occur).
    m.complete('s',source['stepId'],source['token'],{'kind':'failure',
        'code':'INVALID_CONDITION_CANNOT_DECIDE_CODE','correctable':True,
        'responseText':json.dumps(bad)})
    fixed=deepcopy(bad)
    for item in fixed['checks']:item.pop('status')
    provider=RepairProvider(fixed)
    st,response=repair_step(m,provider)
    m.complete('s',st['stepId'],st['token'],response)
    after=m.read('s');r=after['rounds'][source['roundId']]
    assert r['repairRecord']['status']=='succeeded' and len(provider.calls)==1
    assert r['phase']=='plan' and r['counts']['model']==2
    assert after['workGoals'][key]['versions']==before['workGoals'][key]['versions']
    assert after['workGoals'][key]['status']=='active' and not after['completed']
    assert all(after['checks'][ref]==report for ref,report in before['checks'].items())
    new_reports=[report for ref,report in after['checks'].items() if ref not in before['checks']]
    assert len(new_reports)==1
    assert new_reports[0]['checks'][0]['conditionStatus']=='invalid'
    assert new_reports[0]['checks'][0]['status']=='inconclusive'


@pytest.mark.parametrize('fault',['no_evidence','wrong_code'])
def test_repair_cannot_invent_passing_evidence_after_policy_clarification(tmp_path,fault):
    m=new_manager(tmp_path);key=establish(m)
    before=deepcopy(m.read('s'))
    m.command('s','check','check',{'subgoalIds':[key]})
    source=claim_failure(m)
    fixed=check_reply('passed','invalid' if fault=='wrong_code' else 'valid')(request_payload(m.read('s'),source))
    for item in fixed['checks']:
        item['quotes']=[] if fault=='no_evidence' else [{'codeRef':'code:999','startLine':1,'endLine':1}]
    repair_step(m,RepairProvider(fixed))
    after=m.read('s');r=after['rounds'][source['roundId']]
    assert r['repairRecord']['status']=='failed'
    assert after['workGoals']==before['workGoals'] and after['checks']==before['checks']
    assert not after['completed']


def test_new_goal_cannot_replace_existing_condition_during_repair(tmp_path):
    m=new_manager(tmp_path);key=establish(m)
    original=deepcopy(m.read('s')['workGoals'])
    m.command('s','ask2','message',{'text':'继续解释'})
    claim_failure(m)
    value=compact(create=True);value['goalChanges'][0]['replaces']=[key]
    repair_step(m,RepairProvider(value))
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['repairRecord']['validation']['reason']=='REPAIR_SCOPE_VIOLATION'
    assert s['workGoals']==original


def test_legacy_contract_review_consumes_reserved_second_call(tmp_path):
    class ReviewProvider:
        public_config={}
        def __init__(self):self.calls=0;self.payload=None
        def complete(self,*,messages,tools,deadline):
            self.calls+=1
            self.payload=json.loads(messages[-1]['content'])
            value=approved(self.payload)
            return {'content':json.dumps(value),'toolCalls':[]}
    m=new_manager(tmp_path,workflow_version=2);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m);review=ReviewProvider();p=RepairProvider(compact(create=True,targets=['new:last']))
    repair_step(m,p,review)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='finished' and len(s['outbox'])==1
    assert review.calls==1 and len(p.calls)==1 and r['repairRecord']['modelCalls']==2
    assert r['counts']['model']==3
    assert len(s['decisions'])==1 and s['decisions'][0]['review'] is not None
    assert {g['subgoalId'] for g in review.payload['proposedWorkGoals']}==set(s['workGoals'])
    assert review.payload['candidate']==s['decisions'][0]['plan']
    goal=next(iter(s['workGoals'].values()))
    assert goal['versions'][0]['contentHash']==review.payload['proposedWorkGoals'][0]['contentHash']


def test_insufficient_budget_records_reason_without_request(tmp_path):
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    m.store.change('s','limited',{},lambda s:s['limits'].update(model=2))
    claim_failure(m)
    r=m.read('s')['rounds'][m.read('s')['currentRoundId']]
    assert r['repairRecord']['status']=='exhausted' and r['repairRecord']['modelCalls']==0
    assert r['failure']=='REPAIR_BUDGET_EXHAUSTED'


@pytest.mark.parametrize('text',['{"choice":"help","choice":"wait"}',
    '{"choice":"help","unexpected":NaN}', '{"choice":"help","unexpected":1e999}'])
def test_known_json_parser_failures_are_correctable_without_discarding_values(tmp_path,text):
    class TextProvider:
        public_config={}
        def complete(self,**unused):return {'content':text,'toolCalls':[]}
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    st=m.claim('s','w',materials=materials(m))
    failure=DynamicCoachExecutor(TextProvider()).execute(m.read('s'),st)
    assert failure['kind']=='failure' and failure['correctable']
    m.complete('s',st['stepId'],st['token'],failure)
    r=m.read('s')['rounds'][st['roundId']]
    assert r['phase']=='repair' and r['repairRecord']['original']==text
    assert not m.read('s')['outbox']

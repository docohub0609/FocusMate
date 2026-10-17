from copy import deepcopy
import pytest
from focusmate.core.context_builder import hash_content
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_runtime_subgoals import mutation


def materials(m):
    s=m.read('s');seq=len(s['messages']);version=s['codeVersion']
    contract={'coachPolicy':{'completeSolutionAllowed':False}}
    data=[('contract','goal',contract)] + [('message:'+str(x['sequence']),'dialogue',{'role':'user','text':x['text']}) for x in s['messages']]
    if version:data.append(('code:'+str(version),'code',{'snapshotRef':'code:'+str(version),'text':'print(1)','language':'python3'}))
    records=[{'ref':ref,'kind':kind,'version':version or 1,'content':content,'contentHash':hash_content(content),
              'goalContext':s['goalContext']} for ref,kind,content in data]
    return {'records':records,'currentCodeRef':'code:'+str(version) if version else None,
            'userRequestRef':'message:'+str(seq),'contractRef':'contract'}


def diagnosis():
    return {'questions':[{'questionKey':'q','question':'为什么？','dependsOn':[],
        'history':{'relation':'unknown','previousQuestionRefs':[],'reason':'无历史','evidenceRefs':['message:1']},
        'subgoals':[],'findings':[{'text':'用户询问原因','evidenceRefs':['message:1']}],
        'unknowns':[],'evidenceRefs':['message:1']}],'contractConcerns':[]}


def plan(*, changes=(), targets=(), next_step='help'):
    return {'goalChanges':list(changes),'next':next_step,'checkTargets':list(targets) if next_step=='check' else [],
        'action':{'questionKeys':['q'],'subgoalIds':list(targets),'actionType':'hint','hintLevel':1,
            'answerScope':'guided','reason':'针对问题','evidenceRefs':['message:1'],'requestQuotes':[],
            'content':'看看剩余节点数量。','diagnosisCorrections':[],
            'followUp':{'kind':'none','reason':'回答当前问题','criterionIds':[],'checks':[],'expectedUserAction':''}}}


def review():
    from focusmate.core.action_planning import REVIEW_KEYS
    return {'kind':'review','actionReview':{'checks':{k:{'verdict':'pass','reason':'匹配原材料',
        'candidateQuote':'','materialRefs':['message:1']} for k in REVIEW_KEYS}},
        'goalReview':{'status':'passed','reason':'目标符合当前问题','evidenceRefs':['message:1']}}


def step(m, result):
    st=m.claim('s','w',materials=materials(m))
    assert st is not None
    return m.complete('s',st['stepId'],st['token'],result)


def test_review_is_atomic_with_work_goals_and_help(tmp_path):
    m=manager(tmp_path);m.command('s','a','ask',{'text':'为什么不足k也反转了？'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[mutation()]})
    step(m,{'kind':'plan','value':plan(changes=[mutation()],targets=['new:boundary'])})
    assert not m.read('s')['workGoals'] and not m.read('s')['outbox']
    step(m,review())
    state=m.read('s')
    assert len(state['workGoals'])==1 and len(state['outbox'])==1
    goal=next(iter(state['workGoals']))
    assert state['outbox'][0]['learningAction']['subgoalIds']==[goal]
    assert state['status']=='waiting_user'


def test_simple_question_does_not_create_a_goal(tmp_path):
    m=manager(tmp_path);m.command('s','a','ask',{'text':'这一行是什么意思？'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[]})
    step(m,{'kind':'plan','value':plan()});step(m,review())
    assert m.read('s')['workGoals']=={}
    assert len(m.read('s')['outbox'])==1


def test_verifier_report_is_material_for_the_next_plan(tmp_path):
    m=manager(tmp_path);m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','a','ask',{'text':'检查这个边界'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[mutation()]})
    step(m,{'kind':'plan','value':plan(changes=[mutation()],targets=['new:boundary'],next_step='check')})
    step(m,review())
    st=m.claim('s','w',materials=materials(m))
    from focusmate.core.dynamic_coach_roles import verification_bundle
    state=m.read('s');r=state['rounds'][st['roundId']]
    b=verification_bundle(state,r,st)
    from focusmate.core.dynamic_coach_execution import request_payload
    schema=request_payload(state,st)['outputSchema']
    assert schema['properties']['checks']['items']['properties']['evidenceType']['enum']==b['subgoal']['acceptableEvidence']
    m.complete('s',st['stepId'],st['token'],{'kind':'verification','value':{'checks':[
        {'checkId':b['checks'][0]['checkId'],'status':'notReady','reason':'当前代码没有链表处理',
         'quotes':[{'ref':'code:1','text':'print(1)'}],'evidenceType':'codeStructureCheck','limitations':['没有相关实现']} ]}})
    following=m.claim('s','w',materials=materials(m))
    assert following['kind']=='plan'
    from focusmate.core.dynamic_coach_execution import request_payload
    payload=request_payload(m.read('s'),following)
    assert any(x['kind']=='verifiedFact' and x['content']['checks'][0]['status']=='notReady' for x in payload['materials'])
    assert payload['checkBatchDone'] is True


def test_review_failure_does_not_leave_goals(tmp_path):
    m=manager(tmp_path);m.command('s','a','ask',{'text':'原因'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[mutation()]})
    step(m,{'kind':'plan','value':plan(changes=[mutation()],targets=['new:boundary'])})
    rejected=review();rejected['goalReview']['status']='inconclusive'
    step(m,rejected)
    assert not m.read('s')['workGoals'] and not m.read('s')['outbox']


def test_goal_review_rejection_reaches_replan_before_any_goal_or_help_is_published(tmp_path):
    from focusmate.core.dynamic_coach_execution import request_payload
    m=manager(tmp_path,repair_policy_version=0);m.command('s','a','ask',{'text':'改好后再检查，不要执行'})
    change=mutation();change['checks'].append('修改后基于代码结构或用户说明核对，不依赖执行')
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[change]})
    step(m,{'kind':'plan','value':plan(changes=[change],targets=['new:boundary'])})
    rejected=review();rejected['goalReview'].update(status='failed',reason='第三项描述教练操作，不能作为代码成功条件')
    step(m,rejected)
    state=m.read('s')
    assert not state['workGoals'] and not state['outbox']
    st=m.claim('s','w',materials=materials(m));assert st['kind']=='plan'
    payload=request_payload(m.read('s'),st)
    assert payload['previousReview']['goalReview']==rejected['goalReview']
    assert m.read('s')['rounds'][st['roundId']]['counts']['model']==4


@pytest.mark.parametrize('bad_phase',['plan','review'])
def test_last_format_repair_uses_remaining_seventh_call_and_keeps_saved_report(tmp_path,bad_phase):
    from focusmate.core.dynamic_coach_roles import verification_bundle
    m=manager(tmp_path,repair_policy_version=0);m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','a','ask',{'text':'检查边界'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[mutation()]})
    step(m,{'kind':'plan','value':plan(changes=[mutation()],targets=['new:boundary'],next_step='check')})
    step(m,review())
    st=m.claim('s','w',materials=materials(m));state=m.read('s')
    b=verification_bundle(state,state['rounds'][st['roundId']],st)
    m.complete('s',st['stepId'],st['token'],{'kind':'verification','value':{'checks':[
        {'checkId':b['checks'][0]['checkId'],'status':'notReady','reason':'没有相关实现',
         'quotes':[{'ref':'code:1','text':'print(1)'}],'evidenceType':'codeStructureCheck','limitations':['缺材料']}]}})
    if bad_phase=='review':step(m,{'kind':'plan','value':plan()})
    step(m,{'kind':'failure','code':'MODEL_RESPONSE_INVALID','correctable':True,'responseText':'{bad'})
    if bad_phase=='plan':step(m,{'kind':'plan','value':plan()})
    step(m,review())
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==7
    assert len(state['checks'])==1 and len(state['outbox'])==1
    assert m.claim('s','w',materials=materials(m)) is None


def test_dynamic_fact_selection_is_persisted_and_reaches_following_plan(tmp_path):
    from focusmate.core.dynamic_coach_execution import request_payload
    from focusmate.adapters.coach_chat import strict_transport_schema
    from jsonschema import Draft202012Validator
    m=manager(tmp_path);m.command('s','a','ask',{'text':'检查样例'})
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':[mutation()]})
    step(m,{'kind':'plan','value':plan(changes=[mutation()],targets=['new:boundary'],next_step='check')})
    step(m,review())
    material=materials(m)
    content={'examples':[{'input':'1900\n','output':'NO\n'}]}
    material['records'].append({'ref':'sample','kind':'problemFact','version':1,'content':content,
        'contentHash':hash_content(content),'goalContext':m.read('s')['goalContext']})
    st=m.claim('s','w',materials=material);payload=request_payload(m.read('s'),st)
    fact=next(f for f in payload['factCatalog'] if f['path']=='/examples/0/output')
    value={'checks':[{'checkId':payload['requirements'][0]['checkId'],'status':'notReady','reason':'仅题面预期输出，缺用户证据',
        'quotes':[{'factId':fact['factId']}],'evidenceType':'codeStructureCheck','limitations':['未检查代码']}]}
    Draft202012Validator(payload['outputSchema']).validate(value)
    Draft202012Validator(strict_transport_schema(payload['outputSchema'])).validate(value)
    m.complete('s',st['stepId'],st['token'],{'kind':'verification','value':value})
    state=m.read('s');report=next(iter(state['checks'].values()))
    assert report['checks'][0]['quotes'][0]['text']=='{"/examples/0/output":"NO\\n"}'
    following=m.claim('s','w',materials=material)
    next_payload=request_payload(m.read('s'),following)
    assert any(x['kind']=='verifiedFact' and x['content']['checks'][0]['quotes'][0]['factId']==fact['factId']
               for x in next_payload['materials'])


def test_multiple_targets_keep_partial_reports_and_reserve_post_check_budget(tmp_path):
    m=manager(tmp_path);m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','a','ask',{'text':'检查三个问题'})
    changes=[mutation('new:'+str(i)) for i in range(3)]
    targets=[x['key'] for x in changes]
    step(m,{'kind':'diagnosis','value':diagnosis(),'candidateGoals':changes})
    step(m,{'kind':'plan','value':plan(changes=changes,targets=targets,next_step='check')})
    step(m,review())
    from focusmate.core.dynamic_coach_roles import verification_bundle
    for status in ('passed','notReady'):
        st=m.claim('s','w',materials=materials(m));state=m.read('s')
        b=verification_bundle(state,state['rounds'][st['roundId']],st)
        m.complete('s',st['stepId'],st['token'],{'kind':'verification','value':{'checks':[
            {'checkId':b['checks'][0]['checkId'],'status':status,'reason':'显式测试夹具，非真实模型结论',
             'quotes':[{'ref':'code:1','text':'print(1)'}],'evidenceType':'codeStructureCheck',
             'limitations':['仅模拟批次状态，不证明业务内容正确']} ]}})
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert len(state['checks'])==2 and r['counts']['model']==5
    assert len(r['uncheckedTargets'])==1
    assert r['uncheckedTargets'][0]['reason']=='MODEL_BUDGET_RESERVED'
    assert sorted(g['status'] for g in state['workGoals'].values())==['active','active','verified']
    step(m,{'kind':'plan','value':plan()});step(m,review())
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==7

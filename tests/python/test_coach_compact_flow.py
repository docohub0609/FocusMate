"""Controlled model replies test routing and evidence gates, not model quality."""
from copy import deepcopy
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from focusmate.adapters.coach_chat import strict_transport_schema
from focusmate.core.coach_compact_wire import compact_plan_schema, normalize_compact_plan, validate_compact_plan
from focusmate.core.coach_goal_review_wire import goal_requirement_catalog
from focusmate.core.context_builder import hash_content
from focusmate.core.dynamic_coach_execution import DynamicCoachExecutor, request_payload
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import materials, review
from tests.python.test_coach_worker import Provider


def compact(*, choice='help', targets=(), create=False, content='检查循环的结束位置，末元素是否也会被处理。'):
    value={'choice':choice,'understanding':{'question':'末元素为什么没有处理？',
        'finding':'用户当前关注循环的末元素边界。','evidenceRefs':['message:1']}}
    if choice=='help':
        value['help']={'content':content,'actionType':'hint','hintLevel':1,
            'answerScope':'guided','evidenceRefs':['message:1']}
    if choice=='check' or targets:value['targets']=list(targets)
    if create:
        value['goalChanges']=[{'kind':'create','key':'new:last','reason':'用户希望修改后检查这一处问题。',
            'objective':'处理末元素','checks':['遍历包含末元素而不越界'],
            'acceptableEvidence':['codeStructureCheck'],'evidenceRefs':['message:1']}]
    return value


def run_step(m, reply):
    material=materials(m)
    for record in material['records']:
        if record['kind']=='code':
            record['content']['text']='numbers = [1, 2, 3]\nfor value in numbers:\n    print(value)\n'
            record['contentHash']=hash_content(record['content'])
    st=m.claim('s','worker',materials=material)
    assert st is not None
    state=m.read('s');payload=request_payload(state,st)
    answer=reply(payload) if callable(reply) else reply
    provider=Provider([answer]);result=DynamicCoachExecutor(provider).execute(state,st)
    m.complete('s',st['stepId'],st['token'],result)
    return payload, result


def approved(payload):
    value=review();value.pop('kind')
    if payload.get('requirementCatalog'):
        value['goalReview']['requirementReviews']=[{'requirementId':item['requirementId'],
            'verdict':'pass','reason':'模拟模型确认这是末元素的局部性质。'}
            for item in payload['requirementCatalog']]
    return value


def checked(status):
    def reply(payload):
        return {'checks':[{'checkId':item['checkId'],'status':status,
            'reason':'模拟检查判定：遍历原列表包含最后一个值，没有使用越界索引。',
            'quotes':[{'ref':'code:1','text':'for value in numbers:'}],
            'evidenceType':'codeStructureCheck','limitations':['模拟模型用于流程测试，未实际执行程序。']}
            for item in payload['requirements']]}
    return reply


def established(m):
    m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','ask','ask',{'text':'为什么漏掉末元素？改后帮我检查。'})
    run_step(m,compact(create=True,targets=['new:last']))
    run_step(m,approved)
    return next(iter(m.read('s')['workGoals']))


def test_ordinary_help_needs_two_calls_without_a_goal_or_separate_diagnosis(tmp_path):
    m=manager(tmp_path,workflow_version=2)
    m.command('s','ask','ask',{'text':'循环怎么包含最后一个元素？'})
    payload,result=run_step(m,compact())
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['phase']=='review' and r['counts']['model']==1
    assert 'diagnosis' not in payload and 'candidateGoals' not in payload
    assert not state['workGoals'] and not state['outbox']
    assert r['diagnosis']['questions'][0]['findings'][0]['text']==compact()['understanding']['finding']
    run_step(m,approved)
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==2
    assert state['workGoals']=={} and len(state['outbox'])==1
    assert state['currentWorkGoalIds']==[]
    assert state['outbox'][0]['learningAction']['content']==compact()['help']['content']


def test_wait_is_one_understanding_call_and_no_unreviewed_generated_text_is_published(tmp_path):
    m=manager(tmp_path,workflow_version=2);key=established(m)
    m.command('s','ack','message',{'text':'好的，我先去写'})
    value=compact(choice='wait');value['understanding']['finding']='内部判断不能作为未经审核的辅导正文。'
    run_step(m,value)
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==1
    assert r['waitMessage']=='好的，等你修改。'
    assert state['currentWorkGoalIds']==[key]
    assert len(state['outbox'])==1 and len(state['workGoals'])==1


def test_explicit_check_passes_in_one_call_with_alternative_code_and_no_new_help(tmp_path):
    m=manager(tmp_path,workflow_version=2);key=established(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    payload,result=run_step(m,checked('passed'))
    assert payload['previousHelp'] is None
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==1
    assert r['checkBatchDone'] and state['workGoals'][key]['status']=='verified'
    assert len(state['outbox'])==1 and not state['completed']
    report=state['checks'][r['reportRefs'][0]]
    assert report['checks'][0]['quotes'][0]['text']=='for value in numbers:'
    assert r['reportEvents'][report['reportId']]['roundId']==r['roundId']
    assert r['reportEvents'][report['reportId']]['historySequence']>0
    assert m.claim('s','worker',materials=materials(m)) is None


@pytest.mark.parametrize('status',['failed','notReady','inconclusive'])
def test_unsolved_check_is_saved_before_help_and_three_calls_finish(tmp_path,status):
    m=manager(tmp_path,workflow_version=2);key=established(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,checked(status))
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert len(state['checks'])==1 and r['phase']=='plan'
    assert state['workGoals'][key]['status']=='active' and len(state['outbox'])==1
    payload,_=run_step(m,compact(targets=[key],content='这一步还需要补足依据，再检查末元素对应的路径。'))
    assert any(item['kind']=='verifiedFact' and item['content']['verifierResult']['status']==status
               for item in payload['materials'])
    payload,_=run_step(m,approved)
    assert payload['requirementCatalog']==[]  # Same accepted condition is not reviewed a second time.
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==3
    assert len(state['outbox'])==2 and len(state['checks'])==1


def test_first_question_can_establish_a_local_check_without_publishing_fake_success(tmp_path):
    m=manager(tmp_path,workflow_version=2)
    m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','ask','ask',{'text':'这样是否还会漏掉最后一个元素？'})
    run_step(m,compact(choice='check',create=True,targets=['new:last']))
    assert not m.read('s')['workGoals'] and not m.read('s')['checks']
    run_step(m,approved)
    assert not m.read('s')['outbox'] and not m.read('s')['checks']
    run_step(m,checked('passed'))
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['counts']['model']==3 and r['status']=='finished'
    assert len(state['checks'])==1 and not state['outbox']


def test_followup_reuses_goal_and_new_topic_does_not_withdraw_old_problem(tmp_path):
    m=manager(tmp_path,workflow_version=2);key=established(m)
    m.command('s','follow','message',{'text':'这里再解释一点'})
    run_step(m,compact(targets=[key]));payload,_=run_step(m,approved)
    assert payload['requirementCatalog']==[] and len(m.read('s')['workGoals'])==1
    m.command('s','topic','message',{'text':'先解释输入的含义'})
    run_step(m,compact(content='输入是一行数字，以空格分隔。'));run_step(m,approved)
    state=m.read('s')
    assert state['currentWorkGoalIds']==[] and state['workGoals'][key]['status']=='active'
    assert len(state['workGoals'][key]['versions'])==1


def test_unrequested_full_solution_does_not_reach_review(tmp_path):
    m=manager(tmp_path,workflow_version=2)
    m.command('s','ask','ask',{'text':'解释这一处循环'})
    value=compact();value['help']['answerScope']='fullSolution'
    with pytest.raises(ValueError,match='FULL_SOLUTION_NOT_AUTHORIZED'):run_step(m,value)
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert not state['outbox'] and not any(st['kind']=='review' for st in state['steps'].values())


def test_unknown_reference_is_not_replaced_with_a_convenient_known_reference(tmp_path):
    m=manager(tmp_path,workflow_version=2)
    m.command('s','ask','ask',{'text':'解释这一处循环'})
    value=compact();value['understanding']['evidenceRefs']=['fabricated-source']
    with pytest.raises(ValueError,match='INVALID_EVIDENCE_REFERENCE'):run_step(m,value)
    state=m.read('s')
    assert not state['outbox'] and not state['workGoals']


@pytest.mark.parametrize('value',[
    {**compact(choice='wait'),'help':{'content':'不能偷偷发布'}},
    {**compact(),'unexpected':True},
    {**compact(),'goalChanges':[{'kind':'reuse','key':'w','reason':'复用','expectedVersion':1}]},
])
def test_compact_wire_rejects_unknown_fields_instead_of_silently_dropping_them(value):
    with pytest.raises(ValidationError):normalize_compact_plan(value,{})


def test_compact_schema_has_no_program_owned_ids_and_survives_strict_transport():
    for value in [compact(),compact(choice='wait'),compact(choice='check',create=True,targets=['new:last'])]:
        schema=compact_plan_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
        Draft202012Validator(strict_transport_schema(schema)).validate(value)
    output=json.dumps(compact_plan_schema())
    assert 'expectedVersion' not in output and 'questionKeys' not in output
    assert 'diagnosisCorrections' not in output and 'createdAt' not in output
    value=compact();value['help']['hintLevel']='wrong'
    with pytest.raises(ValidationError) as exc:validate_compact_plan(value)
    assert list(exc.value.absolute_path)==['help','hintLevel']


def test_only_created_or_revised_conditions_need_a_second_goal_review(tmp_path):
    m=manager(tmp_path,workflow_version=2);key=established(m)
    m.command('s','new','message',{'text':'现在还要考虑空数组'})
    value=compact(targets=[key]);change=compact(create=True)['goalChanges'][0]
    change.update(kind='revise',key=key,objective='处理末元素与空数组',checks=['遍历包含末元素而不越界','空数组不访问任何索引'])
    value['goalChanges']=[change]
    run_step(m,value)
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    catalog=goal_requirement_catalog(state,r)
    assert len(catalog)==2 and all(item['workGoalVersion']==2 for item in catalog)
    assert len(state['workGoals'][key]['versions'])==1  # No mutation before approval.
    run_step(m,approved)
    assert len(m.read('s')['workGoals'][key]['versions'])==2


def test_claimed_pass_without_current_code_evidence_cannot_close_the_issue(tmp_path):
    m=manager(tmp_path,workflow_version=2);key=established(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    def no_code(payload):
        value=checked('passed')(payload)
        value['checks'][0]['quotes']=[{'ref':'message:1','text':'改后帮我检查'}]
        return value
    with pytest.raises(ValueError,match='CURRENT_CODE_EVIDENCE_REQUIRED'):run_step(m,no_code)
    state=m.read('s')
    assert not state['checks'] and state['workGoals'][key]['status']=='active'
    assert state['rounds'][state['currentRoundId']]['status']!='finished'


def test_saved_check_survives_exhausted_help_format_repair(tmp_path):
    m=manager(tmp_path,workflow_version=2,repair_policy_version=0);key=established(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,checked('notReady'))
    reports=deepcopy(m.read('s')['checks'])
    run_step(m,{'unexpected':'bad output'})
    run_step(m,{'unexpected':'still bad output'})
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r['status']=='failed' and r['phase']=='plan'
    assert state['checks']==reports and r['reportRefs']==list(reports)
    assert len(state['outbox'])==1  # The initial hint remains; failed prose is never published.
    assert r['counts']['model']==3 and state['workGoals'][key]['status']=='active'

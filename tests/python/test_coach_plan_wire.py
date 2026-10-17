"""Model response transport must preserve the checked internal decision contract."""
import json
import pytest
from services.coach_worker.worker import CoachWorker
from tests.python.test_coach_service import setup_service
from tests.python.test_coach_worker import Provider
from tests.python.test_dynamic_coach_loop import diagnosis, plan, review


@pytest.mark.parametrize('legacy',[False,True])
def test_worker_accepts_flat_or_legacy_plan_but_preserves_reviewed_action(tmp_path,legacy):
    _,goal,service=setup_service(tmp_path)
    session=service.workspace(goal,create=True)['session']['sessionId']
    service.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释循环'}})
    candidate=plan()
    wire=candidate if legacy else {**candidate['action'],**{k:v for k,v in candidate.items() if k!='action'}}
    checked=review();checked.pop('kind')
    provider=Provider([{'value':diagnosis(),'candidateGoals':[]},wire,checked])
    worker=CoachWorker(service,provider)
    for _ in range(3):assert worker.process_once()
    public=service.read(session)
    assert public['round']['status']=='finished'
    assert [x['content'] for x in public['items'] if x['kind']=='action']==[candidate['action']['content']]
    review_input=json.loads(provider.calls[2]['messages'][-1]['content'])
    assert review_input['candidate']==candidate


@pytest.mark.parametrize('extra',[{'unknown':'discard me'},{'action':{'answerScope':'fullSolution'}}])
def test_flat_plan_never_discards_unknown_or_mixed_envelope_fields(tmp_path,extra):
    _,goal,service=setup_service(tmp_path)
    session=service.workspace(goal,create=True)['session']['sessionId']
    service.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释'}})
    candidate=plan();wire={**candidate['action'],**{k:v for k,v in candidate.items() if k!='action'},**extra}
    provider=Provider([{'value':diagnosis(),'candidateGoals':[]},wire])
    worker=CoachWorker(service,provider)
    assert worker.process_once() and worker.process_once()
    state=service.manager.read(session)
    assert not state['outbox'] and not state['workGoals']
    assert state['rounds'][state['currentRoundId']]['phase']=='repair'
    assert state['rounds'][state['currentRoundId']]['status']=='processing'


@pytest.mark.parametrize('bad_format',['missing','extra'])
def test_worker_requires_every_goal_condition_review_before_publication(tmp_path,bad_format):
    from tests.python.test_runtime_subgoals import mutation
    _,goal,service=setup_service(tmp_path,repair_policy_version=0)
    session=service.workspace(goal,create=True)['session']['sessionId']
    service.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'修复边界'}})
    checked=review();checked.pop('kind')
    provider=Provider([{'value':diagnosis(),'candidateGoals':[mutation()]},
                       plan(changes=[mutation()],targets=['new:boundary']),checked])
    worker=CoachWorker(service,provider)
    for _ in range(2):assert worker.process_once()
    if bad_format=='extra':
        from focusmate.core.coach_goal_review_wire import goal_requirement_catalog
        state=service.manager.read(session)
        catalog=goal_requirement_catalog(state,state['rounds'][state['currentRoundId']])
        checked['goalReview']['requirementReviews']=[{'requirementId':catalog[0]['requirementId'],
            'verdict':'pass','reason':'模拟审核条件','materialRefs':['message:1']}]
    assert worker.process_once()
    public=service.read(session)
    assert not public['workGoals'] and not [x for x in public['items'] if x['kind']=='action']
    payload=json.loads(provider.calls[-1]['messages'][-1]['content'])
    catalog=payload['requirementCatalog']
    assert len(catalog)==1 and catalog[0]['requirement']==mutation()['checks'][0]
    checked['goalReview']['requirementReviews']=[{'requirementId':catalog[0]['requirementId'],
                                                'verdict':'pass','reason':'模拟模型确认这一项描述代码性质'}]
    provider.responses.append(checked)
    assert worker.process_once()
    if bad_format=='extra':
        repair=json.loads(provider.calls[-1]['messages'][-1]['content'])
        assert 'goalReview.requirementReviews.0' in repair['formatError']
        assert "'materialRefs' was unexpected" in repair['formatError']
        assert json.loads(repair['previousRejectedResponse'])['goalReview']['requirementReviews'][0]['materialRefs']==['message:1']
    assert service.read(session)['round']['status']=='finished'
    state=service.manager.read(session)
    done=[s for s in state['steps'].values() if (s.get('result') or {}).get('goalRequirementAudit')]
    assert done[0]['result']['goalRequirementAudit']['requirements']==catalog


@pytest.mark.parametrize('field,value',[('checks',['userExplanation']),('criterionIds',['new:lcs-state'])])
def test_contradictory_followup_is_repaired_with_field_path_before_review(tmp_path,field,value):
    from copy import deepcopy
    _,goal,service=setup_service(tmp_path,repair_policy_version=0)
    session=service.workspace(goal,create=True)['session']['sessionId']
    service.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释状态定义'}})
    good=plan();good['action']['followUp'].update(kind='awaitInformation',criterionIds=[],checks=[],expectedUserAction='说明状态含义')
    bad=deepcopy(good);bad['action']['followUp'][field]=value
    checked=review();checked.pop('kind')
    provider=Provider([{'value':diagnosis(),'candidateGoals':[]},bad,good,checked])
    worker=CoachWorker(service,provider)
    for _ in range(2):assert worker.process_once()
    assert not service.manager.read(session)['outbox']
    assert worker.process_once()
    repair=json.loads(provider.calls[2]['messages'][-1]['content'])
    assert 'followUp.'+field in repair['formatError']
    assert json.loads(repair['previousRejectedResponse'])==bad
    assert worker.process_once()
    assert service.read(session)['round']['status']=='finished'
    assert [x['content'] for x in service.read(session)['items'] if x['kind']=='action']==[good['action']['content']]

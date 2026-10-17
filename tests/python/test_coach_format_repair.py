from tests.python.test_coach_service import setup_service
from tests.python.test_dynamic_coach_loop import diagnosis,plan,review
from tests.python.test_coach_worker import Provider
from services.coach_worker.worker import CoachWorker


def test_structural_rejection_returns_specific_feedback_and_uses_same_budget(tmp_path):
    e,g,s=setup_service(tmp_path,repair_policy_version=0);session=s.workspace(g,create=True)['session']['sessionId']
    s.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释'}})
    bad=plan();bad['extra']='not allowed'
    checked=review();checked.pop('kind')
    provider=Provider([{'value':diagnosis(),'candidateGoals':[]},bad,plan(),checked])
    worker=CoachWorker(s,provider)
    for _ in range(4):assert worker.process_once()
    state=s.manager.read(session);r=state['rounds'][state['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==4
    assert len(state['outbox'])==1
    import json
    repair=json.loads(provider.calls[2]['messages'][1]['content'])
    assert json.loads(repair['previousRejectedResponse'])==bad
    assert 'Additional properties' in repair['formatError']


def test_schema_lists_real_references_without_changing_validation_rules(tmp_path):
    from focusmate.core.dynamic_coach_execution import constrain_refs
    schema={'properties':{'evidenceRefs':{'type':'array','items':{'type':'string'}}}}
    result=constrain_refs(schema,['code:1'])
    assert result['properties']['evidenceRefs']['items']['enum']==['code:1']
    assert 'enum' not in schema['properties']['evidenceRefs']['items']


def test_reference_enum_does_not_constrain_question_or_goal_ids():
    from focusmate.core.dynamic_coach_execution import constrain_refs
    from focusmate.core.dynamic_coach_roles import PLAN_SCHEMA
    schema=constrain_refs(PLAN_SCHEMA,['code:1','message:1'])
    action=schema['properties']['action']['properties']
    assert 'enum' not in action['questionKeys']['items']
    assert 'enum' not in action['subgoalIds']['items']


def test_review_unknown_reference_names_rejected_id_for_repair(tmp_path):
    import json
    _,g,s=setup_service(tmp_path,repair_policy_version=0);session=s.workspace(g,create=True)['session']['sessionId']
    s.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释'}})
    good=review();good.pop('kind')
    from copy import deepcopy
    bad=deepcopy(good);bad['goalReview']['evidenceRefs']=['code:1']
    provider=Provider([{'value':diagnosis(),'candidateGoals':[]},plan(),bad,good])
    worker=CoachWorker(s,provider)
    for _ in range(4):assert worker.process_once()
    payload=json.loads(provider.calls[3]['messages'][1]['content'])
    assert "invalidEvidenceRefs=['code:1']" in payload['formatError']
    assert 'code:1' not in payload['evidenceCatalog']
    assert s.read(session)['round']['status']=='finished'

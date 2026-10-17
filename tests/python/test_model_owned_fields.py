import json
import time
import pytest
from tests.python.test_verifier_candidates import context,FakeTransport
from services.verifier_builder.candidates import CandidateProvider,CandidateFailure
from tests.python.test_skill_execution import Provider,request,module


IDENTITY=('schemaVersion','role','problemRef','problemContentHash','featureSetId')


def test_builder_needs_content_not_copied_job_identity(context):
    transport=FakeTransport(context,change=lambda role,doc:{k:v for k,v in doc.items() if k not in IDENTITY})
    result=CandidateProvider(transport).generate(*context,time.monotonic()+5)
    assert result.problem_id=='problem-submit' and len(transport.calls)==3
    for _,payload,_ in transport.calls:
        assert not set(IDENTITY)&set(payload['responseSchema']['properties'])


@pytest.mark.parametrize('field,value',[('role','wrong'),('problemContentHash','0'*64),('schemaVersion','2.0.0')])
def test_legacy_builder_identity_conflict_is_not_overwritten(context,field,value):
    transport=FakeTransport(context,change=lambda role,doc:{**doc,field:value})
    with pytest.raises(CandidateFailure):CandidateProvider(transport).generate(*context,time.monotonic()+5)


def test_skill_review_does_not_copy_pinned_quote_or_pointer():
    class CompactReviewer(Provider):
        def complete(self,**kw):
            result=super().complete(**kw);value=json.loads(result.content)
            for item in value.get('checks',{}).values():
                item.pop('quote');item.pop('candidatePointer')
            return module().ProviderResponse(json.dumps(value),'fake')
    provider=CompactReviewer();report=module().execute_skill(request(),provider)
    assert report['status']=='accepted'
    for item in provider.calls[1][1]['reviewSchema']['properties']['checks']['properties'].values():
        assert 'quote' not in item['properties'] and 'candidatePointer' not in item['properties']


def test_mutation_generation_and_legality_review_omit_identity(context):
    from tests.python.test_mutation_candidates import Transport
    from services.verifier_builder.mutation_candidates import MutationProvider
    def strip(doc):
        for key in IDENTITY:doc.pop(key)
    transport=Transport(strip);provider=MutationProvider(transport)
    mutants=provider.generate(*context,time.monotonic()+5)
    assert provider.assess_witnesses(*context,mutants,time.monotonic()+5)=={'boundary':'legal'}
    assert all(not set(IDENTITY)&set(p['responseSchema']['properties']) for p in transport.calls)


def test_full_answer_reviewer_selects_location_without_retyping():
    from tests.python.test_action_policy import answer
    from tests.python.test_action_review import review
    from focusmate.core.coach_review_locations import restore_review_locations
    from focusmate.core.action_planning import validate_action_review
    candidate=answer();value={'actionReview':review('fail')}
    for item in value['actionReview']['checks'].values():
        item.pop('candidateQuote');item['candidateField']='content'
    restored=restore_review_locations(value,candidate)
    assert validate_action_review(restored['actionReview'],candidate,['c02'])=='failed'
    value['actionReview']['checks']['fact']['candidateField']='invented'
    with pytest.raises(ValueError,match='INVALID_REVIEW_FIELD'):restore_review_locations(value,candidate)


def test_message_reference_restores_only_real_user_text():
    from tests.python.test_process_verification import prepared,judgment
    from focusmate.core.coach_condition_verification import finish_condition_verification
    bundle=prepared();value=judgment(bundle)
    for check in value['checks']:
        check['conditionStatus']='valid';check['quotes']=[{'messageRef':'u01'}]
    report=finish_condition_verification(value,bundle)
    assert report['checks'][0]['quotes'][0]['text']==bundle['records'][-1]['content']['text']
    value['checks'][0]['quotes']=[{'messageRef':'g01'}]
    with pytest.raises(ValueError,match='INVALID_USER_MESSAGE_REFERENCE'):finish_condition_verification(value,bundle)


def test_compact_input_legality_review_keeps_full_saved_binding(context):
    from tests.python.test_verifier_input_repair import Transport
    from services.verifier_builder.input_repair import InputRepairProvider
    from services.verifier_builder.cross_check import CheckedCase
    transport=Transport(context,lambda doc:{k:v for k,v in doc.items() if k not in IDENTITY})
    review=InputRepairProvider(CandidateProvider(transport)).review_cases(*context,
        (CheckedCase('c','small',('minimumSize',),b'1\n1\n'),),time.monotonic()+5)
    assert review['problemRef']['problemId']=='problem-submit'
    assert review['cases'][0]['status']=='legal'


def test_full_solution_still_crosses_independent_review_with_field_locations(tmp_path,monkeypatch):
    from tests.python.test_dynamic_coach_manager import manager
    import tests.python.test_coach_compact_flow as helper
    from focusmate.core.action_planning import REQUEST_POLICY
    from focusmate.core.context_builder import hash_content
    original=helper.materials
    def materials(m):
        data=original(m);contract=data['records'][0]
        contract['content']['coachPolicy']=REQUEST_POLICY
        contract['contentHash']=hash_content(contract['content'])
        return data
    monkeypatch.setattr(helper,'materials',materials)
    m=manager(tmp_path,workflow_version=3)
    m.command('s','ask','ask',{'text':'请给完整代码'})
    value=helper.compact();value['help'].update(answerScope='fullSolution',requestQuotes=[{'ref':'message:1','text':'请给完整代码'}])
    helper.run_step(m,value)
    assert not m.read('s')['outbox']
    def reviewed(payload):
        value=helper.approved(payload)
        assert 'candidateFields' in payload
        for item in value['actionReview']['checks'].values():
            item.pop('candidateQuote');item['candidateField']=None
        return value
    helper.run_step(m,reviewed)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==2
    assert s['outbox'][-1]['learningAction']['semanticStatus']=='modelReviewedNotVerified'

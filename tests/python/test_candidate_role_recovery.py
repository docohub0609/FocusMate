"""Saved roles survive a sibling format failure, without sharing answers."""
import json
import time
import pytest
from apps.api.deepseek_provider import ProviderResponse
from services.verifier_builder.candidates import CandidateProvider, CandidateFailure
from tests.python.test_verifier_candidates import context, FakeTransport


def test_completed_roles_survive_and_only_missing_role_is_requested(context):
    saved={}
    bad=FakeTransport(context,change=lambda role,doc:'{' if role=='slowOracle' else doc)
    with pytest.raises(CandidateFailure):
        CandidateProvider(bad).generate(*context,time.monotonic()+10,checkpoints=saved,save_role=lambda role,value:saved.__setitem__(role,value))
    assert set(saved)=={'inputGenerator','fastReference'}
    good=FakeTransport(context)
    CandidateProvider(good).generate(*context,time.monotonic()+10,checkpoints=saved)
    assert [x[1]['role'] for x in good.calls]==['slowOracle']


def test_one_format_correction_keeps_role_context_and_original(context):
    class Transport(FakeTransport):
        def complete(self,**kwargs):
            payload=json.loads(kwargs['user_prompt'])
            response=super().complete(**kwargs)
            if payload['role']=='slowOracle' and 'formatCorrection' not in payload:
                return ProviderResponse(response.response_id,response.model,'{')
            if 'formatCorrection' in payload:
                assert payload['formatCorrection']['originalResponse']=='{'
                assert payload['formatCorrection']['error']
                assert 'PRIVATE-fastReference' not in kwargs['user_prompt']
            return response
    transport=Transport(context)
    CandidateProvider(transport).generate(*context,time.monotonic()+10)
    assert len(transport.calls)==4


def test_role_checkpoint_is_fenced_and_content_failure_clears_it(tmp_path):
    from datetime import timedelta
    from tests.python.test_verifier_builder_jobs import jobs, MutableClock
    from tests.python.test_submission_storage import frozen_problem
    from apps.api.problem_storage import StorageFailure
    clock=MutableClock();path=tmp_path/'roles.db';frozen_problem(path)
    repo=jobs(path,clock);claim=repo.discover_and_claim('one',30)
    repo.save_role(claim,'slowOracle',{'private':'saved'})
    repo.pause(claim,'CANDIDATE_SCHEMA_INVALID');repo.retry('problem-submit',1)
    second=repo.discover_and_claim('two',30)
    assert repo.role_checkpoints(second)=={'slowOracle':{'private':'saved'}}
    with pytest.raises(StorageFailure):repo.save_role(claim,'slowOracle',{'private':'stale'})
    repo.pause(second,'REFERENCE_DISAGREEMENT');repo.retry('problem-submit',1)
    third=repo.discover_and_claim('three',30)
    assert repo.role_checkpoints(third)=={}
    clock.value+=timedelta(seconds=31)
    with pytest.raises(StorageFailure):repo.save_role(third,'slowOracle',{'private':'late'})

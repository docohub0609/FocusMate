"""Original build gates run after one restricted candidate, without a second retry."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
import sqlite3

import pytest

from apps.api.problem_storage import StorageFailure
from services.verifier_builder import automatic_repair
from services.verifier_builder.candidates import CandidateProvider
from tests.python.test_verifier_builder_worker import environment, asset_status
from tests.python.test_builder_case_repair import Repair
from tests.python.test_verifier_cross_check import cases, result


class Agent:
    def __init__(self, candidate, callback=None):
        self.candidate, self.callback, self.calls = candidate, callback, []

    def complete(self, **request):
        self.calls.append(request)
        if self.callback:
            self.callback()
        return {'content':'', 'toolCalls':[{'toolName':'submit_repair', 'callId':'one',
            'arguments':{'replacement':self.candidate}}]}


def connect(environment, callback=None, extra=None):
    path, jobs, runner, provider, worker = environment
    provider._parse_role = CandidateProvider(None)._parse_role
    candidate = {'source':provider.candidates.generator.source, 'coveragePlan':[
        {'tag':item.tag,'featureKeys':list(item.feature_keys)} for item in provider.candidates.coverage_plan]}
    candidate.update(extra or {})
    agent = Agent(candidate, callback)
    worker.repair_agent_provider = agent
    worker.automatic_review_provider = Repair(runner)
    return agent


def receipt(path):
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT record_json FROM verifier_auto_repairs').fetchall()
    assert len(rows) == 1
    return json.loads(rows[0][0])


def test_normal_build_has_no_repair_calls_or_receipts(environment):
    path, jobs, runner, provider, worker = environment
    agent = connect(environment)
    assert worker.run_once().status == 'completed'
    assert not agent.calls
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM verifier_auto_repairs').fetchone()[0] == 0


def test_new_policy_keeps_limited_material_without_claiming_trusted_completion(environment):
    from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
    path,jobs,runner,provider,worker=environment
    class NoChallenge:
        last_requests=[]
        def generate(self,*args,**kwargs):return ()
    worker.checker.mutation_provider=NoChallenge()
    assert worker.run_once().status=='completed'
    material=SQLiteTrustedAssetProvider(path).get('problem-submit',1)
    assert material.assurance=='limited'
    with sqlite3.connect(path) as db:
        report=json.loads(db.execute('SELECT cross_check_report_json FROM verifier_assets').fetchone()[0])
    assert report['checkPolicyVersion']=='verifier-cross-check-v4'
    assert report['mutationRepairCount']==report['mutationEligible']==report['mutationKilled']==0
    assert report['warningCodes']==['MUTATION_CHALLENGE_UNAVAILABLE']
    # T074 relies on material assurance, not the worker completed state.
    assert jobs.get('problem-submit',1)['assurance']=='limited'


def test_explicit_retry_upgrades_old_policy_without_reusing_unreviewed_generator(environment):
    path,jobs,runner,provider,worker=environment
    claim=jobs.discover_and_claim('old-worker',60)
    jobs.save_checkpoint(claim,asdict(provider.candidates))
    with jobs._connect() as db:db.execute('UPDATE verifier_build_jobs SET repair_policy=0')
    jobs.pause(claim,'VERIFIER_BUILDER_INTERNAL_ERROR')
    jobs.retry('problem-submit',1)
    next_claim=jobs.discover_and_claim('new-worker',60)
    assert next_claim.repair_policy==1 and next_claim.attempt==2
    assert jobs.checkpoint(next_claim) is None


def test_one_generator_candidate_and_one_review_continue_original_build(environment):
    path, jobs, runner, provider, worker = environment
    runner.generated = json.dumps({'cases':cases(True)[:3]}).encode()
    agent = connect(environment, lambda:setattr(runner,'generated',json.dumps({'cases':cases(True)}).encode()))
    assert worker.run_once().status == 'completed'
    value = receipt(path)
    assert value['status'] == 'succeeded' and value['modelCalls'] == 2
    assert [call['purpose'] for call in value['calls']] == ['repair','inputLegalityReview']
    assert all(call['status'] == 'returned' for call in value['calls'])
    assert len(agent.calls) == len(worker.automatic_review_provider.reviews) == 1
    assert value['validation']['result']['candidates']['slow_oracle'] == asdict(provider.candidates.slow_oracle)
    assert value['validation']['result']['candidates']['fast_reference'] == asdict(provider.candidates.fast_reference)
    assert asset_status(path) == 'ready'
    assert 'SECRET' not in json.dumps(jobs.get('problem-submit',1))
    assert worker.run_once() is None


def test_exit_137_on_generated_small_input_keeps_exit_info_and_can_repair(environment):
    path, jobs, runner, provider, worker = environment
    repaired = [False]
    runner.change = lambda role,request,_: result(reason='exited',exit_code=137) if (
        role == 'slow' and request.operation == 'execute' and request.stdin == b'100\n' and not repaired[0]) else None
    agent = connect(environment, lambda:repaired.__setitem__(0,True))
    assert worker.run_once().status == 'completed'
    value = receipt(path)
    assert value['failure']['code'] == 'REFERENCE_EXECUTION_FAILED'
    assert value['materials']['failure']['execution']['exitCode'] == 137
    assert value['materials']['failure']['execution']['terminationReason'] == 'exited'
    assert 'SMALL_REFERENCE_TIMEOUT' not in json.dumps(value)
    assert len(agent.calls) == 1


@pytest.mark.parametrize('kind',['disagreement','public','infrastructure'])
def test_ineligible_failures_do_not_launch_repair(environment,kind):
    path, jobs, runner, provider, worker = environment
    def changed(role, request, _):
        if request.operation != 'execute':return None
        if kind == 'disagreement' and role == 'slow' and request.stdin == b'100\n':return result(b'wrong')
        if kind == 'public' and role == 'fast' and request.stdin in runner.public:return result(b'wrong')
        if kind == 'infrastructure':return result(reason='infrastructureError',exit_code=None)
    runner.change = changed
    agent = connect(environment)
    assert worker.run_once().status == 'paused'
    assert not agent.calls
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM verifier_auto_repairs').fetchone()[0] == 0


@pytest.mark.parametrize('extra',[{}, {'slowOracle':'overwrite'}, {'source':''}])
def test_rejected_candidate_stops_without_second_candidate_or_publication(environment,extra):
    path,jobs,runner,provider,worker = environment
    runner.generated = json.dumps({'cases':cases(True)[:3]}).encode()
    agent = connect(environment,extra=extra)
    assert worker.run_once().status == 'paused'
    value = receipt(path)
    assert value['status'] == 'failed' and value['modelCalls'] == 1
    assert len(agent.calls) == 1 and not worker.automatic_review_provider.reviews
    assert asset_status(path) == 'building'
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute('SELECT candidate_json FROM verifier_candidate_sets').fetchone()[0])
    assert saved['generator'] == asdict(provider.candidates.generator)


def test_receipt_is_idempotent_and_fenced_by_same_attempt_and_lease(environment):
    path,jobs,runner,provider,worker = environment
    from focusmate.core.repair_agent import new_repair_record, bind_candidate
    claim = jobs.discover_and_claim('owner',60)
    assert claim.repair_policy == 1 and claim.attempt == 1
    value = new_repair_record(repair_id='one',owner={'attempt':1},failure={'code':'X'},materials={},original={})
    automatic_repair.save(jobs,claim,value)
    automatic_repair.save(jobs,claim,value)
    bind_candidate(value,{'source':'print(1)'})
    automatic_repair.save(jobs,claim,value)
    other = deepcopy(value);other['candidate']={'source':'print(2)'}
    with pytest.raises(StorageFailure,match='cannot replace'):
        automatic_repair.save(jobs,claim,other)
    value['modelCalls']=1
    automatic_repair.save(jobs,claim,value)
    other=deepcopy(value);other['modelCalls']=0
    with pytest.raises(StorageFailure,match='restore spent budget'):
        automatic_repair.save(jobs,claim,other)
    now = jobs.clock();jobs.clock=lambda:now+timedelta(seconds=61)
    with pytest.raises(StorageFailure):automatic_repair.save(jobs,claim,value)
    assert jobs.discover_and_claim('replacement',60) is None
    assert receipt(path)['status'] == 'interrupted'
    assert jobs.get('problem-submit',1)['status'] == 'paused'


def test_saved_repair_survives_failure_before_sealing_without_new_model(environment):
    path,jobs,runner,provider,worker = environment
    runner.generated=json.dumps({'cases':cases(True)[:3]}).encode()
    agent=connect(environment,lambda:setattr(runner,'generated',json.dumps({'cases':cases(True)}).encode()))
    original_factory=worker.builder_factory
    worker.builder_factory=lambda *args:(_ for _ in ()).throw(RuntimeError('after validation'))
    assert worker.run_once().status=='paused'
    assert receipt(path)['status']=='succeeded'
    jobs.retry('problem-submit',1)
    worker.builder_factory=original_factory
    assert worker.run_once().status=='completed'
    assert len(agent.calls)==len(worker.automatic_review_provider.reviews)==1
    assert provider.calls==1


def test_generator_format_failure_reuses_successful_reference_roles(environment):
    from apps.api.deepseek_provider import ProviderResponse
    path,jobs,runner,old_provider,worker = environment
    original=old_provider.candidates
    class Transport:
        def __init__(self):self.calls=[]
        def complete(self,**request):
            role=json.loads(request['user_prompt'])['role'];self.calls.append(role)
            if role=='inputGenerator':return ProviderResponse('first-generator','fixture','{"source":')
            if role=='slowOracle':value={'source':original.slow_oracle.source}
            else:value={'source':original.fast_reference.source,'mutants':[
                {'mutantKey':item.mutant_key,'defectTag':item.defect_tag,'source':item.source}
                for item in original.mutants]}
            return ProviderResponse('original-'+role,'fixture',json.dumps(value))
    transport=Transport();worker.provider=CandidateProvider(transport)
    worker.repair_agent_provider=Agent({'source':original.generator.source,'coveragePlan':[
        {'tag':item.tag,'featureKeys':list(item.feature_keys)} for item in original.coverage_plan]})
    worker.automatic_review_provider=Repair(runner)
    assert worker.run_once().status=='completed'
    assert sorted(transport.calls)==['fastReference','inputGenerator','slowOracle']
    value=receipt(path)
    assert value['status']=='succeeded' and value['modelCalls']==2
    assert value['original']=='{"source":'
    assert value['validation']['result']['candidates']['slow_oracle']['source']==original.slow_oracle.source


def test_internal_validation_failure_is_retained_without_second_call(environment):
    path,jobs,runner,provider,worker = environment
    runner.generated = json.dumps({'cases':cases(True)[:3]}).encode()
    agent = connect(environment,lambda:setattr(runner,'generated',json.dumps({'cases':cases(True)}).encode()))
    worker.automatic_review_provider.review_cases=lambda *args:(_ for _ in ()).throw(RuntimeError('private crash'))
    assert worker.run_once().status == 'paused'
    assert receipt(path)['status'] == 'failed'
    assert receipt(path)['validation']['result']['code'] == 'VERIFIER_BUILDER_INTERNAL_ERROR'
    assert len(agent.calls)==1
    assert 'private crash' not in json.dumps(jobs.get('problem-submit',1))

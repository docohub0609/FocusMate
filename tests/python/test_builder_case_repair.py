from dataclasses import replace, asdict
from datetime import timedelta
import hashlib
import json
import sqlite3
import pytest

from services.verifier_builder.cross_check import CrossCheckFailure
from tests.python.test_verifier_builder_worker import environment as base_environment, asset_status
from tests.python.test_verifier_cross_check import cases, result


@pytest.fixture
def environment(base_environment):
    # These tests describe already-started jobs, which retain their old protocol.
    from apps.api.problem_intake import timestamp
    path, jobs, *_ = base_environment
    with jobs._connect() as db:
        jobs._discover_one(db, timestamp(jobs.clock()))
        db.execute('UPDATE verifier_build_jobs SET repair_policy=0')
    def resume_legacy(problem_id, version):
        # Simulate the persisted old protocol's authorized recovery. Current
        # user-initiated retry upgrades policy and has separate v4 coverage.
        with jobs._connect() as db:
            db.execute("UPDATE verifier_build_jobs SET status='queued',phase=NULL,issue_code=NULL WHERE problem_id=? AND problem_version=?",(problem_id,version))
        return jobs.get(problem_id,version)
    jobs.retry=resume_legacy
    return base_environment


class Repair:
    def __init__(self, runner, fail=False):
        self.runner, self.fail = runner, fail
        self.calls, self.reviews = [], []

    def repair(self, record, features, candidates, feedback, deadline):
        self.calls.append((feedback, deadline))
        if not self.fail:
            self.runner.generated = json.dumps({'cases': cases(True)}).encode()
        return replace(candidates, generator=replace(candidates.generator, response_id='repaired',
            prompt_version='verifier-input-generator-repair-v1'))

    def review_cases(self, record, features, selected, deadline):
        self.reviews.append((selected, deadline))
        inputs = [{'caseKey': c.case_key, 'stdin': c.stdin.decode('utf-8')} for c in selected]
        digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        return {'caseSetHash': 'sha256:' + digest,
            'cases': [{'caseKey': c.case_key, 'status': 'legal'} for c in selected]}


def test_missing_coverage_repaired_once_preserving_references_and_deadline(environment):
    path,jobs,runner,provider,worker=environment
    runner.generated=json.dumps({'cases':cases(True)[:3]}).encode()
    repair=Repair(runner);worker.input_repair_provider=repair
    outcome=worker.run_once()
    assert outcome.status=='completed'
    assert len(repair.calls)==len(repair.reviews)==1
    assert repair.calls[0][1]==repair.reviews[0][1]
    assert asset_status(path)=='ready' and provider.calls==1
    with sqlite3.connect(path) as db:
        checkpoint=json.loads(db.execute('select candidate_json from verifier_candidate_sets').fetchone()[0])
        saved=db.execute('select status,feedback_json,review_json from verifier_case_repairs').fetchall()
    assert checkpoint['slow_oracle']==asdict(provider.candidates.slow_oracle)
    assert checkpoint['fast_reference']==asdict(provider.candidates.fast_reference)
    assert len(saved)==1 and saved[0][0]=='checked' and saved[0][2]
    assert 'SECRET' not in json.dumps(jobs.get('problem-submit',1))


def test_repair_does_not_loop_when_coverage_still_missing(environment):
    path,jobs,runner,provider,worker=environment
    runner.generated=json.dumps({'cases':cases(True)[:3]}).encode()
    repair=Repair(runner,fail=True);worker.input_repair_provider=repair
    outcome=worker.run_once()
    assert outcome.issue_code=='GENERATOR_COVERAGE_INSUFFICIENT'
    assert len(repair.calls)==1 and not repair.reviews
    assert asset_status(path)=='building'


@pytest.mark.parametrize('reason,code',[('wallTimeExceeded','SMALL_REFERENCE_TIMEOUT'),('exited','REFERENCE_EXECUTION_FAILED')])
def test_only_explicit_slow_small_timeout_requests_shrinking(environment,reason,code):
    path,jobs,runner,provider,worker=environment
    def change(role,req,_):
        if role=='slow' and req.stdin==b'100\n': return result(reason=reason,exit_code=137)
    runner.change=change
    repair=Repair(runner);worker.input_repair_provider=repair
    outcome=worker.run_once()
    assert outcome.issue_code==code
    assert len(repair.calls)==(1 if reason=='wallTimeExceeded' else 0)
    assert asset_status(path)=='building'


def test_answer_disagreement_never_uses_input_repair(environment):
    _,_,runner,_,worker=environment
    runner.change=lambda role,req,_:result(b'WRONG') if role=='slow' and req.stdin==b'100\n' else None
    repair=Repair(runner);worker.input_repair_provider=repair
    assert worker.run_once().issue_code=='REFERENCE_DISAGREEMENT'
    assert not repair.calls


def test_uncertain_legality_cannot_seal(environment):
    path,_,runner,_,worker=environment
    from services.verifier_builder.candidates import CandidateFailure
    runner.generated=json.dumps({'cases':cases(True)[:3]}).encode()
    repair=Repair(runner)
    def reject(*args):raise CandidateFailure('GENERATOR_INPUT_LEGALITY_UNCONFIRMED','Unconfirmed.',True)
    repair.review_cases=reject;worker.input_repair_provider=repair
    assert worker.run_once().issue_code=='GENERATOR_INPUT_LEGALITY_UNCONFIRMED'
    assert asset_status(path)=='building'


def test_repair_receipt_is_exclusive_and_old_worker_cannot_write(environment):
    from datetime import timedelta
    from apps.api.problem_storage import StorageFailure
    from services.verifier_builder import repair_storage
    path,jobs,_,provider,worker=environment
    claim=jobs.discover_and_claim('first',60)
    jobs.save_checkpoint(claim,asdict(provider.candidates))
    assert repair_storage.reserve(jobs,claim,{'reason':'missingLargeCoverage'},provider.candidates)
    assert not repair_storage.reserve(jobs,claim,{'reason':'missingLargeCoverage'},provider.candidates)
    assert jobs.get('problem-submit',1)['phase']=='repairingCases'
    assert jobs.discover_and_claim('second',60) is None
    now=jobs.clock();jobs.clock=lambda:now+timedelta(seconds=120)
    assert jobs.discover_and_claim('second',60) is None
    assert jobs.get('problem-submit',1)['issueCode']=='BUILDER_LEASE_EXPIRED'
    with pytest.raises(StorageFailure):repair_storage.save_generator(jobs,claim,provider.candidates)
    with pytest.raises(StorageFailure):repair_storage.save_review(jobs,claim,{'cases':[]}, ())
    # A restarted worker cannot replay an uncertain network request automatically.
    assert worker.run_once() is None and provider.calls==0


def test_repair_cannot_extend_original_deadline(environment):
    path,_,runner,_,worker=environment
    now=[10.0];worker.monotonic=lambda:now[0];worker.checker.monotonic=worker.monotonic
    worker.build_seconds=60
    runner.generated=json.dumps({'cases':cases(True)[:3]}).encode()
    repair=Repair(runner);original=repair.repair
    def late(*args):
        candidate=original(*args);now[0]=args[-1];return candidate
    repair.repair=late;worker.input_repair_provider=repair
    assert worker.run_once().issue_code=='BUILD_BUDGET_EXCEEDED'
    assert repair.calls[0][1]==70.0 and not repair.reviews
    assert asset_status(path)=='building'


@pytest.mark.parametrize('interruption', ['network', 'budget', 'lease', 'configuration'])
def test_explicit_retry_restores_input_review_without_regenerating(environment, interruption):
    from services.verifier_builder.candidates import CandidateFailure
    path, jobs, runner, provider, worker = environment
    now = [10.0]
    worker.monotonic = worker.checker.monotonic = lambda: now[0]
    worker.build_seconds = 60
    runner.generated = json.dumps({'cases': cases(True)[:3]}).encode()
    repair = Repair(runner)
    original = repair.review_cases
    first = [True]

    def interrupted(*args):
        review = original(*args)
        if first[0]:
            first[0] = False
            if interruption in {'network', 'configuration'}:
                code = 'LLM_TEMPORARILY_UNAVAILABLE' if interruption == 'network' else 'LLM_CONFIGURATION_REQUIRED'
                raise CandidateFailure(code, 'Unavailable.', True)
            if interruption == 'budget':
                now[0] = args[-1]
            else:
                expired = jobs.clock() + timedelta(seconds=120)
                jobs.clock = lambda: expired
        return review

    repair.review_cases = interrupted
    worker.input_repair_provider = repair
    assert worker.run_once().issue_code == {
        'network': 'LLM_TEMPORARILY_UNAVAILABLE', 'budget': 'BUILD_BUDGET_EXCEEDED',
        'lease': 'BUILDER_LEASE_EXPIRED', 'configuration': 'LLM_CONFIGURATION_REQUIRED'}[interruption]
    assert asset_status(path) == 'building'
    assert worker.run_once() is None  # No automatic replay of an uncertain call.
    if interruption == 'configuration':
        assert jobs.requeue_configuration_paused() == 1
    else:
        jobs.retry('problem-submit', 1)
    assert worker.run_once().status == 'completed'
    assert len(repair.calls) == 1 and provider.calls == 1
    assert len(repair.reviews) == 2
    assert asset_status(path) == 'ready'
    with sqlite3.connect(path) as db:
        receipts = db.execute('select status,review_json from verifier_case_repairs').fetchall()
    assert sorted(row[0] for row in receipts) == ['checked', 'generated']
    assert all(json.loads(row[1])['caseSetHash'] for row in receipts if row[1])


def test_review_for_different_selected_inputs_cannot_seal(environment):
    path, _, runner, _, worker = environment
    runner.generated = json.dumps({'cases': cases(True)[:3]}).encode()
    repair = Repair(runner)
    original = repair.review_cases

    def wrong_inputs(*args):
        return {**original(*args), 'caseSetHash': 'sha256:' + '0' * 64}

    repair.review_cases = wrong_inputs
    worker.input_repair_provider = repair
    assert worker.run_once().issue_code == 'GENERATOR_REPAIR_INVALID'
    assert asset_status(path) == 'building'


def test_previously_checked_review_cannot_cover_changed_selected_inputs(environment):
    path, jobs, runner, provider, worker = environment
    runner.generated = json.dumps({'cases': cases(True)[:3]}).encode()
    repair = Repair(runner)
    original = repair.review_cases
    reports = []

    def replay_old(*args):
        reports.append(original(*args))
        return reports[0]

    repair.review_cases = replay_old
    worker.input_repair_provider = repair
    runner.change = lambda role, req, _: result(reason='infrastructureError') if (
        role == 'slow' and req.operation == 'execute') else None
    assert worker.run_once().issue_code == 'VERIFIER_BUILDER_INTERNAL_ERROR'
    with sqlite3.connect(path) as db:
        assert db.execute('select status from verifier_case_repairs').fetchone()[0] == 'checked'
    changed = cases(True)
    changed[1]['stdin'] = '108\n'
    runner.generated = json.dumps({'cases': changed}).encode()
    runner.change = None
    jobs.retry('problem-submit', 1)
    assert worker.run_once().issue_code == 'GENERATOR_REPAIR_INVALID'
    assert len(repair.reviews) == 2 and len(repair.calls) == provider.calls == 1
    assert reports[0]['caseSetHash'] != reports[1]['caseSetHash']
    assert asset_status(path) == 'building'


@pytest.mark.parametrize('damage', ['missingReceipt', 'missingReviewer', 'coverage'])
def test_restored_repaired_generator_never_skips_review_or_repairs_again(environment, damage):
    from services.verifier_builder.candidates import CandidateFailure
    path, jobs, runner, provider, worker = environment
    runner.generated = json.dumps({'cases': cases(True)[:3]}).encode()
    repair = Repair(runner)
    original = repair.review_cases

    def unavailable(*args):
        original(*args)
        raise CandidateFailure('LLM_TEMPORARILY_UNAVAILABLE', 'Unavailable.', True)

    repair.review_cases = unavailable
    worker.input_repair_provider = repair
    assert worker.run_once().issue_code == 'LLM_TEMPORARILY_UNAVAILABLE'
    if damage == 'missingReceipt':
        with sqlite3.connect(path) as db:
            db.execute('delete from verifier_case_repairs')
    elif damage == 'missingReviewer':
        worker.input_repair_provider = None
    else:
        runner.generated = json.dumps({'cases': cases(True)[:3]}).encode()
    jobs.retry('problem-submit', 1)
    code = 'GENERATOR_COVERAGE_INSUFFICIENT' if damage == 'coverage' else 'GENERATOR_INPUT_LEGALITY_UNCONFIRMED'
    assert worker.run_once().issue_code == code
    assert len(repair.calls) == provider.calls == 1
    assert asset_status(path) == 'building'

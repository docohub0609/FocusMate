from dataclasses import replace
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from apps.api.problem_storage import ProblemRepository
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider

from services.verifier_builder.cross_check import CrossCheckFailure
from tests.python.test_verifier_builder_worker import environment as base_environment


@pytest.fixture
def environment(base_environment):
    # These v3 evidence tests retain the old repair-count contract.
    from apps.api.problem_intake import timestamp
    _,jobs,*_=base_environment
    with jobs._connect() as db:
        jobs._discover_one(db,timestamp(jobs.clock()))
        db.execute('UPDATE verifier_build_jobs SET repair_policy=0')
    return base_environment


def test_failed_mutation_precheck_preserves_final_candidates_and_private_audit(environment):
    path, jobs, runner, provider, worker = environment
    final = tuple(replace(m, source=m.source + '\n# final-repair') for m in provider.candidates.mutants)
    class Checker:
        latest_mutants = final
        mutation_audit = [{'originalSource': 'PRIVATE-AUDIT-SENTINEL'}]
        mutation_provider = SimpleNamespace(last_requests=[{'role': 'mutationGenerator', 'content': 'PRIVATE-RESPONSE'}])
        def precheck(self, *args):
            raise CrossCheckFailure('REFERENCE_DISAGREEMENT')
    worker.checker = Checker()
    outcome = worker.run_once()
    assert outcome.status == 'paused'
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT * FROM verifier_candidate_sets').fetchall()
    assert '# final-repair' in str(rows)
    audit_files = list(worker.workspace_root.glob('mutation-audit/*.json'))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text(encoding='utf-8'))
    assert audit['mutationAudit'] == Checker.mutation_audit
    assert audit['mutationRequests'] == Checker.mutation_provider.last_requests
    assert audit['originalCandidates']['mutants'][0]['source'] != audit['finalCandidates']['mutants'][0]['source']
    assert 'PRIVATE-AUDIT-SENTINEL' not in json.dumps(jobs.get('problem-submit', 1))


def test_v2_run_records_configured_budget(environment):
    _, jobs, _, provider, worker = environment
    claim = jobs.discover_and_claim('test', 60)
    worker.build_seconds = 300
    run = worker._run_record(claim, provider.candidates, None, worker.monotonic(), 'inconclusive')
    assert run['buildBudgetMs'] == 300000
    assert run['assurance'] == 'full'
    assert run['warningCodes'] == []
    assert run['mutationRepairCount'] == 0


@pytest.mark.parametrize('damage', [None, 'repair', 'warning', 'eligible', 'assurance'])
def test_limited_material_requires_precise_evidence(environment, damage):
    path, jobs, _, provider, worker = environment
    claim = jobs.discover_and_claim('test', 60)
    repository = ProblemRepository(path)
    record = repository.get('problem-submit', 1)
    features = repository.get_feature_set(record['lifecycle']['confirmedFeatureSetId'])
    checked = worker.checker.precheck(record, features, provider.candidates, time.monotonic() + 30)
    checked = replace(checked, assurance='limited', mutation_eligible=0, mutation_killed=0,
                      warning_codes=('MUTATION_CHALLENGE_UNAVAILABLE',), mutation_repair_count=1)
    if damage == 'repair': checked = replace(checked, mutation_repair_count=0)
    if damage == 'warning': checked = replace(checked, warning_codes=())
    if damage == 'eligible': checked = replace(checked, mutation_eligible=1, mutation_killed=0)
    if damage == 'assurance': checked = replace(checked, assurance='full')
    if damage:
        with pytest.raises(CrossCheckFailure):
            worker._validate_checked(claim, provider.candidates, checked)
    else:
        worker._validate_checked(claim, provider.candidates, checked)


def test_limited_success_seals_with_current_policy_budget_and_warning(environment):
    path, _, _, _, worker = environment
    original = worker.checker
    class LimitedChecker:
        latest_mutants = ()
        mutation_audit = [{'attempt': 1}]
        def precheck(self, *args):
            checked = original.precheck(*args)
            return replace(checked, mutation_attempted=0, mutation_eligible=0, mutation_killed=0,
                           assurance='limited', warning_codes=('MUTATION_CHALLENGE_UNAVAILABLE',),
                           mutation_repair_count=1)
    worker.checker = LimitedChecker()
    worker.build_seconds = 300
    outcome = worker.run_once()
    assert outcome.status == 'completed'
    SQLiteTrustedAssetProvider(path).get('problem-submit', 1)
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute('SELECT cross_check_report_json FROM verifier_assets').fetchone()[0])
    assert report['checkPolicyVersion'] == 'verifier-cross-check-v3'
    assert report['assurance'] == 'limited'
    assert report['buildBudgetMs'] == 300000
    assert report['warningCodes'] == ['MUTATION_CHALLENGE_UNAVAILABLE']

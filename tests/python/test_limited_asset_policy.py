import json
from pathlib import Path
import pytest
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.oj_worker.judge import JudgeFailure
from tests.python.test_final_oj import seeded, finish, service, request


def report(**updates):
    value = dict(reportId='r', outcome='passed', checkedMaterialHash='sha256:'+'a'*64,
        checksRun=['compile','generatorDeterminism','publicExamples','smallDifferential','largeDeterminism','mutation','sealedMaterialIntegrity'],
        createdAt='2026-09-26T00:00:00Z', checkPolicyVersion='verifier-cross-check-v2',
        generatorHash='sha256:'+'b'*64, slowOracleHash='sha256:'+'c'*64, fastReferenceHash='sha256:'+'d'*64,
        seed=1, smallCount=8, largeCount=0, agreementCount=8, mutationAttempted=0,mutationEligible=0,mutationKilled=0,
        elapsedMs=150000,buildBudgetMs=300000,assurance='limited',warningCodes=['MUTATION_CHALLENGE_UNAVAILABLE'],mutationRepairCount=1,safeFailureSummaries=[])
    return value | updates


def verify(value):
    SQLiteTrustedAssetProvider._verify_automatic_report(value,[{}]*8,[{'origin':'generatedCandidate','content_hash':'sha256:'+'d'*64}])


def test_limited_and_extended_budget_full_are_valid():
    verify(report())
    verify(report(assurance='full',warningCodes=[],mutationAttempted=1,mutationEligible=1,mutationKilled=1))


@pytest.mark.parametrize('change',[{'mutationRepairCount':0},{'warningCodes':[]},{'mutationEligible':1},{'mutationKilled':1},{'elapsedMs':300000},{'assurance':'full','warningCodes':[]}])
def test_forged_limited_cannot_bypass_evidence(change):
    with pytest.raises(JudgeFailure): verify(report(**change))


def test_v1_remains_strict():
    value=report(checkPolicyVersion='verifier-cross-check-v1',elapsedMs=1000,mutationAttempted=1,mutationEligible=1,mutationKilled=1)
    for key in ['assurance','warningCodes','mutationRepairCount','buildBudgetMs']: value.pop(key)
    verify(value)
    with pytest.raises(JudgeFailure): verify(value | {'elapsedMs':120000})


def test_limited_ac_does_not_complete_goal(tmp_path,monkeypatch):
    from dataclasses import replace
    path, _, sub, _ = seeded(tmp_path)
    finish(path,tmp_path)
    original=SQLiteTrustedAssetProvider.get_on_connection
    monkeypatch.setattr(SQLiteTrustedAssetProvider,'get_on_connection',lambda self,*args: replace(original(self,*args),assurance='limited'))
    result=service(path).verify(request(sub))
    assert result['status']=='requestFailed'
    assert result['reason']=='UNTRUSTED_ASSETS'

def test_completed_status_exposes_only_safe_assurance(tmp_path):
    import sqlite3
    from tests.python.test_verifier_builder_jobs import jobs, MutableClock
    path, _, _, _ = seeded(tmp_path)
    repo=jobs(path,MutableClock())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE problem_versions SET verifier_asset_status='missing'")
    claim=repo.discover_and_claim('builder',30)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE verifier_build_jobs SET status='completed',phase=NULL")
        db.execute('UPDATE verifier_assets SET cross_check_report_json=?',(json.dumps(report()),))
    value=repo.get('problem-submit',1)
    assert value['assurance']=='limited'
    assert value['warningCodes']==['MUTATION_CHALLENGE_UNAVAILABLE']
    assert 'generatorHash' not in value

@pytest.mark.parametrize('stored_field,value',[('assurance','full'),('warning_codes',[]),('mutation_repair_count',0)])
def test_persisted_precheck_must_match_report(stored_field,value):
    import hashlib
    sources={role:{'source':role} for role in ['generator','slow_oracle','fast_reference']}
    candidate=dict(problem_id='p',problem_version=1,problem_content_hash='h',feature_set_id='f',mutants=[],**sources)
    checked=dict(problem_id='p',problem_version=1,problem_content_hash='h',feature_set_id='f',assurance='limited',warning_codes=['MUTATION_CHALLENGE_UNAVAILABLE'],mutation_repair_count=1)
    value_report=report(**{field:'sha256:'+hashlib.sha256(sources[role]['source'].encode()).hexdigest() for field,role in [('generatorHash','generator'),('slowOracleHash','slow_oracle'),('fastReferenceHash','fast_reference')]})
    run={k:v for k,v in value_report.items() if k not in ['reportId','checksRun']}
    run.update(runId='r',schemaVersion='1.0.0',problemRef={'problemId':'p','problemVersion':1},featureSetId='f')
    evidence=dict(candidate_json=json.dumps(candidate),prechecked_json=json.dumps(checked),record_json=json.dumps(run))
    SQLiteTrustedAssetProvider._verify_automatic_evidence(value_report,evidence,'p',1,'h','f')
    checked[stored_field]=value
    evidence['prechecked_json']=json.dumps(checked)
    with pytest.raises(JudgeFailure):
        SQLiteTrustedAssetProvider._verify_automatic_evidence(value_report,evidence,'p',1,'h','f')

def test_limited_judge_result_keeps_warning_after_reload(tmp_path,monkeypatch):
    from dataclasses import replace
    from services.oj_worker.judge import OJResultRepository
    from src.focusmate.contracts.schema_validation import validate
    path, _, sub, _ = seeded(tmp_path)
    original=SQLiteTrustedAssetProvider.get
    monkeypatch.setattr(SQLiteTrustedAssetProvider,'get',lambda self,*args:replace(original(self,*args),assurance='limited'))
    finish(path,tmp_path)
    result=OJResultRepository(path).get_for_submission(sub['submissionId'])
    assert result['verdict']=='AC'
    assert result['assurance']=='limited'
    assert result['warningCodes']==['MUTATION_CHALLENGE_UNAVAILABLE']
    validate('OJResult',result)

def test_limited_result_cannot_complete_when_current_bundle_is_full(tmp_path,monkeypatch):
    from dataclasses import replace
    path, _, sub, _ = seeded(tmp_path)
    original=SQLiteTrustedAssetProvider.get
    monkeypatch.setattr(SQLiteTrustedAssetProvider,'get',lambda self,*args:replace(original(self,*args),assurance='limited'))
    finish(path,tmp_path)
    result=service(path).verify(request(sub))
    assert result['status']=='requestFailed'
    assert result['reason']=='UNTRUSTED_ASSETS'


def test_existing_oj_database_migrates_without_changing_old_results(tmp_path):
    import sqlite3
    from services.oj_worker.judge import OJResultRepository
    path, _, sub, _ = seeded(tmp_path)
    finish(path,tmp_path)
    before=OJResultRepository(path).get_for_submission(sub['submissionId'])
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE oj_results DROP COLUMN assurance_json')
    after=OJResultRepository(path).get_for_submission(sub['submissionId'])
    assert after==before
    assert 'assurance' not in after and 'warningCodes' not in after

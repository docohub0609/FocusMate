from copy import deepcopy
import sqlite3
import time

import pytest

from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.core.session_manager import SessionManager
from focusmate.contracts.manager import ManagerError
from tests.python.test_final_oj import seeded, request, finish, service


def setup(tmp_path):
    path, repo, sub, code = seeded(tmp_path)
    m = SessionManager(SQLiteManagerStore(tmp_path/'manager.sqlite3'))
    m.create('s1', {'goalId':'g1','scope':'goal'}, {'problemId':'problem-submit','problemVersion':1},
             contract_version=1, code_version=1, allowed_tools=[])
    return m, service(path), repo, sub


def test_manager_pause_wait_resume_and_zero_models(tmp_path):
    m, verifier, repo, sub = setup(tmp_path)
    receipt = m.start_final_verification('s1', request(sub), verifier)
    m.command('s1','pause1','pause',{})
    assert m.advance_final_verification('s1', verifier)['status'] == 'paused'
    assert verifier.report('g1','r1') is None
    m.command('s1','resume1','resume',{})
    assert m.advance_final_verification('s1', verifier)['status'] == 'waitingResult'
    finish(repo.path,tmp_path)
    result = m.advance_final_verification('s1', verifier)
    assert result['verifierResult']['status'] == 'passed'
    r = m.read('s1')['rounds'][receipt['roundId']]
    assert r['counts'] == {'model':0,'tool':0,'execution':0}
    assert m.read('s1')['outbox'] == []
    assert m.start_final_verification('s1',request(sub),verifier) == receipt


@pytest.mark.parametrize('change', ['code', 'message', 'expiry'])
def test_manager_stale_round_cannot_complete(tmp_path, change):
    m, verifier, repo, sub = setup(tmp_path)
    receipt = m.start_final_verification('s1', request(sub), verifier)
    m.advance_final_verification('s1', verifier)
    finish(repo.path, tmp_path)
    if change == 'code':
        repo.create_code_snapshot('g1','print(7)','python3')
        m.command('s1','c','code_changed',{'codeVersion':2})
    elif change == 'message': m.command('s1','q','message',{'text':'换个问题'})
    else: m.clock = lambda: time.time()+1000
    result = m.advance_final_verification('s1',verifier,round_id=receipt['roundId'])
    assert result['status'] in ('superseded','expired')
    assert verifier.goal_state('g1')['status'] == 'active'


def test_business_commit_survives_manager_ack_failure_and_recovers(tmp_path):
    m, verifier, repo, sub = setup(tmp_path)
    receipt = m.start_final_verification('s1',request(sub),verifier)
    finish(repo.path,tmp_path)
    with sqlite3.connect(m.store.path) as db:
        db.execute("CREATE TRIGGER break_ack BEFORE UPDATE ON manager_sessions BEGIN SELECT RAISE(ABORT,'ack failed'); END")
    with pytest.raises(Exception): m.advance_final_verification('s1',verifier)
    saved = verifier.report('g1','r1')
    assert saved['verifierResult']['status'] == 'passed'
    with sqlite3.connect(m.store.path) as db: db.execute('DROP TRIGGER break_ack')
    m.recover('s1')
    m.command('s1','pause','pause',{})
    m.clock = lambda: time.time()+1000
    assert m.advance_final_verification('s1',verifier) == saved
    assert m.read('s1')['status'] == 'paused'
    with sqlite3.connect(repo.path) as db:
        assert db.execute('SELECT COUNT(*) FROM final_verifier_results').fetchone()[0] == 1


def test_manager_cannot_substitute_goal_or_contract_or_database(tmp_path):
    m, verifier, _, sub = setup(tmp_path)
    with pytest.raises(ManagerError): m.start_final_verification('s1',{**request(sub),'goalId':'other'},verifier)
    with pytest.raises(ManagerError): m.start_final_verification('s1',{**request(sub),'goalContractVersion':2},verifier)
    m.start_final_verification('s1',request(sub),verifier)
    other = deepcopy(verifier)
    other.store.path = tmp_path/'other.db'
    with pytest.raises(ManagerError): m.advance_final_verification('s1',other)


def test_manager_cannot_adopt_other_owner_completed_request(tmp_path):
    from focusmate.contracts.final_oj import FinalOJError
    m, verifier, repo, sub = setup(tmp_path)
    finish(repo.path,tmp_path)
    verifier.verify(request(sub))
    m.start_final_verification('s1',request(sub),verifier)
    with pytest.raises(FinalOJError,match='REQUEST_CONFLICT'):
        m.advance_final_verification('s1',verifier)


def test_submission_must_match_manager_code_version(tmp_path):
    m, verifier, repo, sub = setup(tmp_path)
    repo.create_code_snapshot('g1','print(1)','python3')
    newer,_ = repo.create_submission({'goalContext':{'goalId':'g1','scope':'goal'},
        'problemRef':{'problemId':'problem-submit','problemVersion':1},'codeVersion':2,
        'language':'python3','idempotencyKey':'submit2'})
    m.start_final_verification('s1',request(newer),verifier)
    assert m.advance_final_verification('s1',verifier)['reason'] == 'CALLER_CONTEXT_MISMATCH'


@pytest.mark.parametrize('invalidate',[False,True])
def test_standard_manager_recovery_accepts_oj_wait_without_execution_lease(tmp_path,invalidate):
    m,verifier,repo,sub = setup(tmp_path)
    receipt = m.start_final_verification('s1',request(sub),verifier)
    if invalidate: m.command('s1','code2','code_changed',{'codeVersion':2})
    m.recover('s1')
    result = m.advance_final_verification('s1',verifier,round_id=receipt['roundId'])
    assert result['status'] == ('superseded' if invalidate else 'waitingResult')
    assert m.claim('s1','worker') is None

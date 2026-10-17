import json
import sqlite3

from apps.api.goal_contract_storage import GoalContractRepository
from tests.python.test_editor_workspace import editor
from tests.python.test_final_oj import seeded, finish, request, service


def test_new_workspace_contract_is_minimal_and_idempotent(tmp_path):
    repo = editor(tmp_path)
    goal = repo.workspace('problem-submit', 1, create=True)['goal']['goalId']
    contracts = GoalContractRepository(repo.path)
    assert hasattr(contracts, 'ensure_minimal'), 'A confirmed problem needs a contract without AI-generated stages'
    first = contracts.ensure_minimal(goal)
    assert first == contracts.ensure_minimal(goal)
    assert first['schemaVersion'] == '2.0.0'
    assert 'subgoals' not in first and 'navigationPolicy' not in first
    assert first['finalCriterion'] == {'verifierType': 'finalOj', 'requiredVerdict': 'AC'}
    assert contracts.get_current(goal)['compilation']['contract'] == first


def test_existing_frozen_contract_is_not_rewritten(tmp_path):
    path, _, _, _ = seeded(tmp_path)
    repo = GoalContractRepository(path)
    with sqlite3.connect(path) as db:
        before = db.execute('SELECT compilation_json FROM goal_contract_versions').fetchone()[0]
    assert hasattr(repo, 'ensure_minimal')
    assert repo.ensure_minimal('g1') == json.loads(before)['contract']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT compilation_json FROM goal_contract_versions').fetchone()[0] == before


def test_zero_subgoal_final_verification(tmp_path):
    path, _, sub, _ = seeded(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM goal_contract_versions')
    repo = GoalContractRepository(path)
    assert hasattr(repo, 'ensure_minimal')
    repo.ensure_minimal('g1')
    finish(path, tmp_path)
    result = service(path).verify(request(sub))
    assert result['verifierResult']['status'] == 'passed'
    state = service(path).goal_state('g1')
    assert state['status'] == 'completed' and state['subgoalStates'] == []


def test_minimal_contract_cannot_verify_a_changed_problem_hash(tmp_path):
    path, _, sub, _ = seeded(tmp_path)
    with sqlite3.connect(path) as db:db.execute('DELETE FROM goal_contract_versions')
    GoalContractRepository(path).ensure_minimal('g1')
    finish(path,tmp_path)
    with sqlite3.connect(path) as db:db.execute("UPDATE problem_versions SET content_hash=?",('0'*64,))
    result=service(path).verify(request(sub))
    assert result.get('verifierResult',{}).get('status')!='passed'
    assert 'PROBLEM_VERSION_CHANGED' in json.dumps(result)

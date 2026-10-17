"""Final acceptance fixtures are developer data, not production cross-check claims."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from apps.api.goal_contract_storage import _TABLES
from apps.api.submission_storage import SubmissionRepository
from focusmate.core.goal_template import build_generic_algorithm_goal_contract
from services.oj_worker.judge import JudgeSubmissionProcessor, OJResultRepository
from services.oj_worker.queue import WorkerQueue
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.oj_worker.worker import Worker
from tests.python.test_trusted_assets import build_expected_material, mark_ready, FakeJudgeRunner


def seeded(tmp_path):
    path = tmp_path / 'business.sqlite3'
    material, _ = build_expected_material(path)
    mark_ready(path, material)
    repo = SubmissionRepository(path, goal_id_factory=lambda: 'g1')
    goal = repo.create_goal({'problemId': 'problem-submit', 'problemVersion': 1})
    code = repo.create_code_snapshot('g1', 'print("1 4")\n', 'python3')
    contract = build_generic_algorithm_goal_contract(goal, created_at=goal['createdAt'])
    contract['contractStatus'] = 'frozen'
    with sqlite3.connect(path) as db:
        for sql in _TABLES: db.execute(sql)
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('g1', 1, 1, 'frozen', 'a'*64, json.dumps({'contract': contract}), '{}', None, None,
             goal['createdAt'], goal['createdAt'], goal['createdAt'], None))
    submission, _ = repo.create_submission({'goalContext': {'goalId': 'g1', 'scope': 'goal'},
        'problemRef': goal['problemRef'], 'codeVersion': 1, 'language': 'python3', 'idempotencyKey': 'submit1'})
    OJResultRepository(path)
    return path, repo, submission, code


def request(submission, key='r1'):
    return {'schemaVersion': '1.0.0', 'requestId': key, 'goalId': 'g1',
            'goalContractVersion': 1, 'submissionId': submission['submissionId']}


def finish(path, tmp_path, *, wrong=False, runner=None):
    assets = SQLiteTrustedAssetProvider(path)
    bundle = assets.get('problem-submit', 1)
    runner = runner or FakeJudgeRunner([b'wrong' if wrong else c.expected_output for c in bundle.cases])
    queue = WorkerQueue(path)
    worker = Worker(queue, JudgeSubmissionProcessor(runner, assets, OJResultRepository(path)),
                    'final-test', tmp_path / 'runs')
    return worker.process_once()


def service(path):
    from focusmate.adapters.final_oj import SQLiteFinalOJStore
    from focusmate.core.final_oj import GoalStateManager
    return GoalStateManager(SQLiteFinalOJStore(path))


def test_wait_then_ac_atomic_completion_and_no_process_passes(tmp_path):
    path, repo, submission, _ = seeded(tmp_path)
    manager = service(path)
    waiting = manager.verify(request(submission))
    assert waiting['status'] == 'waitingResult'
    assert 'verifierResult' not in waiting
    assert manager.goal_state('g1')['status'] == 'active'
    assert 'currentSubgoalId' not in manager.goal_state('g1')
    finish(path, tmp_path)
    report = manager.verify(request(submission))
    assert report['status'] == 'verified'
    assert report['verifierResult']['status'] == 'passed'
    state = manager.goal_state('g1')
    assert state['status'] == 'completed'
    assert state['lastVerifierResultId'] == report['verifierResult']['verifierResultId']
    assert all(s['status'] == 'notStarted' for s in state['subgoalStates'])
    assert repo.get_goal('g1')['status'] == 'active'  # legacy identity contract
    repo.create_code_snapshot('g1', 'print("new")', 'python3')
    assert manager.verify(request(submission)) == report
    assert manager.goal_state('g1') == state
    duplicate = manager.verify(request(submission, 'r2'))
    assert duplicate['status'] == 'alreadyCompleted'
    assert duplicate['completionResultId'] == state['lastVerifierResultId']


def test_non_ac_is_verifier_failed_not_request_failed(tmp_path):
    path, _, sub, _ = seeded(tmp_path)
    finish(path, tmp_path, wrong=True)
    m = service(path)
    result = m.verify(request(sub))
    assert result['status'] == 'verified'
    assert result['verifierResult']['status'] == 'failed'
    assert m.goal_state('g1')['status'] == 'active'


@pytest.mark.parametrize('damage,reason', [
    ("UPDATE code_snapshots SET source_code='tampered'", 'CODE_HASH_MISMATCH'),
    ("UPDATE private_test_cases SET expected_output_text='PRIVATE-SECRET'", 'UNTRUSTED_ASSETS'),
    ("UPDATE verifier_assets SET status='rejected'", 'UNTRUSTED_ASSETS'),
    ("UPDATE oj_results SET code_version=7", 'RESULT_REFERENCE_MISMATCH'),
    ("UPDATE oj_results SET test_summary_json='{\"passed\":0,\"total\":3,\"firstFailedCase\":null}'", 'RESULT_INCONSISTENT'),
    ("DELETE FROM raw_events WHERE event_name='ojResultProduced'", 'RESULT_RECEIPT_MISSING'),
    ("UPDATE submissions SET status='running'", 'RESULT_INCONSISTENT'),
])
def test_corruption_cannot_complete_or_leak(tmp_path, damage, reason):
    path, _, sub, _ = seeded(tmp_path)
    finish(path, tmp_path)
    with sqlite3.connect(path) as db: db.execute(damage)
    m = service(path)
    result = m.verify(request(sub))
    assert result['status'] == 'requestFailed'
    assert result['reason'] == reason
    assert 'PRIVATE-SECRET' not in json.dumps(result)
    assert not m.goal_state('g1') or m.goal_state('g1')['status'] != 'completed'


def test_changed_code_and_contract_block_waiting_request(tmp_path):
    path, repo, sub, _ = seeded(tmp_path)
    m = service(path)
    m.verify(request(sub))
    repo.create_code_snapshot('g1', 'print(2)', 'python3')
    finish(path, tmp_path)
    assert m.verify(request(sub))['reason'] == 'CODE_VERSION_CHANGED'
    with sqlite3.connect(path) as db: db.execute("UPDATE goal_contract_versions SET status='superseded'")
    assert m.verify(request(sub, 'new'))['reason'] == 'CONTRACT_VERSION_CHANGED'


def test_idempotency_conflict_and_source_binding(tmp_path):
    from focusmate.contracts.final_oj import FinalOJError
    path, _, sub, _ = seeded(tmp_path)
    m = service(path)
    original = request(sub)
    m.verify(original, owner={'kind': 'session', 'sessionId': 's1', 'roundId': 'r1'})
    with pytest.raises(FinalOJError, match='REQUEST_CONFLICT'):
        m.verify({**original, 'submissionId': 'another'}, owner={'kind': 'session', 'sessionId': 's1', 'roundId': 'r1'})
    with pytest.raises(FinalOJError, match='REQUEST_CONFLICT'): m.verify(original)


def test_invalid_payload_and_missing_source_never_complete(tmp_path):
    from focusmate.contracts.final_oj import FinalOJError
    path, _, sub, _ = seeded(tmp_path)
    m = service(path)
    with pytest.raises(FinalOJError): m.verify({**request(sub), 'verdict': 'AC'})
    assert m.verify({**request(sub), 'submissionId': 'missing'})['status'] == 'requestFailed'
    with sqlite3.connect(path) as db: db.execute("UPDATE submissions SET status='expired'")
    assert m.verify(request(sub, 'r2'))['reason'] == 'SUBMISSION_UNAVAILABLE'


def test_final_commit_failure_rolls_back_result_state_history_and_request(tmp_path):
    path, _, sub, _ = seeded(tmp_path)
    finish(path, tmp_path)
    m = service(path)
    with sqlite3.connect(path) as db:
        # Fail the final write, after the result, state and history were inserted.
        db.execute("CREATE TRIGGER break_final BEFORE INSERT ON final_oj_requests BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(Exception): m.verify(request(sub))
    assert m.goal_state('g1') is None
    assert m.report('g1', 'r1') is None
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM final_verifier_results').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM goal_state_history').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM oj_results').fetchone()[0] == 1
        db.execute('DROP TRIGGER break_final')
    assert m.verify(request(sub))['verifierResult']['status'] == 'passed'


def test_parallel_requests_complete_once(tmp_path):
    path, _, sub, _ = seeded(tmp_path)
    finish(path, tmp_path)
    m = service(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: m.verify(request(sub, 'r'+str(n))), range(2)))
    assert sorted(r['status'] for r in results) == ['alreadyCompleted', 'verified']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM final_verifier_results').fetchone()[0] == 1


def test_provider_can_validate_in_callers_transaction(tmp_path):
    path, _, _, _ = seeded(tmp_path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        bundle = SQLiteTrustedAssetProvider(path).get_on_connection(db, 'problem-submit', 1)
        assert bundle.status == 'ready'


def test_new_contract_request_keeps_old_history_and_does_not_copy_verified_nodes(tmp_path):
    path,_,sub,_ = seeded(tmp_path)
    m = service(path)
    m.verify(request(sub))
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT * FROM goal_contract_versions').fetchone()
        content = json.loads(row[5]); content['contract']['version'] = 2
        db.execute("UPDATE goal_contract_versions SET status='superseded'")
        values = list(row); values[1] = 2; values[4] = 'b'*64; values[5] = json.dumps(content)
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
    assert m.verify(request(sub))['status'] == 'superseded'
    assert m.verify({**request(sub,'r2'),'goalContractVersion':2})['status'] == 'waitingResult'
    assert m.goal_state('g1')['goalContractVersion'] == 2
    with sqlite3.connect(path) as db:
        history = db.execute('SELECT state_json FROM goal_state_history ORDER BY version').fetchall()
    assert [json.loads(r[0])['goalContractVersion'] for r in history] == [1,2]


def test_current_contract_edit_and_cross_goal_result_rejected(tmp_path):
    path,_,sub,_ = seeded(tmp_path)
    m = service(path)
    m.verify(request(sub))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE goal_contract_versions SET content_hash=?",('b'*64,))
    assert m.verify(request(sub))['reason'] == 'PINNED_MATERIAL_CHANGED'
    finish(path,tmp_path)
    with sqlite3.connect(path) as db: db.execute("UPDATE oj_results SET goal_id='other'")
    assert m.verify(request(sub,'r2'))['reason'] == 'RESULT_REFERENCE_MISMATCH'


def test_paused_legacy_state_not_implicitly_resumed(tmp_path):
    path,_,sub,_ = seeded(tmp_path)
    m = service(path); m.verify(request(sub))
    with sqlite3.connect(path) as db:
        state = json.loads(db.execute('SELECT state_json FROM goal_states').fetchone()[0])
        state['status'] = 'paused'
        db.execute('UPDATE goal_states SET state_json=?',(json.dumps(state),))
    finish(path,tmp_path)
    assert m.verify(request(sub))['reason'] == 'GOAL_NOT_ACTIVE'


def test_actual_table_missing_returns_safe_error(tmp_path):
    path,_,sub,_ = seeded(tmp_path)
    m = service(path)
    with sqlite3.connect(path) as db: db.execute('DROP TABLE oj_results')
    from focusmate.contracts.final_oj import FinalOJError
    with pytest.raises(FinalOJError,match='FINAL_STORAGE_UNAVAILABLE'): m.verify(request(sub))
    assert m.goal_state('g1') is None


def test_frozen_contract_for_other_problem_cannot_complete(tmp_path):
    path, _, sub, _ = seeded(tmp_path)
    finish(path, tmp_path)
    with sqlite3.connect(path) as db:
        content = json.loads(db.execute('SELECT compilation_json FROM goal_contract_versions').fetchone()[0])
        content['contract']['problemRef']['problemVersion'] = 2
        db.execute('UPDATE goal_contract_versions SET compilation_json=?', (json.dumps(content),))
    m = service(path)
    assert m.verify(request(sub))['reason'] == 'CONTRACT_INVALID'
    assert m.goal_state('g1') is None

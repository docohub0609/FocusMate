from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest
from apps.api import submission_storage
from tests.python.test_submission_storage import frozen_problem, prepared_repository, submission_request


def editor(tmp_path):
    from apps.api.editor_storage import EditorRepository
    path = tmp_path / 'editor.db'
    frozen_problem(path)
    return EditorRepository(path)


def test_workspace_is_atomic_and_recoverable(tmp_path):
    repo = editor(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: repo.workspace('problem-submit', 1, create=True), range(2)))
    assert results[0] == results[1]
    assert results[0]['draft']['sourceCode'] == ''
    with sqlite3.connect(repo.path) as db:
        assert db.execute('select count(*) from goal_records').fetchone()[0] == 1


def test_draft_idempotency_conflict_empty_and_immutable_snapshot(tmp_path):
    repo = editor(tmp_path)
    goal = repo.workspace('problem-submit', 1, create=True)['goal']['goalId']
    request = dict(sourceCode='for x in', expectedRevision=0, operationId='save1')
    first = repo.save_draft(goal, request)
    assert first['revision'] == 1 and first['codeVersion'] == 1
    assert repo.save_draft(goal, request) == first
    with pytest.raises(submission_storage.SubmissionFailure):
        repo.save_draft(goal, dict(request, sourceCode='different'))
    with pytest.raises(submission_storage.SubmissionFailure):
        repo.save_draft(goal, dict(request, operationId='other'))
    empty = repo.save_draft(goal, dict(sourceCode='', expectedRevision=1, operationId='clear'))
    assert empty['sourceCode'] == '' and empty['codeVersion'] is None
    assert submission_storage.SubmissionRepository(repo.path).get_code_snapshot(goal, 1)['sourceCode'] == 'for x in'
    assert repo.draft(goal) == empty


def test_snapshot_and_event_rollback_together(tmp_path, monkeypatch):
    repo = editor(tmp_path)
    goal = repo.workspace('problem-submit', 1, create=True)['goal']['goalId']
    from src.focusmate.adapters.raw_events import SQLiteEventStore
    def fail(*args, **kwargs):
        raise RuntimeError('event failure')
    monkeypatch.setattr(SQLiteEventStore, 'append_on_connection', fail)
    with pytest.raises(RuntimeError):
        repo.save_draft(goal, dict(sourceCode='print(1)', expectedRevision=0, operationId='save'))
    assert repo.draft(goal)['revision'] == 0
    with sqlite3.connect(repo.path) as db:
        assert db.execute('select count(*) from code_snapshots').fetchone()[0] == 0


def test_submission_same_key_different_snapshot_is_rejected(tmp_path):
    repo = prepared_repository(tmp_path)
    repo.create_submission(submission_request())
    repo.create_code_snapshot('goal-submit', 'print(2)', 'python3')
    with pytest.raises(submission_storage.SubmissionFailure, match='different'):
        repo.create_submission(submission_request(code_version=2))


def test_same_submission_key_with_missing_snapshot_is_still_conflict(tmp_path):
    repo = prepared_repository(tmp_path)
    repo.create_submission(submission_request())
    with pytest.raises(submission_storage.SubmissionFailure) as failure:
        repo.create_submission(submission_request(code_version=999))
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'

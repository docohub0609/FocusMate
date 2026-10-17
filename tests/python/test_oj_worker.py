from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path

from apps.api.submission_storage import SubmissionRepository
from services.oj_worker.queue import WorkerQueue
from services.oj_worker.worker import ProcessorTimeout, Worker
from tests.python.test_submission_storage import prepared_repository, submission_request


class RecordingProcessor:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome
        self.calls: list[tuple[object, Path, str]] = []

    def process(self, item: object, workspace: Path) -> None:
        self.calls.append((item, workspace, (workspace / "main.py").read_text(encoding="utf-8")))
        if self.outcome == "error":
            raise RuntimeError("fake runner failure")
        if self.outcome == "timeout":
            raise ProcessorTimeout("fake timeout")


def queued_repository(tmp_path: Path) -> SubmissionRepository:
    repo = prepared_repository(tmp_path, "ready")
    repo.create_submission(submission_request())
    return repo


def test_failure_diagnostic_survives_worker_restart_without_exception_text(tmp_path):
    from services.oj_worker.diagnostics import get_failure
    repo = queued_repository(tmp_path)
    worker = Worker(WorkerQueue(repo.path), RecordingProcessor('error'), 'worker-a', tmp_path/'workspaces')
    worker.process_once()
    failure = get_failure(repo.path, 'submission-submit')
    assert failure['stage'] == 'execute' and failure['errorType'] == 'RuntimeError'
    assert failure['goalId'] == 'goal-submit' and failure['codeVersion'] == 1
    assert 'fake runner failure' not in str(failure)
    assert Worker(WorkerQueue(repo.path), RecordingProcessor(), 'worker-b', tmp_path/'workspaces').process_once() is None


def test_expired_claim_cannot_write_failure_diagnostic(tmp_path):
    import pytest
    from services.oj_worker.queue import QueueFailure
    from services.oj_worker.diagnostics import get_failure
    repo = queued_repository(tmp_path)
    queue = WorkerQueue(repo.path)
    claim = queue.claim_next('old-worker')
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE submission_claims SET lease_expires_at='2000-01-01T00:00:00Z'")
    with pytest.raises(QueueFailure):
        queue.transition_owned(claim, 'preparing','failed',failure={'stage':'load'})
    assert get_failure(repo.path,claim.submission_id) is None


def test_two_workers_cannot_claim_the_same_submission(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)

    def claim(worker_id: str):
        return WorkerQueue(repo.path).claim_next(worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["worker-a", "worker-b"]))
    assert sum(claim is not None for claim in claims) == 1
    assert repo.get_submission("submission-submit")["status"] == "preparing"


def test_queue_is_fifo_and_does_not_claim_awaiting_assets(tmp_path: Path) -> None:
    waiting = prepared_repository(tmp_path / "waiting", "missing")
    waiting.create_submission(submission_request())
    assert WorkerQueue(waiting.path).claim_next("worker") is None

    repo = queued_repository(tmp_path / "fifo")
    repo.submission_id_factory = lambda: "submission-second"
    repo.create_code_snapshot("goal-submit", "print('second')\n", "python3")
    repo.create_submission(submission_request(code_version=2, key="click-2"))
    claim = WorkerQueue(repo.path).claim_next("worker")
    assert claim is not None
    assert claim.submission_id == "submission-submit"


def test_fake_success_runs_full_lifecycle_and_cleans_workspace(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    runner = RecordingProcessor()
    root = tmp_path / "workspaces"
    worker = Worker(WorkerQueue(repo.path), runner, "worker-a", root)

    assert worker.process_once() == "submission-submit"
    assert repo.get_submission("submission-submit")["status"] == "finished"
    assert runner.calls[0][2] == "print(input())\n"
    assert not runner.calls[0][1].exists()
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM submission_claims").fetchone()[0] == 0
        assert "oj_results" not in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_fake_error_and_timeout_reach_safe_terminal_states(tmp_path: Path) -> None:
    for outcome, expected in [("error", "failed"), ("timeout", "expired")]:
        case = tmp_path / outcome
        repo = queued_repository(case)
        runner = RecordingProcessor(outcome)
        worker = Worker(WorkerQueue(repo.path), runner, f"worker-{outcome}", case / "workspaces")
        worker.process_once()
        assert repo.get_submission("submission-submit")["status"] == expected
        assert not runner.calls[0][1].exists()


def test_hash_mismatch_fails_without_calling_runner(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE code_snapshots SET content_hash='tampered' WHERE goal_id='goal-submit'"
        )
        db.commit()
    runner = RecordingProcessor()
    Worker(WorkerQueue(repo.path), runner, "worker-a", tmp_path / "workspaces").process_once()
    assert repo.get_submission("submission-submit")["status"] == "failed"
    assert runner.calls == []


def test_missing_snapshot_fails_without_calling_runner(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    with sqlite3.connect(repo.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM code_snapshots WHERE goal_id='goal-submit'")
        db.commit()
    runner = RecordingProcessor()
    Worker(WorkerQueue(repo.path), runner, "worker-a", tmp_path / "workspaces").process_once()
    assert repo.get_submission("submission-submit")["status"] == "failed"
    assert runner.calls == []


def test_goal_problem_ownership_mismatch_fails_without_runner(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    with sqlite3.connect(repo.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "UPDATE goal_records SET problem_id='another-problem' WHERE goal_id='goal-submit'"
        )
        db.commit()
    runner = RecordingProcessor()
    Worker(WorkerQueue(repo.path), runner, "worker-a", tmp_path / "workspaces").process_once()
    assert repo.get_submission("submission-submit")["status"] == "failed"
    assert runner.calls == []


def test_stale_lease_expires_submission_and_cleans_known_workspace(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)]
    clock = lambda: current[0]
    repo = queued_repository(tmp_path)
    queue = WorkerQueue(repo.path, clock=clock)
    claim = queue.claim_next("crashed-worker", lease_seconds=60)
    assert claim is not None
    root = tmp_path / "workspaces"
    orphan = root / f"focusmate-{claim.submission_id}-orphan"
    orphan.mkdir(parents=True)
    (orphan / "main.py").write_text("untrusted", encoding="utf-8")
    queue.set_workspace(claim, orphan)

    current[0] += timedelta(seconds=61)
    worker = Worker(queue, RecordingProcessor(), "recovery-worker", root)
    assert worker.recover_expired() == 1
    assert repo.get_submission("submission-submit")["status"] == "expired"
    assert not orphan.exists()


def test_claim_survives_repository_restart(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    first = WorkerQueue(repo.path)
    claim = first.claim_next("worker-a")
    assert claim is not None
    reopened = WorkerQueue(repo.path)
    with sqlite3.connect(repo.path) as db:
        row = db.execute(
            "SELECT worker_id FROM submission_claims WHERE submission_id=?",
            (claim.submission_id,),
        ).fetchone()
    assert row == ("worker-a",)
    assert reopened.claim_next("worker-b") is None

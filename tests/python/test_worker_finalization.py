from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Event

import pytest

from services.oj_worker.judge import (
    JudgeOutcome,
    OJResultRepository,
    TrustedTestBundle,
    TrustedTestCase,
)
from services.oj_worker.queue import QueueFailure, WorkItem, WorkerQueue
from services.oj_worker.worker import Worker
from tests.python.test_submission_storage import prepared_repository, submission_request


def queued_repository(tmp_path: Path):
    repo = prepared_repository(tmp_path, "ready")
    repo.create_submission(submission_request())
    return repo


def work_item() -> WorkItem:
    return WorkItem(
        "submission-submit", "goal-submit", "problem-submit", 1, 1,
        "python3", "python-default-v1", "print(input())\n", "unused",
    )


def trusted_bundle() -> TrustedTestBundle:
    return TrustedTestBundle(
        "asset-test", 1, "problem-submit", 1, "ready",
        (TrustedTestCase("private-1", b"hello\n", b"hello\n"),),
    )


def accepted_outcome() -> JudgeOutcome:
    return JudgeOutcome("AC", 1, None, 5, 1024, None)


def test_existing_pre_token_claim_table_is_migrated_in_place(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    with sqlite3.connect(repo.path) as db:
        db.execute(
            """CREATE TABLE submission_claims (
                submission_id TEXT PRIMARY KEY,
                worker_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                workspace_path TEXT,
                FOREIGN KEY(submission_id) REFERENCES submissions(submission_id)
            )"""
        )
        db.execute(
            "UPDATE submissions SET status='preparing' WHERE submission_id='submission-submit'"
        )
        db.execute(
            """INSERT INTO submission_claims
               VALUES('submission-submit','legacy-worker',
                      '2026-09-06T08:00:00Z','2026-09-06T08:01:00Z',NULL)"""
        )

    WorkerQueue(repo.path, token_factory=lambda: "migrated-token")
    with sqlite3.connect(repo.path) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(submission_claims)")
        }
        token = db.execute(
            "SELECT claim_token FROM submission_claims WHERE submission_id='submission-submit'"
        ).fetchone()[0]
    assert "claim_token" in columns
    assert token == "migrated-token"


def test_live_claim_can_renew_but_expired_claim_cannot_revive(tmp_path: Path) -> None:
    current = [datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)]
    repo = queued_repository(tmp_path)
    queue = WorkerQueue(
        repo.path, clock=lambda: current[0], token_factory=lambda: "claim-generation-1",
    )
    claim = queue.claim_next("worker-a", lease_seconds=60)
    assert claim is not None
    assert claim.claim_token == "claim-generation-1"

    current[0] += timedelta(seconds=50)
    renewed = queue.renew(claim, lease_seconds=60)
    assert renewed.lease_expires_at != claim.lease_expires_at

    # The original in-memory Claim remains usable because ownership is the token;
    # the authoritative lease expiry lives in SQLite.
    current[0] += timedelta(seconds=20)
    queue.transition_owned(claim, "preparing", "running")

    current[0] += timedelta(seconds=41)
    with pytest.raises(QueueFailure, match="expired"):
        queue.renew(claim, lease_seconds=60)


def test_old_claim_generation_cannot_save_after_a_new_claim_exists(tmp_path: Path) -> None:
    current = [datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)]
    tokens = iter(["old-token", "new-token"])
    repo = queued_repository(tmp_path)
    queue = WorkerQueue(repo.path, clock=lambda: current[0], token_factory=lambda: next(tokens))
    results = OJResultRepository(repo.path)

    old_claim = queue.claim_next("same-worker-id", lease_seconds=60)
    assert old_claim is not None
    queue.transition_owned(old_claim, "preparing", "running")
    current[0] += timedelta(seconds=61)
    assert len(queue.expire_stale()) == 1

    # T021 does not retry expired work automatically. This direct state change
    # models a future/manual retry solely to prove claim generations are fenced.
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE submissions SET status='queued' WHERE submission_id='submission-submit'"
        )
    new_claim = queue.claim_next("same-worker-id", lease_seconds=60)
    assert new_claim is not None
    assert new_claim.claim_token != old_claim.claim_token

    with pytest.raises(QueueFailure, match="no longer owns"):
        queue.finalize_owned(
            old_claim,
            lambda db: results.save(
                work_item(), trusted_bundle(), accepted_outcome(), connection=db,
            ),
        )

    assert results.get_for_submission("submission-submit") is None
    assert repo.get_submission("submission-submit")["status"] == "preparing"
    queue.release(old_claim)
    with sqlite3.connect(repo.path) as db:
        token = db.execute(
            "SELECT claim_token FROM submission_claims WHERE submission_id='submission-submit'"
        ).fetchone()[0]
    assert token == "new-token"


def test_result_insert_rolls_back_if_finalization_crashes_before_commit(tmp_path: Path) -> None:
    repo = queued_repository(tmp_path)
    queue = WorkerQueue(repo.path, token_factory=lambda: "claim-token")
    results = OJResultRepository(repo.path, id_factory=lambda: "oj-result-atomic")
    claim = queue.claim_next("worker-a")
    assert claim is not None
    queue.transition_owned(claim, "preparing", "running")

    class SimulatedCrash(Exception):
        pass

    def write_then_crash(db: sqlite3.Connection):
        results.save(
            work_item(), trusted_bundle(), accepted_outcome(), connection=db,
        )
        raise SimulatedCrash("process stopped before commit")

    with pytest.raises(SimulatedCrash):
        queue.finalize_owned(claim, write_then_crash)

    # All three changes share one transaction: none can be half-committed.
    assert results.get_for_submission("submission-submit") is None
    assert repo.get_submission("submission-submit")["status"] == "running"
    with sqlite3.connect(repo.path) as db:
        assert db.execute(
            "SELECT claim_token FROM submission_claims WHERE submission_id='submission-submit'"
        ).fetchone() == ("claim-token",)


def test_owned_finalization_commits_result_finished_and_claim_removal_together(
    tmp_path: Path,
) -> None:
    repo = queued_repository(tmp_path)
    queue = WorkerQueue(repo.path, token_factory=lambda: "claim-token")
    results = OJResultRepository(repo.path, id_factory=lambda: "oj-result-atomic")
    claim = queue.claim_next("worker-a")
    assert claim is not None
    queue.transition_owned(claim, "preparing", "running")

    saved = queue.finalize_owned(
        claim,
        lambda db: results.save(
            work_item(), trusted_bundle(), accepted_outcome(), connection=db,
        ),
    )

    assert saved["ojResultId"] == "oj-result-atomic"
    assert results.get_for_submission("submission-submit")["verdict"] == "AC"
    assert repo.get_submission("submission-submit")["status"] == "finished"
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM submission_claims").fetchone()[0] == 0


def test_generic_worker_renews_lease_during_blocking_processor(tmp_path: Path) -> None:
    current = [datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)]
    renewed = Event()
    repo = queued_repository(tmp_path)

    class SignalingQueue(WorkerQueue):
        def renew(self, claim, *, lease_seconds=60):
            result = super().renew(claim, lease_seconds=lease_seconds)
            renewed.set()
            return result

    class SlowProcessor:
        def process(self, item: WorkItem, workspace: Path) -> None:
            # Move close to the original deadline, wait for the real heartbeat,
            # then finish after that original deadline but within the renewed one.
            current[0] += timedelta(milliseconds=800)
            assert renewed.wait(timeout=2)
            current[0] += timedelta(milliseconds=400)

    worker = Worker(
        SignalingQueue(repo.path, clock=lambda: current[0]),
        SlowProcessor(), "worker-a", tmp_path / "workspaces",
        lease_seconds=1,
    )
    assert worker.process_once() == "submission-submit"
    assert repo.get_submission("submission-submit")["status"] == "finished"
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM submission_claims").fetchone()[0] == 0

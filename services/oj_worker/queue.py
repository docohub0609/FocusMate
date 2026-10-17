"""SQLite-backed submission claiming for the independent OJ worker."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import json
from .diagnostics import initialize
from typing import Callable, TypeVar
from uuid import uuid4


Clock = Callable[[], datetime]
T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Claim:
    submission_id: str
    worker_id: str
    claim_token: str
    claimed_at: str
    lease_expires_at: str
    workspace_path: str | None = None


@dataclass(frozen=True)
class WorkItem:
    submission_id: str
    goal_id: str
    problem_id: str
    problem_version: int
    code_version: int
    language: str
    resource_policy_ref: str
    source_code: str
    content_hash: str


class QueueFailure(Exception):
    """A safe worker-side queue or reference failure."""


class WorkerQueue:
    """Claim submissions without depending on API or core orchestration code."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock = utc_now,
        token_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.token_factory = token_factory
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            initialize(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS submission_claims (
                    submission_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    workspace_path TEXT,
                    FOREIGN KEY(submission_id) REFERENCES submissions(submission_id)
                )"""
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(submission_claims)")
            }
            if "claim_token" not in columns:
                # Existing T021 databases predate claim generations. Give every
                # surviving claim its own token before enforcing token uniqueness.
                db.execute("ALTER TABLE submission_claims ADD COLUMN claim_token TEXT")
                rows = db.execute(
                    "SELECT submission_id FROM submission_claims WHERE claim_token IS NULL"
                ).fetchall()
                for row in rows:
                    db.execute(
                        "UPDATE submission_claims SET claim_token=? WHERE submission_id=?",
                        (self.token_factory(), row["submission_id"]),
                    )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_submission_claim_token
                   ON submission_claims(claim_token)
                   WHERE claim_token IS NOT NULL"""
            )

    def claim_next(self, worker_id: str, *, lease_seconds: int = 60) -> Claim | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        now = self.clock()
        claimed_at = timestamp(now)
        lease_expires_at = timestamp(now + timedelta(seconds=lease_seconds))
        claim_token = self.token_factory()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    """SELECT submission_id FROM submissions
                       WHERE status='queued'
                       ORDER BY created_at, submission_id LIMIT 1"""
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                submission_id = row["submission_id"]
                changed = db.execute(
                    "UPDATE submissions SET status='preparing' WHERE submission_id=? AND status='queued'",
                    (submission_id,),
                ).rowcount
                if changed != 1:
                    db.rollback()
                    return None
                db.execute(
                    """INSERT INTO submission_claims(
                           submission_id,worker_id,claim_token,claimed_at,
                           lease_expires_at,workspace_path
                       ) VALUES(?,?,?,?,?,NULL)""",
                    (
                        submission_id, worker_id, claim_token, claimed_at,
                        lease_expires_at,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return Claim(
            submission_id, worker_id, claim_token, claimed_at, lease_expires_at,
        )

    def load_work_item(self, claim: Claim) -> WorkItem:
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT s.submission_id,s.goal_id,s.problem_id,s.problem_version,
                          s.code_version,s.language,s.resource_policy_ref,
                          c.source_code,c.content_hash,c.goal_id AS code_goal_id,
                          g.problem_id AS goal_problem_id,g.problem_version AS goal_problem_version,
                          p.lifecycle_status
                   FROM submissions s
                   JOIN submission_claims q ON q.submission_id=s.submission_id
                   JOIN goal_records g ON g.goal_id=s.goal_id
                   JOIN code_snapshots c
                     ON c.goal_id=s.goal_id AND c.code_version=s.code_version
                   JOIN problem_versions p
                     ON p.problem_id=s.problem_id AND p.version=s.problem_version
                   WHERE s.submission_id=? AND q.worker_id=? AND q.claim_token=?
                     AND julianday(q.lease_expires_at)>julianday(?)
                     AND s.status='preparing'""",
                (
                    claim.submission_id, claim.worker_id, claim.claim_token,
                    timestamp(self.clock()),
                ),
            ).fetchone()
        if row is None:
            raise QueueFailure("The claimed submission or one of its references is unavailable.")
        if row["code_goal_id"] != row["goal_id"]:
            raise QueueFailure("The code snapshot belongs to another goal.")
        if (
            row["goal_problem_id"] != row["problem_id"]
            or row["goal_problem_version"] != row["problem_version"]
        ):
            raise QueueFailure("The goal belongs to another problem version.")
        if row["lifecycle_status"] != "frozen":
            raise QueueFailure("The problem version is no longer frozen.")
        return WorkItem(
            submission_id=row["submission_id"], goal_id=row["goal_id"],
            problem_id=row["problem_id"], problem_version=row["problem_version"],
            code_version=row["code_version"], language=row["language"],
            resource_policy_ref=row["resource_policy_ref"], source_code=row["source_code"],
            content_hash=row["content_hash"],
        )

    def set_workspace(self, claim: Claim, workspace_path: Path) -> None:
        with closing(self._connect()) as db, db:
            changed = db.execute(
                """UPDATE submission_claims SET workspace_path=?
                   WHERE submission_id=? AND worker_id=? AND claim_token=?
                     AND julianday(lease_expires_at)>julianday(?)""",
                (
                    str(workspace_path), claim.submission_id, claim.worker_id,
                    claim.claim_token, timestamp(self.clock()),
                ),
            ).rowcount
        if changed != 1:
            raise QueueFailure("The worker no longer owns this submission.")

    def transition_owned(self, claim: Claim, source: str, target: str, *, failure: dict | None = None) -> None:
        with closing(self._connect()) as db, db:
            owned = db.execute(
                """SELECT 1 FROM submission_claims
                   WHERE submission_id=? AND worker_id=? AND claim_token=?
                     AND julianday(lease_expires_at)>julianday(?)""",
                (
                    claim.submission_id, claim.worker_id, claim.claim_token,
                    timestamp(self.clock()),
                ),
            ).fetchone()
            if owned is None:
                raise QueueFailure("The worker no longer owns this submission.")
            changed = db.execute(
                "UPDATE submissions SET status=? WHERE submission_id=? AND status=?",
                (target, claim.submission_id, source),
            ).rowcount
            if changed == 1 and failure is not None:
                context = db.execute('SELECT goal_id,problem_id,problem_version,code_version FROM submissions WHERE submission_id=?', (claim.submission_id,)).fetchone()
                record = {**failure, 'submissionId': claim.submission_id, 'goalId': context['goal_id'],
                          'problemId': context['problem_id'], 'problemVersion': context['problem_version'],
                          'codeVersion': context['code_version'], 'recordedAt': timestamp(self.clock())}
                db.execute('INSERT INTO submission_failures VALUES (?,?)', (claim.submission_id, json.dumps(record)))
        if changed != 1:
            raise QueueFailure(f"Submission cannot move from {source} to {target}.")

    def renew(self, claim: Claim, *, lease_seconds: int = 60) -> Claim:
        """Extend a live claim without allowing an expired generation to revive."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        now_value = self.clock()
        renewed_until = timestamp(now_value + timedelta(seconds=lease_seconds))
        now = timestamp(now_value)
        with closing(self._connect()) as db, db:
            changed = db.execute(
                """UPDATE submission_claims SET lease_expires_at=?
                   WHERE submission_id=? AND worker_id=? AND claim_token=?
                     AND julianday(lease_expires_at)>julianday(?)
                     AND EXISTS (
                         SELECT 1 FROM submissions s
                         WHERE s.submission_id=submission_claims.submission_id
                           AND s.status IN ('preparing','running')
                     )""",
                (
                    renewed_until, claim.submission_id, claim.worker_id,
                    claim.claim_token, now,
                ),
            ).rowcount
        if changed != 1:
            raise QueueFailure("The worker claim is missing or has expired.")
        return Claim(
            claim.submission_id, claim.worker_id, claim.claim_token,
            claim.claimed_at, renewed_until, claim.workspace_path,
        )

    def finalize_owned(
        self,
        claim: Claim,
        write_result: Callable[[sqlite3.Connection], T],
    ) -> T:
        """Atomically write a result, finish the Submission, and release ownership."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                owned = db.execute(
                    """SELECT 1 FROM submission_claims q
                       JOIN submissions s ON s.submission_id=q.submission_id
                       WHERE q.submission_id=? AND q.worker_id=? AND q.claim_token=?
                         AND julianday(q.lease_expires_at)>julianday(?)
                         AND s.status='running'""",
                    (
                        claim.submission_id, claim.worker_id, claim.claim_token,
                        timestamp(self.clock()),
                    ),
                ).fetchone()
                if owned is None:
                    raise QueueFailure(
                        "The worker no longer owns a live running submission."
                    )
                result = write_result(db)
                changed = db.execute(
                    """UPDATE submissions SET status='finished'
                       WHERE submission_id=? AND status='running'""",
                    (claim.submission_id,),
                ).rowcount
                if changed != 1:
                    raise QueueFailure("The running submission could not be finalized.")
                deleted = db.execute(
                    """DELETE FROM submission_claims
                       WHERE submission_id=? AND worker_id=? AND claim_token=?""",
                    (claim.submission_id, claim.worker_id, claim.claim_token),
                ).rowcount
                if deleted != 1:
                    raise QueueFailure("The worker claim changed during finalization.")
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def release(self, claim: Claim) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """DELETE FROM submission_claims
                   WHERE submission_id=? AND worker_id=? AND claim_token=?""",
                (claim.submission_id, claim.worker_id, claim.claim_token),
            )

    def expire_stale(self) -> list[Claim]:
        now = timestamp(self.clock())
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    """SELECT * FROM submission_claims
                       WHERE julianday(lease_expires_at)<=julianday(?)""",
                    (now,),
                ).fetchall()
                claims = [Claim(**dict(row)) for row in rows]
                for claim in claims:
                    db.execute(
                        """UPDATE submissions SET status='expired'
                           WHERE submission_id=? AND status IN ('preparing','running')""",
                        (claim.submission_id,),
                    )
                    db.execute(
                        """DELETE FROM submission_claims
                           WHERE submission_id=? AND claim_token=?""",
                        (claim.submission_id, claim.claim_token),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return claims

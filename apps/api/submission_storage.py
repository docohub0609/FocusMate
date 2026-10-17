"""Persistent GoalRecord, CodeSnapshot, and Submission lifecycle rules."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .problem_intake import timestamp, utc_now
from .problem_storage import DEFAULT_DB_PATH, ProblemRepository
from .schema_validation import validate
from src.focusmate.adapters.raw_events import SQLiteEventStore, event_from_record


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


class SubmissionFailure(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


class SubmissionRepository:
    """Store real submission references without executing user code."""

    TRANSITIONS = {
        "awaitingAssets": {"queued", "failed"},
        "queued": {"preparing", "failed", "expired"},
        "preparing": {"running", "failed", "expired"},
        "running": {"finished", "failed", "expired"},
        "finished": set(),
        "failed": set(),
        "expired": set(),
    }

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Clock = utc_now,
        goal_id_factory: IdFactory = lambda: _new_id("goal"),
        snapshot_id_factory: IdFactory = lambda: _new_id("code-snapshot"),
        submission_id_factory: IdFactory = lambda: _new_id("submission"),
    ) -> None:
        self.path = Path(path or DEFAULT_DB_PATH)
        self.clock = clock
        self.goal_id_factory = goal_id_factory
        self.snapshot_id_factory = snapshot_id_factory
        self.submission_id_factory = submission_id_factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        # ProblemRepository owns the referenced problem tables and safely creates them if absent.
        ProblemRepository(self.path)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS goal_records (
                    goal_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'active'),
                    FOREIGN KEY(problem_id, problem_version)
                        REFERENCES problem_versions(problem_id, version)
                );
                CREATE TABLE IF NOT EXISTS code_snapshots (
                    code_snapshot_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    code_version INTEGER NOT NULL,
                    language TEXT NOT NULL CHECK(language = 'python3'),
                    source_code TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    UNIQUE(goal_id, code_version),
                    FOREIGN KEY(goal_id) REFERENCES goal_records(goal_id)
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL,
                    code_version INTEGER NOT NULL,
                    language TEXT NOT NULL CHECK(language = 'python3'),
                    resource_policy_ref TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(goal_id, idempotency_key),
                    FOREIGN KEY(goal_id) REFERENCES goal_records(goal_id),
                    FOREIGN KEY(goal_id, code_version)
                        REFERENCES code_snapshots(goal_id, code_version),
                    FOREIGN KEY(problem_id, problem_version)
                        REFERENCES problem_versions(problem_id, version)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(submissions)")}
            if "status_reason" not in columns:
                db.execute("ALTER TABLE submissions ADD COLUMN status_reason TEXT")
            SQLiteEventStore.initialize(db)

    def create_goal(self, problem_ref: dict[str, Any]) -> dict[str, Any]:
        record = ProblemRepository(self.path).get(
            problem_ref["problemId"], problem_ref["problemVersion"],
        )
        if record["lifecycle"]["status"] != "frozen":
            raise SubmissionFailure(
                "PROBLEM_VERSION_NOT_FROZEN",
                "A goal can only be created for a frozen problem version.", 409,
            )
        result = {
            "goalId": self.goal_id_factory(), "schemaVersion": "1.0.0",
            "createdAt": timestamp(self.clock()), "problemRef": problem_ref,
            "status": "active",
        }
        validate("GoalRecord", result)
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO goal_records VALUES(?,?,?,?,?,?)",
                (
                    result["goalId"], result["schemaVersion"], result["createdAt"],
                    problem_ref["problemId"], problem_ref["problemVersion"], result["status"],
                ),
            )
        return result

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM goal_records WHERE goal_id=?", (goal_id,)).fetchone()
        if row is None:
            raise SubmissionFailure("GOAL_NOT_FOUND", "The requested goal was not found.", 404)
        result = {
            "goalId": row["goal_id"], "schemaVersion": row["schema_version"],
            "createdAt": row["created_at"],
            "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
            "status": row["status"],
        }
        validate("GoalRecord", result)
        return result

    def create_code_snapshot(self, goal_id: str, source_code: str, language: str) -> dict[str, Any]:
        goal = self.get_goal(goal_id)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute(
                "SELECT COALESCE(MAX(code_version),0) FROM code_snapshots WHERE goal_id=?",
                (goal_id,),
            ).fetchone()[0]
            result = {
                "codeSnapshotId": self.snapshot_id_factory(), "schemaVersion": "1.0.0",
                "createdAt": timestamp(self.clock()),
                "goalContext": {"goalId": goal_id, "scope": "goal"},
                "codeVersion": latest + 1, "language": language, "sourceCode": source_code,
                "contentHash": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
            }
            validate("CodeSnapshot", result)
            db.execute(
                "INSERT INTO code_snapshots VALUES(?,?,?,?,?,?,?,?)",
                (
                    result["codeSnapshotId"], result["schemaVersion"], result["createdAt"],
                    goal_id, result["codeVersion"], language, source_code, result["contentHash"],
                ),
            )
            event = event_from_record('codeSnapshotCreated', dict(result, problemRef=goal['problemRef']))
            self._event_store().append_on_connection(db, event, trusted_source='api')
        return result

    def _event_store(self):
        # Tables are initialized before business transactions; constructing a second
        # connection here would contend with the caller's write lock.
        return SQLiteEventStore(self.path, initialize=False, clock=lambda: timestamp(self.clock()))

    def get_code_snapshot(self, goal_id: str, code_version: int) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM code_snapshots WHERE goal_id=? AND code_version=?",
                (goal_id, code_version),
            ).fetchone()
        if row is None:
            raise SubmissionFailure(
                "CODE_SNAPSHOT_NOT_FOUND", "The requested code snapshot was not found.", 404,
            )
        result = {
            "codeSnapshotId": row["code_snapshot_id"], "schemaVersion": row["schema_version"],
            "createdAt": row["created_at"],
            "goalContext": {"goalId": row["goal_id"], "scope": "goal"},
            "codeVersion": row["code_version"], "language": row["language"],
            "sourceCode": row["source_code"], "contentHash": row["content_hash"],
        }
        validate("CodeSnapshot", result)
        return result

    def create_submission(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        goal_id = request["goalContext"]["goalId"]
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute(
                "SELECT * FROM submissions WHERE goal_id=? AND idempotency_key=?",
                (goal_id, request["idempotencyKey"]),
            ).fetchone()
            if existing is not None:
                saved = self._submission(existing)
                if any(saved[key] != request[key] for key in ('goalContext', 'problemRef', 'codeVersion', 'language')):
                    raise SubmissionFailure('IDEMPOTENCY_CONFLICT', 'The same operation has different submission parameters.', 409)
                return self._submission(existing), False

            goal = self.get_goal(goal_id)
            if goal["problemRef"] != request["problemRef"]:
                raise SubmissionFailure(
                    "GOAL_CONTEXT_MISMATCH", "The goal belongs to another problem version.", 409,
                )
            snapshot = self.get_code_snapshot(goal_id, request["codeVersion"])
            if snapshot["language"] != request["language"]:
                raise SubmissionFailure(
                    "CODE_CONTEXT_MISMATCH", "The code snapshot language does not match the submission.", 409,
                )
            problem = ProblemRepository(self.path).get(
                request["problemRef"]["problemId"], request["problemRef"]["problemVersion"],
            )
            asset_status = problem["problemPackage"]["verifierAssetStatus"]
            if asset_status == "rejected":
                raise SubmissionFailure(
                    "VERIFIER_ASSET_REJECTED",
                    "Trusted verification assets could not be built for this problem version.", 422,
                )
            initial_status = "queued" if asset_status == "ready" else "awaitingAssets"
            result = {
                "submissionId": self.submission_id_factory(), "schemaVersion": "1.0.0",
                "createdAt": timestamp(self.clock()), "goalContext": request["goalContext"],
                "problemRef": request["problemRef"], "codeVersion": request["codeVersion"],
                "language": request["language"], "resourcePolicyRef": "python-default-v1",
                "idempotencyKey": request["idempotencyKey"], "status": initial_status,
            }
            validate("Submission", result)
            db.execute(
                """INSERT INTO submissions(
                    submission_id,schema_version,created_at,goal_id,problem_id,problem_version,
                    code_version,language,resource_policy_ref,idempotency_key,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result["submissionId"], result["schemaVersion"], result["createdAt"], goal_id,
                    request["problemRef"]["problemId"], request["problemRef"]["problemVersion"],
                    result["codeVersion"], result["language"], result["resourcePolicyRef"],
                    result["idempotencyKey"], result["status"],
                ),
            )
            event = event_from_record('submissionRequested', result)
            self._event_store().append_on_connection(db, event, trusted_source='api')
            from focusmate.adapters.coach_events import capture_execution
            capture_execution(db, 'submission', result['submissionId'], goal_id, result['codeVersion'])
        return result, True

    @staticmethod
    def _submission(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "submissionId": row["submission_id"], "schemaVersion": row["schema_version"],
            "createdAt": row["created_at"],
            "goalContext": {"goalId": row["goal_id"], "scope": "goal"},
            "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
            "codeVersion": row["code_version"], "language": row["language"],
            "resourcePolicyRef": row["resource_policy_ref"],
            "idempotencyKey": row["idempotency_key"], "status": row["status"],
        }
        if "status_reason" in row.keys() and row["status_reason"]:
            result["statusReason"] = row["status_reason"]
        validate("Submission", result)
        return result

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM submissions WHERE submission_id=?", (submission_id,),
            ).fetchone()
        if row is None:
            raise SubmissionFailure("SUBMISSION_NOT_FOUND", "The requested submission was not found.", 404)
        return self._submission(row)

    def transition(self, submission_id: str, target_status: str) -> dict[str, Any]:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT * FROM submissions WHERE submission_id=?", (submission_id,),
            ).fetchone()
            if row is None:
                raise SubmissionFailure("SUBMISSION_NOT_FOUND", "The requested submission was not found.", 404)
            if target_status not in self.TRANSITIONS[row["status"]]:
                raise SubmissionFailure(
                    "SUBMISSION_STATE_TRANSITION_INVALID",
                    f"Submission cannot move from {row['status']} to {target_status}.", 409,
                )
            db.execute(
                "UPDATE submissions SET status=? WHERE submission_id=?",
                (target_status, submission_id),
            )
        return self.get_submission(submission_id)

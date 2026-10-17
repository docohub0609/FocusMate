"""Persistent claiming and safe public status for verifier-build jobs."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from apps.api.problem_intake import timestamp, utc_now
from apps.api.problem_storage import ProblemRepository, StorageFailure
from src.focusmate.contracts.schema_validation import validate


Clock = Callable[[], datetime]

PHASE_MESSAGES = {
    "generating": "Verifier asset candidates are being generated.",
    "prechecking": "Verifier asset candidates are being checked.",
    "sealing": "Checked verifier asset candidates are being sealed.",
    "crossChecking": "Sealed verifier assets are being cross-checked.",
}

ISSUES: dict[str, tuple[str, str, bool]] = {
    "PROBLEM_FACTS_INSUFFICIENT": (
        "The frozen problem does not contain enough confirmed facts to build safe verifier assets.",
        "reviseProblem",
        False,
    ),
    "LLM_TEMPORARILY_UNAVAILABLE": (
        "The candidate provider is temporarily unavailable.", "retry", True,
    ),
    "LLM_CONFIGURATION_REQUIRED": (
        "The candidate provider requires valid configuration or account access.",
        "configureProvider", False,
    ),
    "VERIFIER_BUILDER_INTERNAL_ERROR": (
        "The verifier builder encountered an internal error.", "retry", True,
    ),
    "CANDIDATE_SCHEMA_INVALID": (
        "Generated candidates did not match the required structure.", "retry", True,
    ),
    "CANDIDATE_COMPILE_FAILED": (
        "A generated candidate could not be compiled safely.", "retry", True,
    ),
    "GENERATOR_NON_DETERMINISTIC": (
        "The generated input producer was not deterministic.", "retry", True,
    ),
    "GENERATOR_CASES_INVALID": (
        "The generated input set did not meet the required checks.", "retry", True,
    ),
    "GENERATOR_COVERAGE_INSUFFICIENT": (
        "The distinct generated inputs do not cover the required cases.", "retry", True,
    ),
    "SMALL_REFERENCE_TIMEOUT": (
        "The small-input reference exceeded its execution time limit.", "retry", True,
    ),
    "GENERATOR_REPAIR_INVALID": (
        "The input-generator correction was incomplete or invalid.", "retry", True,
    ),
    "GENERATOR_INPUT_LEGALITY_UNCONFIRMED": (
        "The corrected inputs could not be confirmed legal.", "retry", True,
    ),
    "REFERENCE_PUBLIC_MISMATCH": (
        "A generated reference did not match the public examples.", "retry", True,
    ),
    "REFERENCE_EXECUTION_FAILED": (
        "A generated reference failed to finish successfully within execution limits.", "retry", True,
    ),
    "REFERENCE_DISAGREEMENT": (
        "The independent references disagreed on candidate inputs.", "retry", True,
    ),
    "MUTATION_EVIDENCE_INSUFFICIENT": (
        "The candidate tests did not provide enough fault-detection evidence.", "retry", True,
    ),
    "MUTATION_RESPONSE_INVALID": (
        "The fault-checking response was incomplete or invalid; base candidates are retained.", "retry", True,
    ),
    "MUTATION_TEST_GAP": (
        "A proven faulty program survived the generated tests.", "retry", True,
    ),
    "SANDBOX_STARTUP_TIMEOUT": (
        "The execution environment did not start within its time limit.", "retry", True,
    ),
    "BUILD_BUDGET_EXCEEDED": (
        "The verifier asset build exceeded its time budget.", "retry", True,
    ),
    "BUILDER_LEASE_EXPIRED": (
        "The verifier builder stopped before its lease was renewed.", "retry", True,
    ),
}

CONTENT_FAILURES = frozenset({
    "CANDIDATE_SCHEMA_INVALID", "CANDIDATE_COMPILE_FAILED",
    "GENERATOR_NON_DETERMINISTIC", "GENERATOR_CASES_INVALID",
    "GENERATOR_COVERAGE_INSUFFICIENT", "SMALL_REFERENCE_TIMEOUT",
    "GENERATOR_REPAIR_INVALID", "GENERATOR_INPUT_LEGALITY_UNCONFIRMED",
    "REFERENCE_PUBLIC_MISMATCH", "REFERENCE_DISAGREEMENT", "REFERENCE_EXECUTION_FAILED",
    "MUTATION_EVIDENCE_INSUFFICIENT", "MUTATION_TEST_GAP",
})


@dataclass(frozen=True)
class BuildClaim:
    job_id: str
    problem_id: str
    problem_version: int
    worker_id: str
    claim_token: str
    claimed_at: str
    lease_expires_at: str
    attempt: int = 0
    repair_policy: int = 0


class VerifierBuildJobRepository:
    """Own durable job discovery and conditional worker transitions."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock = utc_now,
        job_id_factory: Callable[[], str] = lambda: f"verifier-build-{uuid4()}",
        token_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.job_id_factory = job_id_factory
        self.token_factory = token_factory
        ProblemRepository(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS verifier_build_jobs (
                    job_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','paused','completed','failed')),
                    phase TEXT CHECK(phase IN ('generating','prechecking','sealing','crossChecking')),
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                    issue_code TEXT,
                    worker_id TEXT,
                    claim_token TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(problem_id, problem_version),
                    FOREIGN KEY(problem_id, problem_version)
                        REFERENCES problem_versions(problem_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_verifier_build_claim_token
                    ON verifier_build_jobs(claim_token) WHERE claim_token IS NOT NULL;
                CREATE TABLE IF NOT EXISTS verifier_candidate_roles (
                    job_id TEXT NOT NULL REFERENCES verifier_build_jobs(job_id),
                    role TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,role)
                );
                CREATE TABLE IF NOT EXISTS verifier_candidate_sets (
                    job_id TEXT PRIMARY KEY REFERENCES verifier_build_jobs(job_id),
                    candidate_json TEXT NOT NULL,
                    prechecked_json TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verifier_case_repairs (
                    job_id TEXT NOT NULL REFERENCES verifier_build_jobs(job_id),
                    claim_token TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('requested','generated','checked')),
                    feedback_json TEXT NOT NULL,
                    original_json TEXT NOT NULL,
                    repaired_json TEXT,
                    review_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id,claim_token)
                );
                CREATE TABLE IF NOT EXISTS verifier_cross_check_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES verifier_build_jobs(job_id),
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verifier_auto_repairs (
                    job_id TEXT NOT NULL REFERENCES verifier_build_jobs(job_id),
                    attempt INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id,attempt)
                );
                """
            )
            columns = {row[1] for row in db.execute('PRAGMA table_info(verifier_build_jobs)')}
            if 'repair_policy' not in columns:
                db.execute('ALTER TABLE verifier_build_jobs ADD COLUMN repair_policy INTEGER NOT NULL DEFAULT 0')

    def discover_and_claim(self, worker_id: str, lease_seconds: int) -> BuildClaim | None:
        self._validate_claim_request(worker_id, lease_seconds)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                now_value = self.clock()
                now = timestamp(now_value)
                lease_expires_at = timestamp(now_value + timedelta(seconds=lease_seconds))
                self._discover_one(db, now)
                self._pause_expired(db, now)
                row = db.execute(
                    """SELECT * FROM verifier_build_jobs
                       WHERE status='queued'
                       ORDER BY created_at, job_id LIMIT 1"""
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                claim_token = self.token_factory()
                changed = db.execute(
                    """UPDATE verifier_build_jobs
                       SET status='running',phase='generating',attempt=attempt+1,
                           issue_code=NULL,worker_id=?,claim_token=?,claimed_at=?,
                           lease_expires_at=?,updated_at=?
                       WHERE job_id=? AND status='queued'""",
                    (worker_id, claim_token, now, lease_expires_at, now, row["job_id"]),
                ).rowcount
                if changed != 1:
                    raise StorageFailure(
                        "BUILD_JOB_CLAIM_CONFLICT",
                        "The verifier build job was claimed concurrently.", 409, [],
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return BuildClaim(
            row["job_id"], row["problem_id"], row["problem_version"],
            worker_id, claim_token, now, lease_expires_at,
            row['attempt'] + 1, row['repair_policy'],
        )

    def _discover_one(self, db: sqlite3.Connection, now: str) -> None:
        row = db.execute(
            """SELECT p.problem_id,p.version
               FROM problem_versions p
               WHERE p.lifecycle_status='frozen'
                 AND p.verifier_asset_status IN ('missing','building')
                 AND NOT EXISTS (
                     SELECT 1 FROM verifier_build_jobs j
                     WHERE j.problem_id=p.problem_id AND j.problem_version=p.version
                 )
               ORDER BY COALESCE(p.frozen_at,p.updated_at),p.problem_id,p.version
               LIMIT 1"""
        ).fetchone()
        if row is None:
            return
        db.execute(
            """INSERT OR IGNORE INTO verifier_build_jobs(
                   job_id,schema_version,problem_id,problem_version,status,phase,
                   attempt,issue_code,worker_id,claim_token,claimed_at,
                   lease_expires_at,created_at,updated_at,completed_at,repair_policy
               ) VALUES(?,?,?,?, 'queued',NULL,0,NULL,NULL,NULL,NULL,NULL,?,?,NULL,1)""",
            (
                self.job_id_factory(), "1.0.0", row["problem_id"], row["version"],
                now, now,
            ),
        )

    @staticmethod
    def _pause_expired(db: sqlite3.Connection, now: str) -> None:
        for row in db.execute('''SELECT r.job_id,r.attempt,r.record_json FROM verifier_auto_repairs r
            JOIN verifier_build_jobs j ON j.job_id=r.job_id AND j.attempt=r.attempt
            WHERE j.status='running' AND julianday(j.lease_expires_at)<=julianday(?)''', (now,)).fetchall():
            repair = json.loads(row['record_json'])
            if repair.get('status') in {'pending','running','submitted','validating'}:
                repair.update(status='interrupted', terminalReason='ownerLeaseExpired')
                db.execute('UPDATE verifier_auto_repairs SET record_json=?,updated_at=? WHERE job_id=? AND attempt=?',
                    (json.dumps(repair,ensure_ascii=False),now,row['job_id'],row['attempt']))
        db.execute(
            """UPDATE verifier_build_jobs
               SET status='paused',phase=NULL,issue_code='BUILDER_LEASE_EXPIRED',
                   worker_id=NULL,claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,
                   updated_at=?
               WHERE status='running'
                 AND julianday(lease_expires_at)<=julianday(?)""",
            (now, now),
        )

    def renew(self, claim: BuildClaim, lease_seconds: int) -> BuildClaim:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                now_value = self.clock()
                now = timestamp(now_value)
                renewed_until = timestamp(now_value + timedelta(seconds=lease_seconds))
                changed = db.execute(
                    """UPDATE verifier_build_jobs SET lease_expires_at=?,updated_at=?
                       WHERE job_id=? AND worker_id=? AND claim_token=? AND status='running'
                         AND julianday(lease_expires_at)>julianday(?)""",
                    (
                        renewed_until, now, claim.job_id, claim.worker_id,
                        claim.claim_token, now,
                    ),
                ).rowcount
                db.commit()
            except Exception:
                db.rollback()
                raise
        self._require_owned_change(changed)
        return BuildClaim(
            claim.job_id, claim.problem_id, claim.problem_version,
            claim.worker_id, claim.claim_token, claim.claimed_at, renewed_until,
            claim.attempt, claim.repair_policy,
        )

    def set_phase(self, claim: BuildClaim, phase: str) -> dict[str, Any]:
        if phase not in PHASE_MESSAGES:
            raise ValueError("phase must be an approved verifier-build phase.")
        self._owned_transition(
            claim,
            "UPDATE verifier_build_jobs SET phase=?,updated_at=?",
            (phase,),
        )
        return self.get(claim.problem_id, claim.problem_version)

    def guard(self, claim: BuildClaim, db: sqlite3.Connection) -> None:
        """Fence writes using the same connection and write lock as the caller."""
        row = db.execute(
            """SELECT 1 FROM verifier_build_jobs WHERE job_id=? AND worker_id=?
               AND claim_token=? AND status='running'
               AND julianday(lease_expires_at)>julianday(?)""",
            (claim.job_id, claim.worker_id, claim.claim_token, timestamp(self.clock())),
        ).fetchone()
        self._require_owned_change(0 if row is None else 1)

    def require_owned(self, claim: BuildClaim) -> None:
        with closing(self._connect()) as db:
            self.guard(claim, db)

    def requeue_configuration_paused(self) -> int:
        """Operator-only recovery, called only after production preflight succeeds."""
        with closing(self._connect()) as db, db:
            return db.execute(
                """UPDATE verifier_build_jobs SET status='queued',phase=NULL,
                   issue_code=NULL,updated_at=? WHERE status='paused'
                   AND issue_code='LLM_CONFIGURATION_REQUIRED'""",
                (timestamp(self.clock()),),
            ).rowcount

    def save_checkpoint(self, claim: BuildClaim, candidate: dict, prechecked: dict | None = None) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(claim, db)
            db.execute(
                """INSERT INTO verifier_candidate_sets VALUES(?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET candidate_json=excluded.candidate_json,
                   prechecked_json=excluded.prechecked_json,updated_at=excluded.updated_at""",
                (claim.job_id, json.dumps(candidate, ensure_ascii=False),
                 None if prechecked is None else json.dumps(prechecked, ensure_ascii=False),
                 timestamp(self.clock())),
            )

    def role_checkpoints(self, claim: BuildClaim) -> dict:
        with closing(self._connect()) as db:
            self.guard(claim,db)
            return {row[0]:json.loads(row[1]) for row in db.execute(
                "SELECT role,value_json FROM verifier_candidate_roles WHERE job_id=?",(claim.job_id,))}

    def save_role(self,claim: BuildClaim,role: str,value: dict) -> None:
        if role not in ('inputGenerator','slowOracle','fastReference'):raise ValueError('UNKNOWN_CANDIDATE_ROLE')
        with closing(self._connect()) as db,db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(claim,db)
            db.execute("INSERT OR REPLACE INTO verifier_candidate_roles VALUES(?,?,?)",
                (claim.job_id,role,json.dumps(value,ensure_ascii=False)))

    def checkpoint(self, claim: BuildClaim) -> dict | None:
        """Recover candidates only; persisted precheck evidence must be recomputed."""
        with closing(self._connect()) as db:
            self.guard(claim, db)
            row = db.execute("SELECT candidate_json FROM verifier_candidate_sets WHERE job_id=?",
                             (claim.job_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def save_run(self, claim: BuildClaim, run: dict) -> None:
        validate("VerifierCrossCheckRun", run)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(claim, db)
            db.execute("INSERT INTO verifier_cross_check_runs VALUES(?,?,?,?)",
                       (run["runId"], claim.job_id, json.dumps(run), run["createdAt"]))

    def fail(self, claim: BuildClaim) -> dict[str, Any]:
        self._owned_transition(claim,
            """UPDATE verifier_build_jobs SET status='failed',phase=NULL,
               issue_code='SEALED_MATERIAL_MISMATCH',worker_id=NULL,claim_token=NULL,
               claimed_at=NULL,lease_expires_at=NULL,updated_at=?,completed_at=?""",
            (), timestamp_fields=2)
        return self.get(claim.problem_id, claim.problem_version)

    def pause(self, claim: BuildClaim, issue: str) -> dict[str, Any]:
        if issue not in ISSUES:
            raise ValueError("issue must be an approved safe verifier-build issue code.")
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.guard(claim, db)
            now = timestamp(self.clock())
            db.execute("""UPDATE verifier_build_jobs
               SET status='paused',phase=NULL,issue_code=?,worker_id=NULL,
                   claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?""",
                (issue, now, claim.job_id))
            row = db.execute('SELECT record_json FROM verifier_auto_repairs WHERE job_id=? AND attempt=?',
                (claim.job_id, claim.attempt)).fetchone()
            if row:
                repair = json.loads(row[0])
                if repair.get('status') in {'pending','running','submitted','validating'}:
                    repair.update(status='exhausted' if issue == 'BUILD_BUDGET_EXCEEDED' else 'interrupted',
                        reason=issue)
                    db.execute('UPDATE verifier_auto_repairs SET record_json=?,updated_at=? WHERE job_id=? AND attempt=?',
                        (json.dumps(repair,ensure_ascii=False),now,claim.job_id,claim.attempt))
        return self.get(claim.problem_id, claim.problem_version)

    def complete(self, claim: BuildClaim) -> dict[str, Any]:
        self._owned_transition(
            claim,
            """UPDATE verifier_build_jobs
               SET status='completed',phase=NULL,issue_code=NULL,worker_id=NULL,
                   claim_token=NULL,claimed_at=NULL,lease_expires_at=NULL,
                   updated_at=?,completed_at=?""",
            (),
            timestamp_fields=2,
        )
        return self.get(claim.problem_id, claim.problem_version)

    def _owned_transition(
        self,
        claim: BuildClaim,
        update_prefix: str,
        values: tuple[object, ...],
        *,
        timestamp_fields: int = 1,
    ) -> None:
        sql = (
            update_prefix
            + " WHERE job_id=? AND worker_id=? AND claim_token=? AND status='running'"
            + " AND julianday(lease_expires_at)>julianday(?)"
        )
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                now = timestamp(self.clock())
                timestamps = (now,) * timestamp_fields
                changed = db.execute(
                    sql,
                    (
                        *values,
                        *timestamps,
                        claim.job_id,
                        claim.worker_id,
                        claim.claim_token,
                        now,
                    ),
                ).rowcount
                db.commit()
            except Exception:
                db.rollback()
                raise
        self._require_owned_change(changed)

    @staticmethod
    def _require_owned_change(changed: int) -> None:
        if changed != 1:
            raise StorageFailure(
                "BUILD_JOB_CLAIM_LOST",
                "The worker no longer owns a live verifier build claim.", 409, [],
            )

    def retry(self, problem_id: str, version: int) -> dict[str, Any]:
        self._require_frozen_context(problem_id, version)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                now = timestamp(self.clock())
                row = self._row(db, problem_id, version)
                if row["status"] == "paused":
                    _, _, retryable = self._issue_details(row["issue_code"])
                    if not retryable:
                        raise StorageFailure(
                            "BUILD_JOB_NOT_RETRYABLE",
                            "This build requires the allowed corrective action before retrying.",
                            409,
                            [],
                        )
                    sealed = self._has_sealed_or_terminal_asset(db, problem_id, version)
                    upgrading = not row['repair_policy'] and not sealed
                    if upgrading:
                        # An explicit user retry is a new attempt. Do not silently
                        # inherit a legacy repaired generator without its review duty.
                        db.execute('DELETE FROM verifier_candidate_sets WHERE job_id=?',(row['job_id'],))
                        db.execute("DELETE FROM verifier_candidate_roles WHERE job_id=? AND role='inputGenerator'",(row['job_id'],))
                    elif row["issue_code"] in CONTENT_FAILURES and not sealed:
                        db.execute("DELETE FROM verifier_candidate_sets WHERE job_id=?", (row["job_id"],))
                        # Format failure only invalidates the missing role, never good siblings.
                        if row["issue_code"] != "CANDIDATE_SCHEMA_INVALID":
                            db.execute("DELETE FROM verifier_candidate_roles WHERE job_id=?",(row["job_id"],))
                    db.execute(
                        """UPDATE verifier_build_jobs
                           SET status='queued',phase=NULL,issue_code=NULL,updated_at=?,repair_policy=?
                           WHERE job_id=? AND status='paused'""",
                        (now, 1 if upgrading else row['repair_policy'], row["job_id"]),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self.get(problem_id, version)

    @staticmethod
    def _has_sealed_or_terminal_asset(db: sqlite3.Connection, problem_id: str, version: int) -> bool:
        tables = {item[0] for item in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('trusted_test_materials','verifier_assets')",
        )}
        # Material and its summary commit separately, so building does not imply
        # that the candidate is safe to replace. Never rewrite an immutable seal.
        if "trusted_test_materials" in tables and db.execute(
            "SELECT 1 FROM trusted_test_materials WHERE problem_id=? AND problem_version=?",
            (problem_id, version),
        ).fetchone() is not None:
            return True
        return "verifier_assets" in tables and db.execute(
            "SELECT 1 FROM verifier_assets WHERE problem_id=? AND problem_version=? AND status IN ('ready','rejected')",
            (problem_id, version),
        ).fetchone() is not None

    def get(self, problem_id: str, version: int) -> dict[str, Any]:
        self._require_frozen_context(problem_id, version)
        with closing(self._connect()) as db:
            row = self._row(db, problem_id, version)
        result = self._safe_status(row)
        with closing(self._connect()) as db:
            receipt = db.execute('SELECT record_json FROM verifier_auto_repairs WHERE job_id=? AND attempt=?',
                (row['job_id'], row['attempt'])).fetchone()
        if receipt:
            from apps.api.repair_presentation import safe_repair
            repair = safe_repair(json.loads(receipt[0]))
            if repair:
                result['repair'] = repair
                if row['status'] == 'running' and repair['status'] in {'pending','running','submitted','validating'}:
                    result.update(phase='repairingCases',message='An automatic repair is being checked.')
        if row["status"] == "running":
            with closing(self._connect()) as db:
                repairing = db.execute("SELECT 1 FROM verifier_case_repairs WHERE job_id=? AND claim_token=? AND status IN ('requested','generated')",
                    (row["job_id"],row["claim_token"])).fetchone()
            if repairing:
                result.update(phase="repairingCases",message="The generated test inputs are being adjusted and checked.")
        if row["status"] == "completed":
            with closing(self._connect()) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verifier_assets'").fetchone()
                asset = db.execute(
                    "SELECT cross_check_report_json FROM verifier_assets WHERE problem_id=? AND problem_version=? AND status='ready'",
                    (problem_id, version),
                ).fetchone() if exists else None
            if asset is not None and asset[0]:
                report = json.loads(asset[0])
                validate("VerifierAssetCrossCheckReport", report)
                result["assurance"] = report.get("assurance", "full")
                result["warningCodes"] = report.get("warningCodes", [])
        validate("VerifierAssetBuildStatus", result)
        return result

    def _require_frozen_context(self, problem_id: str, version: int) -> None:
        record = ProblemRepository(self.path).get(problem_id, version)
        if record["lifecycle"]["status"] != "frozen":
            raise StorageFailure(
                "BUILD_JOB_PROBLEM_NOT_FROZEN",
                "Verifier asset builds require a frozen problem version.", 409, [],
            )

    @staticmethod
    def _row(db: sqlite3.Connection, problem_id: str, version: int) -> sqlite3.Row:
        row = db.execute(
            """SELECT * FROM verifier_build_jobs
               WHERE problem_id=? AND problem_version=?""",
            (problem_id, version),
        ).fetchone()
        if row is None:
            raise StorageFailure(
                "BUILD_JOB_NOT_FOUND",
                "No verifier build job exists for this problem version.", 404, [],
            )
        return row

    @staticmethod
    def _issue_details(issue_code: str | None) -> tuple[str, str, bool]:
        if issue_code is None:
            return "", "none", False
        if issue_code == "SEALED_MATERIAL_MISMATCH":
            return "Sealed verifier material did not match the checked material.", "none", False
        return ISSUES[issue_code]

    def _safe_status(self, row: sqlite3.Row) -> dict[str, Any]:
        status = row["status"]
        phase = row["phase"]
        issue_code = row["issue_code"]
        if status == "queued":
            message, allowed_action, retryable = (
                "The verifier asset build is queued.", "wait", False,
            )
        elif status == "running":
            message, allowed_action, retryable = (
                PHASE_MESSAGES[phase], "wait", False,
            )
        elif status in {"paused", "failed"}:
            message, allowed_action, retryable = self._issue_details(issue_code)
        else:
            message, allowed_action, retryable = (
                "The verifier asset build is complete.", "none", False,
            )
        return {
            "jobId": row["job_id"],
            "schemaVersion": row["schema_version"],
            "problemRef": {
                "problemId": row["problem_id"],
                "problemVersion": row["problem_version"],
            },
            "status": status,
            "phase": phase,
            "attempt": row["attempt"],
            "retryable": retryable,
            "issueCode": issue_code,
            "message": message,
            "allowedAction": allowed_action,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _validate_claim_request(worker_id: str, lease_seconds: int) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-blank.")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")

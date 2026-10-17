"""Persistent verifier-asset lifecycle and submission release gate."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from .problem_intake import timestamp, utc_now
from .problem_storage import ProblemRepository, StorageFailure
from .schema_validation import validate


class VerifierAssetRepository:
    """Internal write boundary; HTTP callers only receive safe read models."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = utc_now,
                 id_factory: Callable[[], str] = lambda: f"asset-{uuid4()}",
                 transaction_guard: Callable[[sqlite3.Connection], None] | None = None) -> None:
        self.path, self.clock, self.id_factory = Path(path), clock, id_factory
        self.transaction_guard = transaction_guard or (lambda db: None)
        ProblemRepository(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS verifier_assets (
                    asset_id TEXT NOT NULL, asset_version INTEGER NOT NULL,
                    schema_version TEXT NOT NULL, problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL, status TEXT NOT NULL,
                    material_summary_json TEXT, cross_check_report_json TEXT,
                    rejection_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(asset_id,asset_version), UNIQUE(problem_id,problem_version),
                    FOREIGN KEY(problem_id,problem_version) REFERENCES problem_versions(problem_id,version)
                );
                CREATE TABLE IF NOT EXISTS verifier_asset_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL, asset_version INTEGER NOT NULL,
                    from_status TEXT NOT NULL, to_status TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(submissions)")}
            if columns and "status_reason" not in columns:
                db.execute("ALTER TABLE submissions ADD COLUMN status_reason TEXT")

    def create_missing(self, problem_id: str, problem_version: int) -> dict[str, Any]:
        record = ProblemRepository(self.path).get(problem_id, problem_version)
        if record["lifecycle"]["status"] != "frozen":
            raise StorageFailure("ASSET_PROBLEM_NOT_FROZEN", "Verifier assets require a frozen problem version.", 409, [])
        now, asset_id = timestamp(self.clock()), self.id_factory()
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self.transaction_guard(db)
                db.execute("INSERT INTO verifier_assets VALUES(?,?,?,?,?,'missing',NULL,NULL,NULL,?,?)",
                           (asset_id, 1, "1.0.0", problem_id, problem_version, now, now))
        except sqlite3.IntegrityError as exc:
            raise StorageFailure("ASSET_ALREADY_EXISTS", "This problem version already has an asset record.", 409, []) from exc
        return self.get(problem_id, problem_version)

    def start_build(self, problem_id: str, problem_version: int) -> dict[str, Any]:
        return self._simple_transition(problem_id, problem_version, "missing", "building", "buildStarted")

    def submit_material(self, problem_id: str, problem_version: int,
                        summary: dict[str, Any]) -> dict[str, Any]:
        validate("VerifierAssetMaterialSummary", summary)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.transaction_guard(db)
            row = self._row(db, problem_id, problem_version)
            self._require_status(row, "building")
            now = timestamp(self.clock())
            db.execute("UPDATE verifier_assets SET status='crossChecking',material_summary_json=?,updated_at=? WHERE asset_id=? AND asset_version=1",
                       (json.dumps(summary, separators=(",", ":")), now, row["asset_id"]))
            self._log(db, row, "crossChecking", "materialRegistered", now)
            self.transaction_guard(db)
            db.commit()
        return self.get(problem_id, problem_version)

    def complete_cross_check(self, problem_id: str, problem_version: int,
                             report: dict[str, Any]) -> dict[str, Any]:
        validate("VerifierAssetCrossCheckReport", report)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.transaction_guard(db)
            row = self._row(db, problem_id, problem_version)
            self._require_status(row, "crossChecking")
            summary = json.loads(row["material_summary_json"])
            if report["checkedMaterialHash"] != summary["materialHash"]:
                db.rollback()
                raise StorageFailure("ASSET_REPORT_MISMATCH", "The report checked a different material version.", 409, [])
            if report["outcome"] == "passed" and summary["buildMethod"] == "publicExamplesOnly":
                db.rollback()
                raise StorageFailure("ASSET_PUBLIC_ONLY", "Public examples alone cannot become trusted final assets.", 409, [])
            target = "ready" if report["outcome"] == "passed" else "rejected"
            reason = None if target == "ready" else "crossCheckFailed"
            now = timestamp(self.clock())
            db.execute("UPDATE verifier_assets SET status=?,cross_check_report_json=?,rejection_reason=?,updated_at=? WHERE asset_id=? AND asset_version=1",
                       (target, json.dumps(report, separators=(",", ":")), reason, now, row["asset_id"]))
            db.execute("UPDATE problem_versions SET verifier_asset_status=?,updated_at=? WHERE problem_id=? AND version=?",
                       (target, now, problem_id, problem_version))
            if self._has_submissions(db):
                if target == "ready":
                    db.execute("UPDATE submissions SET status='queued',status_reason=NULL WHERE problem_id=? AND problem_version=? AND status='awaitingAssets'",
                               (problem_id, problem_version))
                else:
                    db.execute("UPDATE submissions SET status='failed',status_reason='verifierAssetRejected' WHERE problem_id=? AND problem_version=? AND status='awaitingAssets'",
                               (problem_id, problem_version))
            self._log(db, row, target, "crossCheckPassed" if target == "ready" else reason, now)
            # Registration may consume the remaining deadline or lease. Fence
            # the commit so all readiness and submission changes roll back.
            self.transaction_guard(db)
            db.commit()
        return self.get(problem_id, problem_version)

    def reject(self, problem_id: str, problem_version: int, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise StorageFailure("ASSET_REJECTION_REASON_REQUIRED", "A safe rejection reason is required.", 422, [])
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.transaction_guard(db)
            row = self._row(db, problem_id, problem_version)
            if row["status"] not in {"building", "crossChecking"}:
                db.rollback(); self._require_status(row, "building")
            now = timestamp(self.clock())
            db.execute("UPDATE verifier_assets SET status='rejected',rejection_reason=?,updated_at=? WHERE asset_id=? AND asset_version=1",
                       (reason.strip(), now, row["asset_id"]))
            db.execute("UPDATE problem_versions SET verifier_asset_status='rejected',updated_at=? WHERE problem_id=? AND version=?",
                       (now, problem_id, problem_version))
            if self._has_submissions(db):
                db.execute("UPDATE submissions SET status='failed',status_reason='verifierAssetRejected' WHERE problem_id=? AND problem_version=? AND status='awaitingAssets'",
                           (problem_id, problem_version))
            self._log(db, row, "rejected", reason.strip(), now); db.commit()
        return self.get(problem_id, problem_version)

    def get(self, problem_id: str, problem_version: int) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = self._row(db, problem_id, problem_version)
        result: dict[str, Any] = {
            "assetId": row["asset_id"], "schemaVersion": row["schema_version"],
            "assetVersion": row["asset_version"],
            "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
            "status": row["status"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }
        if row["material_summary_json"]: result["materialSummary"] = json.loads(row["material_summary_json"])
        if row["cross_check_report_json"]: result["crossCheckReportRef"] = json.loads(row["cross_check_report_json"])["reportId"]
        if row["rejection_reason"]: result["rejectionReason"] = row["rejection_reason"]
        validate("VerifierAsset", result)
        return result

    def verification_availability(self, problem_id: str, problem_version: int) -> dict[str, Any]:
        try:
            asset = self.get(problem_id, problem_version)
        except StorageFailure as exc:
            if exc.code != "ASSET_NOT_FOUND": raise
            ProblemRepository(self.path).get(problem_id, problem_version)
            return {"problemRef": {"problemId": problem_id, "problemVersion": problem_version},
                    "availability": "verificationUnavailable", "assetStatus": "missing"}
        result = {"problemRef": asset["problemRef"],
                  "availability": "available" if asset["status"] == "ready" else "verificationUnavailable",
                  "assetStatus": asset["status"]}
        if asset["status"] == "ready": result["assetRef"] = {"assetId": asset["assetId"], "assetVersion": asset["assetVersion"]}
        return result

    def transitions(self, asset_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT from_status,to_status,reason,created_at FROM verifier_asset_transitions WHERE asset_id=? ORDER BY transition_id", (asset_id,)).fetchall()
        return [{"fromStatus": r[0], "toStatus": r[1], "reason": r[2], "createdAt": r[3]} for r in rows]

    def _simple_transition(self, problem_id: str, version: int, source: str, target: str, reason: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.transaction_guard(db)
            row = self._row(db, problem_id, version); self._require_status(row, source)
            now = timestamp(self.clock()); db.execute("UPDATE verifier_assets SET status=?,updated_at=? WHERE asset_id=?", (target, now, row["asset_id"]))
            db.execute("UPDATE problem_versions SET verifier_asset_status=?,updated_at=? WHERE problem_id=? AND version=?", (target, now, problem_id, version))
            self._log(db, row, target, reason, now); db.commit()
        return self.get(problem_id, version)

    @staticmethod
    def _row(db: sqlite3.Connection, problem_id: str, version: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM verifier_assets WHERE problem_id=? AND problem_version=?", (problem_id, version)).fetchone()
        if row is None: raise StorageFailure("ASSET_NOT_FOUND", "No verifier asset exists for this problem version.", 404, [])
        return row

    @staticmethod
    def _require_status(row: sqlite3.Row, expected: str) -> None:
        if row["status"] != expected:
            raise StorageFailure("ASSET_STATE_TRANSITION_INVALID", f"Verifier asset cannot move from {row['status']}; expected {expected}.", 409, [])

    @staticmethod
    def _log(db: sqlite3.Connection, row: sqlite3.Row, target: str, reason: str, now: str) -> None:
        db.execute("INSERT INTO verifier_asset_transitions(asset_id,asset_version,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?,?)",
                   (row["asset_id"], row["asset_version"], row["status"], target, reason, now))

    @staticmethod
    def _has_submissions(db: sqlite3.Connection) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submissions'").fetchone() is not None

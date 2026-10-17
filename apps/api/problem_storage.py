"""SQLite persistence and lifecycle rules for ProblemPackage records."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from datetime import datetime
import json
import hashlib
import base64
import re
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

from .problem_identity import problem_content_hash
from .problem_intake import timestamp, utc_now
from .schema_validation import validate


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "focusmate.db"


@dataclass(frozen=True)
class StorageFailure(Exception):
    code: str
    message: str
    status: int
    field_errors: list[dict[str, str]]


class ProblemRepository:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        configured = path or os.environ.get("FOCUSMATE_DB_PATH") or DEFAULT_DB_PATH
        self.path = Path(configured)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS problems (
                    problem_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL CHECK(source = 'userProvided'),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS problem_versions (
                    problem_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    input_spec TEXT NOT NULL,
                    output_spec TEXT NOT NULL,
                    constraints_text TEXT NOT NULL,
                    language_policy_json TEXT NOT NULL,
                    io_mode TEXT NOT NULL,
                    verifier_asset_status TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('draft','frozen')),
                    draft_revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    frozen_at TEXT,
                    confirmed_feature_set_id TEXT,
                    PRIMARY KEY(problem_id, version),
                    FOREIGN KEY(problem_id) REFERENCES problems(problem_id)
                );
                CREATE TABLE IF NOT EXISTS problem_examples (
                    problem_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    example_index INTEGER NOT NULL,
                    input_text TEXT NOT NULL,
                    output_text TEXT NOT NULL,
                    PRIMARY KEY(problem_id, version, example_index),
                    FOREIGN KEY(problem_id, version)
                        REFERENCES problem_versions(problem_id, version) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS problem_feature_sets (
                    feature_set_id TEXT PRIMARY KEY,
                    problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    record_status TEXT NOT NULL CHECK(record_status IN ('active','superseded')),
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(problem_id, problem_version)
                        REFERENCES problem_versions(problem_id, version)
                );
                CREATE INDEX IF NOT EXISTS feature_sets_lookup
                    ON problem_feature_sets(problem_id, problem_version, content_hash, record_status);
                CREATE TABLE IF NOT EXISTS problem_create_operations (
                    operation_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS problem_titles (
                    problem_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    PRIMARY KEY(problem_id, version),
                    FOREIGN KEY(problem_id,version) REFERENCES problem_versions(problem_id,version)
                );
                CREATE TABLE IF NOT EXISTS problem_library_deletions (
                    problem_id TEXT PRIMARY KEY REFERENCES problems(problem_id),
                    deleted_at TEXT NOT NULL
                );
            """)

    def create(self, problem: dict[str, Any], *, operation_key: str | None = None) -> dict[str, Any]:
        validate("ProblemPackage", problem)
        now = timestamp(self.clock())
        content_hash = problem_content_hash(problem)
        # A name is display metadata, but changing it is still a different save request.
        operation_hash = (hashlib.sha256(json.dumps([content_hash, problem['title']], ensure_ascii=False).encode()).hexdigest()
                          if problem.get('title') else content_hash)
        if operation_key is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_key):
            raise StorageFailure('INVALID_OPERATION_KEY', 'Invalid operation identifier.', 422, [])
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if operation_key:
                old = db.execute('SELECT * FROM problem_create_operations WHERE operation_key=?', (operation_key,)).fetchone()
                if old:
                    if old['content_hash'] != operation_hash:
                        raise StorageFailure('IDEMPOTENCY_CONFLICT', 'This operation has different content.', 409, [])
                    return json.loads(old['result_json'])
            db.execute(
                "INSERT INTO problems(problem_id, source, created_at) VALUES(?,?,?)",
                (problem["problemId"], problem["source"], problem["createdAt"]),
            )
            self._insert_version(db, problem, 1, content_hash, now)
            row = db.execute('SELECT * FROM problem_versions WHERE problem_id=? AND version=1', (problem['problemId'],)).fetchone()
            result = self._record(db, row)
            if operation_key:
                db.execute('INSERT INTO problem_create_operations VALUES(?,?,?)',
                           (operation_key, operation_hash, json.dumps(result, ensure_ascii=False)))
        return result

    def set_deleted(self, problem_id: str, deleted: bool) -> dict[str, Any]:
        """Remove from the library without breaking immutable historical references."""
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM problems WHERE problem_id=?', (problem_id,)).fetchone():
                raise StorageFailure('PROBLEM_NOT_FOUND', 'The requested problem was not found.', 404, [])
            if deleted:
                db.execute('INSERT OR IGNORE INTO problem_library_deletions VALUES(?,?)', (problem_id, timestamp(self.clock())))
            else:
                db.execute('DELETE FROM problem_library_deletions WHERE problem_id=?', (problem_id,))
        return {'problemId': problem_id, 'deleted': deleted}

    def list_problems(self, *, limit: int = 20, cursor: str | None = None, deleted: bool = False) -> dict[str, Any]:
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise StorageFailure('INVALID_LIST_REQUEST', 'Invalid page size.', 422, [])
        where, args = '', []
        if cursor:
            try:
                stamp, key = json.loads(base64.urlsafe_b64decode(cursor).decode())
                if not isinstance(stamp, str) or not isinstance(key, str):
                    raise ValueError()
                where = 'AND (v.updated_at < ? OR (v.updated_at = ? AND v.problem_id < ?))'
                args = [stamp, stamp, key]
            except Exception:
                raise StorageFailure('INVALID_LIST_REQUEST', 'Invalid page cursor.', 422, []) from None
        with closing(self._connect()) as db:
            rows = db.execute(f'''SELECT v.*,t.title FROM problem_versions v
                LEFT JOIN problem_titles t ON t.problem_id=v.problem_id AND t.version=v.version
                WHERE v.version=(SELECT MAX(p.version) FROM problem_versions p WHERE p.problem_id=v.problem_id)
                AND {'EXISTS' if deleted else 'NOT EXISTS'}
                    (SELECT 1 FROM problem_library_deletions d WHERE d.problem_id=v.problem_id)
                {where} ORDER BY v.updated_at DESC,v.problem_id DESC LIMIT ?''', (*args, limit + 1)).fetchall()
        selected = rows[:limit]
        items = [{'problemId': r['problem_id'], 'problemVersion': r['version'],
                  'title': r['title'] or '', 'preview': r['statement'][:160], 'updatedAt': r['updated_at'],
                  'status': r['lifecycle_status'], 'verifierAssetStatus': r['verifier_asset_status']} for r in selected]
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last['updated_at'], last['problem_id']]).encode()).decode()
        return {'items': items, 'nextCursor': next_cursor}

    def confirmed_feature_set(self, problem_id: str, version: int) -> dict[str, Any]:
        record = self.get(problem_id, version)
        identifier = record['lifecycle'].get('confirmedFeatureSetId')
        if not identifier:
            raise StorageFailure('FEATURE_NOT_FOUND', 'No confirmed report exists.', 404, [])
        return self.get_feature_set(identifier)

    def _insert_version(
        self,
        db: sqlite3.Connection,
        problem: dict[str, Any],
        draft_revision: int,
        content_hash: str,
        updated_at: str,
    ) -> None:
        db.execute(
            """INSERT INTO problem_versions(
                problem_id,version,schema_version,created_at,statement,input_spec,
                output_spec,constraints_text,language_policy_json,io_mode,
                verifier_asset_status,lifecycle_status,draft_revision,content_hash,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                problem["problemId"], problem["version"], problem["schemaVersion"],
                problem["createdAt"], problem["statement"], problem["inputSpec"],
                problem["outputSpec"], problem["constraints"],
                json.dumps(problem["languagePolicy"], separators=(",", ":")),
                problem["ioMode"], problem["verifierAssetStatus"], "draft",
                draft_revision, content_hash, updated_at,
            ),
        )
        self._replace_examples(db, problem)
        self._save_title(db, problem)

    @staticmethod
    def _save_title(db: sqlite3.Connection, problem: dict[str, Any]) -> None:
        if 'title' in problem:
            db.execute('INSERT OR REPLACE INTO problem_titles VALUES(?,?,?)',
                       (problem['problemId'], problem['version'], problem['title']))
        else:
            db.execute('DELETE FROM problem_titles WHERE problem_id=? AND version=?',
                       (problem['problemId'], problem['version']))

    @staticmethod
    def _replace_examples(db: sqlite3.Connection, problem: dict[str, Any]) -> None:
        db.execute(
            "DELETE FROM problem_examples WHERE problem_id=? AND version=?",
            (problem["problemId"], problem["version"]),
        )
        db.executemany(
            """INSERT INTO problem_examples(
                problem_id,version,example_index,input_text,output_text
            ) VALUES(?,?,?,?,?)""",
            [
                (problem["problemId"], problem["version"], index, item["input"], item["output"])
                for index, item in enumerate(problem["examples"])
            ],
        )

    def get(self, problem_id: str, version: int | None = None) -> dict[str, Any]:
        with closing(self._connect()) as db, db:
            if version is None:
                row = db.execute(
                    "SELECT * FROM problem_versions WHERE problem_id=? ORDER BY version DESC LIMIT 1",
                    (problem_id,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
                    (problem_id, version),
                ).fetchone()
            if row is None:
                raise StorageFailure("PROBLEM_NOT_FOUND", "The requested problem version was not found.", 404, [])
            return self._record(db, row)

    def _record(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        examples = db.execute(
            """SELECT input_text,output_text FROM problem_examples
               WHERE problem_id=? AND version=? ORDER BY example_index""",
            (row["problem_id"], row["version"]),
        ).fetchall()
        problem = {
            "problemId": row["problem_id"], "schemaVersion": row["schema_version"],
            "version": row["version"], "createdAt": row["created_at"],
            "source": "userProvided", "statement": row["statement"],
            "inputSpec": row["input_spec"], "outputSpec": row["output_spec"],
            "constraints": row["constraints_text"],
            "examples": [{"input": item[0], "output": item[1]} for item in examples],
            "languagePolicy": json.loads(row["language_policy_json"]),
            "ioMode": row["io_mode"], "verifierAssetStatus": row["verifier_asset_status"],
        }
        title = db.execute('SELECT title FROM problem_titles WHERE problem_id=? AND version=?',
                           (row['problem_id'], row['version'])).fetchone()
        if title is not None:
            problem['title'] = title['title']
        lifecycle: dict[str, Any] = {
            "status": row["lifecycle_status"], "draftRevision": row["draft_revision"],
            "contentHash": row["content_hash"], "updatedAt": row["updated_at"],
        }
        if row["frozen_at"]:
            lifecycle["frozenAt"] = row["frozen_at"]
            lifecycle["confirmedFeatureSetId"] = row["confirmed_feature_set_id"]
        record = {"problemPackage": problem, "lifecycle": lifecycle}
        validate("ProblemRecord", record)
        return record

    def list_versions(self, problem_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db, db:
            rows = db.execute(
                """SELECT version,lifecycle_status,draft_revision,content_hash,updated_at,frozen_at
                   FROM problem_versions WHERE problem_id=? ORDER BY version""",
                (problem_id,),
            ).fetchall()
        if not rows:
            raise StorageFailure("PROBLEM_NOT_FOUND", "The requested problem was not found.", 404, [])
        return [
            {
                "version": row["version"], "status": row["lifecycle_status"],
                "draftRevision": row["draft_revision"], "contentHash": row["content_hash"],
                "updatedAt": row["updated_at"], **({"frozenAt": row["frozen_at"]} if row["frozen_at"] else {}),
            }
            for row in rows
        ]

    def update_draft(
        self, problem_id: str, version: int, problem: dict[str, Any], expected_revision: int,
    ) -> dict[str, Any]:
        validate("ProblemPackage", problem)
        now = timestamp(self.clock())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
                (problem_id, version),
            ).fetchone()
            self._require_draft(row, expected_revision)
            next_revision = expected_revision + 1
            content_hash = problem_content_hash(problem)
            db.execute(
                """UPDATE problem_versions SET statement=?,input_spec=?,output_spec=?,constraints_text=?,
                   language_policy_json=?,io_mode=?,draft_revision=?,content_hash=?,updated_at=?
                   WHERE problem_id=? AND version=?""",
                (
                    problem["statement"], problem["inputSpec"], problem["outputSpec"],
                    problem["constraints"], json.dumps(problem["languagePolicy"], separators=(",", ":")),
                    problem["ioMode"], next_revision, content_hash, now, problem_id, version,
                ),
            )
            db.execute(
                """UPDATE problem_feature_sets SET record_status='superseded'
                   WHERE problem_id=? AND problem_version=? AND record_status='active'""",
                (problem_id, version),
            )
            problem["problemId"], problem["version"] = problem_id, version
            self._replace_examples(db, problem)
            self._save_title(db, problem)
        return self.get(problem_id, version)

    @staticmethod
    def _require_draft(row: sqlite3.Row | None, expected_revision: int) -> None:
        if row is None:
            raise StorageFailure("PROBLEM_NOT_FOUND", "The requested problem version was not found.", 404, [])
        if row["lifecycle_status"] != "draft":
            raise StorageFailure("PROBLEM_VERSION_FROZEN", "A frozen problem version cannot be changed.", 409, [])
        if row["draft_revision"] != expected_revision:
            raise StorageFailure("DRAFT_REVISION_CONFLICT", "The draft has changed; reload it before saving.", 409, [])

    def create_revision(self, problem_id: str, base_version: int) -> dict[str, Any]:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            base = db.execute(
                "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
                (problem_id, base_version),
            ).fetchone()
            if base is None:
                raise StorageFailure("PROBLEM_NOT_FOUND", "The base problem version was not found.", 404, [])
            if base["lifecycle_status"] != "frozen":
                raise StorageFailure("BASE_VERSION_NOT_FROZEN", "Only a frozen version can be revised.", 409, [])
            if db.execute(
                "SELECT 1 FROM problem_versions WHERE problem_id=? AND lifecycle_status='draft'",
                (problem_id,),
            ).fetchone():
                raise StorageFailure("DRAFT_ALREADY_EXISTS", "This problem already has an editable draft.", 409, [])
            latest = db.execute(
                "SELECT MAX(version) FROM problem_versions WHERE problem_id=?", (problem_id,),
            ).fetchone()[0]
            if base_version != latest:
                raise StorageFailure("BASE_VERSION_NOT_LATEST", "Only the latest frozen version can be revised.", 409, [])
            base_record = self._record(db, base)["problemPackage"]
            new_version = latest + 1
            now = timestamp(self.clock())
            base_record.update({"version": new_version, "createdAt": now, "verifierAssetStatus": "missing"})
            self._insert_version(db, base_record, 1, problem_content_hash(base_record), now)
        return self.get(problem_id, new_version)

    def save_feature_set(self, result: dict[str, Any], *, expected_revision: int | None = None) -> None:
        validate("ProblemFeatures", result)
        ref = result["problemRef"]
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._save_feature_set(db, result, expected_revision)

    def _save_feature_set(self, db: sqlite3.Connection, result: dict[str, Any], expected_revision: int | None) -> None:
        ref = result['problemRef']
        row = db.execute(
            "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
            (ref["problemId"], ref["problemVersion"]),
        ).fetchone()
        if row is None or row["content_hash"] != result["problemContentHash"]:
            raise StorageFailure("FEATURE_STALE", "The parsed problem content is no longer current.", 409, [])
        self._require_draft(row, expected_revision if expected_revision is not None else row['draft_revision'])
        db.execute(
            """UPDATE problem_feature_sets SET record_status='superseded'
               WHERE problem_id=? AND problem_version=? AND content_hash=? AND record_status='active'""",
            (ref["problemId"], ref["problemVersion"], result["problemContentHash"]),
        )
        db.execute(
            """INSERT INTO problem_feature_sets(
               feature_set_id,problem_id,problem_version,content_hash,record_status,result_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                result["featureSetId"], ref["problemId"], ref["problemVersion"],
                result["problemContentHash"], "active",
                json.dumps(result, ensure_ascii=False, separators=(",", ":")), result["createdAt"],
            ),
        )

    def current_feature_set(self, problem_id: str, version: int) -> dict[str, Any]:
        with closing(self._connect()) as db, db:
            row = db.execute(
                """SELECT f.result_json FROM problem_feature_sets f
                   JOIN problem_versions v ON v.problem_id=f.problem_id AND v.version=f.problem_version
                   WHERE f.problem_id=? AND f.problem_version=? AND f.record_status='active'
                   AND f.content_hash=v.content_hash
                   ORDER BY f.created_at DESC LIMIT 1""",
                (problem_id, version),
            ).fetchone()
        if row is None:
            raise StorageFailure("FEATURE_NOT_FOUND", "No current feature report exists for this version.", 404, [])
        return json.loads(row[0])

    def get_feature_set(self, feature_set_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT result_json FROM problem_feature_sets WHERE feature_set_id=?",
                (feature_set_id,),
            ).fetchone()
        if row is None:
            raise StorageFailure("FEATURE_NOT_FOUND", "The requested feature report was not found.", 404, [])
        return json.loads(row[0])

    def confirm_and_freeze(
        self,
        problem_id: str,
        version: int,
        result: dict[str, Any],
        expected_revision: int,
        based_on_feature_set_id: str | None,
    ) -> dict[str, Any]:
        """Atomically store a human-authoritative feature report and freeze its draft."""

        validate("ProblemFeatures", result)
        now = timestamp(self.clock())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version_row = db.execute(
                "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
                (problem_id, version),
            ).fetchone()
            self._require_draft(version_row, expected_revision)
            assert version_row is not None
            expected_ref = {"problemId": problem_id, "problemVersion": version}
            if (
                result["problemRef"] != expected_ref
                or result["problemContentHash"] != version_row["content_hash"]
            ):
                raise StorageFailure(
                    "FEATURE_CONTEXT_MISMATCH",
                    "The reviewed feature report does not match the current problem content.", 409, [],
                )
            active_reports = db.execute(
                "SELECT feature_set_id,result_json FROM problem_feature_sets WHERE problem_id=? AND problem_version=? AND record_status='active'",
                (problem_id, version),
            ).fetchall()
            for active in active_reports:
                active_result = json.loads(active['result_json'])
                if active_result.get('ioProposal') and (
                    based_on_feature_set_id != active['feature_set_id']
                    or result.get('ioProposalDecision') != 'keepOriginal'
                    or result.get('ioProposal') != active_result['ioProposal']
                ):
                    raise StorageFailure('FEATURE_CONFLICT', '请处理当前报告的输入输出建议后再确认。', 422, [])
            if based_on_feature_set_id:
                base = db.execute(
                    "SELECT * FROM problem_feature_sets WHERE feature_set_id=?",
                    (based_on_feature_set_id,),
                ).fetchone()
                if base is None:
                    raise StorageFailure("FEATURE_NOT_FOUND", "The base feature report was not found.", 404, [])
                if (
                    base["problem_id"] != problem_id
                    or base["problem_version"] != version
                    or base["content_hash"] != version_row["content_hash"]
                    or base["record_status"] != "active"
                ):
                    raise StorageFailure(
                        "FEATURE_STALE", "The base feature report is no longer current.", 409, [],
                    )
            db.execute(
                """UPDATE problem_feature_sets SET record_status='superseded'
                   WHERE problem_id=? AND problem_version=? AND record_status='active'""",
                (problem_id, version),
            )
            db.execute(
                """INSERT INTO problem_feature_sets(
                   feature_set_id,problem_id,problem_version,content_hash,record_status,result_json,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    result["featureSetId"], problem_id, version,
                    result["problemContentHash"], "active",
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")), result["createdAt"],
                ),
            )
            db.execute(
                """UPDATE problem_versions SET lifecycle_status='frozen',frozen_at=?,
                   confirmed_feature_set_id=?,updated_at=? WHERE problem_id=? AND version=?""",
                (now, result["featureSetId"], now, problem_id, version),
            )
        return self.get(problem_id, version)

    def freeze(
        self, problem_id: str, version: int, feature_set_id: str, expected_revision: int,
    ) -> dict[str, Any]:
        now = timestamp(self.clock())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version_row = db.execute(
                "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
                (problem_id, version),
            ).fetchone()
            self._require_draft(version_row, expected_revision)
            feature_row = db.execute(
                "SELECT * FROM problem_feature_sets WHERE feature_set_id=?",
                (feature_set_id,),
            ).fetchone()
            if feature_row is None:
                raise StorageFailure("FEATURE_NOT_FOUND", "The confirmed feature report was not found.", 404, [])
            if feature_row["problem_id"] != problem_id or feature_row["problem_version"] != version:
                raise StorageFailure("FEATURE_CONTEXT_MISMATCH", "The feature report belongs to another problem version.", 409, [])
            if feature_row["record_status"] != "active" or feature_row["content_hash"] != version_row["content_hash"]:
                raise StorageFailure("FEATURE_STALE", "The feature report does not match the current draft.", 409, [])
            result = json.loads(feature_row["result_json"])
            if result.get('ioProposal') and result.get('ioProposalDecision') != 'keepOriginal':
                raise StorageFailure('FEATURE_CONFLICT', '请先处理输入输出格式建议，再确认题目。', 422, [])
            if any(item["status"] == "conflict" for item in result["features"]):
                raise StorageFailure("FEATURE_CONFLICT", "Resolve conflicting problem facts before freezing.", 422, [])
            db.execute(
                """UPDATE problem_versions SET lifecycle_status='frozen',frozen_at=?,
                   confirmed_feature_set_id=?,updated_at=? WHERE problem_id=? AND version=?""",
                (now, feature_set_id, now, problem_id, version),
            )
        return self.get(problem_id, version)

"""Register frozen ProblemPackage examples as durable public test cases."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from .problem_intake import timestamp, utc_now
from .problem_storage import ProblemRepository, StorageFailure
from .schema_validation import validate
from .verifier_assets import VerifierAssetRepository


MAX_TEXT_BYTES = 1024 * 1024


class PublicTestCaseRepository:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = utc_now,
                 id_factory: Callable[[], str] = lambda: f"public-test-{uuid4()}",
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
            db.execute("""CREATE TABLE IF NOT EXISTS public_test_cases (
                test_case_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
                problem_id TEXT NOT NULL, problem_version INTEGER NOT NULL,
                asset_id TEXT NOT NULL, asset_version INTEGER NOT NULL,
                example_index INTEGER NOT NULL, stdin_text TEXT NOT NULL,
                expected_output_text TEXT NOT NULL, content_hash TEXT NOT NULL,
                visibility TEXT NOT NULL CHECK(visibility='public'), created_at TEXT NOT NULL,
                UNIQUE(problem_id, problem_version, example_index),
                FOREIGN KEY(problem_id,problem_version) REFERENCES problem_versions(problem_id,version),
                FOREIGN KEY(asset_id,asset_version) REFERENCES verifier_assets(asset_id,asset_version)
            )""")

    def register_examples(self, problem_id: str, problem_version: int) -> list[dict[str, Any]]:
        problem_record = ProblemRepository(self.path).get(problem_id, problem_version)
        if problem_record["lifecycle"]["status"] != "frozen":
            raise StorageFailure("PUBLIC_TEST_PROBLEM_NOT_FROZEN", "Public tests require a frozen problem version.", 409, [])
        examples = problem_record["problemPackage"]["examples"]
        for index, example in enumerate(examples):
            self._check_size(example["input"], f"examples.{index}.input")
            self._check_size(example["output"], f"examples.{index}.output")

        assets = VerifierAssetRepository(self.path, clock=self.clock, transaction_guard=self.transaction_guard)
        try:
            asset = assets.get(problem_id, problem_version)
        except StorageFailure as exc:
            if exc.code != "ASSET_NOT_FOUND":
                raise
            asset = assets.create_missing(problem_id, problem_version)
        if asset["status"] == "missing":
            asset = assets.start_build(problem_id, problem_version)
        if asset["status"] != "building":
            raise StorageFailure("PUBLIC_TEST_ASSET_NOT_BUILDING", "Public tests can only be registered while the asset is building.", 409, [])

        now = timestamp(self.clock())
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.transaction_guard(db)
            for index, example in enumerate(examples):
                digest = self._content_hash(problem_id, problem_version, index, example)
                row = db.execute("SELECT content_hash FROM public_test_cases WHERE problem_id=? AND problem_version=? AND example_index=?",
                                 (problem_id, problem_version, index)).fetchone()
                if row is not None and row["content_hash"] != digest:
                    db.rollback()
                    raise StorageFailure("PUBLIC_TEST_SOURCE_CHANGED", "A registered example changed without a new problem version.", 409, [])
                if row is None:
                    db.execute("INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (self.id_factory(), "1.0.0", problem_id, problem_version,
                                asset["assetId"], asset["assetVersion"], index,
                                example["input"], example["output"], digest, "public", now))
            db.commit()
        return self.list_for_problem(problem_id, problem_version)

    def list_for_problem(self, problem_id: str, problem_version: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM public_test_cases WHERE problem_id=? AND problem_version=? ORDER BY example_index",
                              (problem_id, problem_version)).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _check_size(text: str, field: str) -> None:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise StorageFailure("PUBLIC_TEST_TEXT_TOO_LARGE", "A public sample exceeds the runner byte limit.", 422,
                                 [{"field": field, "code": "tooLarge", "message": "UTF-8 content exceeds 1 MiB."}])

    @staticmethod
    def _content_hash(problem_id: str, version: int, index: int, example: dict[str, str]) -> str:
        value = json.dumps({"problemId": problem_id, "problemVersion": version, "exampleIndex": index,
                            "stdin": example["input"], "expectedOutput": example["output"]},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        result = {"testCaseId": row["test_case_id"], "schemaVersion": row["schema_version"],
                  "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
                  "verifierAssetRef": {"assetId": row["asset_id"], "assetVersion": row["asset_version"]},
                  "exampleIndex": row["example_index"], "stdin": row["stdin_text"],
                  "expectedOutput": row["expected_output_text"], "contentHash": row["content_hash"],
                  "visibility": row["visibility"], "createdAt": row["created_at"]}
        validate("PublicTestCase", result)
        return result

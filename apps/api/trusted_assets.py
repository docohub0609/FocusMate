"""Internal construction and persistence of private trusted-test material."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Protocol
from uuid import uuid4

from jsonschema import ValidationError

from .problem_intake import timestamp, utc_now
from .problem_storage import ProblemRepository, StorageFailure
from .public_tests import MAX_TEXT_BYTES, PublicTestCaseRepository
from .schema_validation import validate
from .verifier_assets import VerifierAssetRepository


ORACLE_SOURCE_MAX_BYTES = 256 * 1024
MATERIAL_MAX_BYTES = 16 * 1024 * 1024
COMPARISON_POLICY = {
    "normalizeLineEndings": True,
    "ignoreOneFinalNewline": True,
    "caseSensitive": True,
    "otherWhitespaceSensitive": True,
}


@dataclass(frozen=True)
class OracleEvaluationFailure(Exception):
    code: str
    message: str


class OracleEvaluator(Protocol):
    def evaluate(self, source_code: str, inputs: tuple[bytes, ...]) -> tuple[bytes, ...]: ...


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class TrustedAssetBuilder:
    """Trusted internal boundary. No browser route exposes this service or its records."""

    def __init__(
        self,
        path: str | Path,
        *,
        evaluator: OracleEvaluator | None = None,
        clock: Callable[[], datetime] = utc_now,
        test_id_factory: Callable[[], str] = lambda: f"private-test-{uuid4()}",
        oracle_id_factory: Callable[[], str] = lambda: f"oracle-{uuid4()}",
        transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.evaluator = evaluator
        self.clock = clock
        self.test_id_factory = test_id_factory
        self.oracle_id_factory = oracle_id_factory
        self.transaction_guard = transaction_guard or (lambda db: None)
        ProblemRepository(self.path)
        VerifierAssetRepository(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reference_oracles (
                    oracle_id TEXT NOT NULL, oracle_version INTEGER NOT NULL,
                    schema_version TEXT NOT NULL, problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL, asset_id TEXT NOT NULL,
                    asset_version INTEGER NOT NULL, language TEXT NOT NULL,
                    source_code TEXT NOT NULL, content_hash TEXT NOT NULL,
                    origin TEXT NOT NULL, resource_policy_ref TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(oracle_id,oracle_version),
                    UNIQUE(asset_id,asset_version),
                    FOREIGN KEY(asset_id,asset_version) REFERENCES verifier_assets(asset_id,asset_version)
                );
                CREATE TABLE IF NOT EXISTS private_test_cases (
                    test_case_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
                    problem_id TEXT NOT NULL, problem_version INTEGER NOT NULL,
                    asset_id TEXT NOT NULL, asset_version INTEGER NOT NULL,
                    case_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    stdin_text TEXT NOT NULL, expected_output_text TEXT NOT NULL,
                    answer_source TEXT NOT NULL CHECK(answer_source IN ('expectedOutput','referenceOracle')),
                    oracle_id TEXT, oracle_version INTEGER, oracle_code_hash TEXT, content_hash TEXT NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility='private'), created_at TEXT NOT NULL,
                    UNIQUE(asset_id,asset_version,case_key),
                    UNIQUE(asset_id,asset_version,ordinal),
                    FOREIGN KEY(asset_id,asset_version) REFERENCES verifier_assets(asset_id,asset_version),
                    FOREIGN KEY(oracle_id,oracle_version) REFERENCES reference_oracles(oracle_id,oracle_version)
                );
                CREATE TABLE IF NOT EXISTS trusted_test_materials (
                    asset_id TEXT NOT NULL, asset_version INTEGER NOT NULL,
                    schema_version TEXT NOT NULL, problem_id TEXT NOT NULL,
                    problem_version INTEGER NOT NULL, public_test_count INTEGER NOT NULL,
                    private_test_count INTEGER NOT NULL, test_count INTEGER NOT NULL,
                    build_method TEXT NOT NULL, material_hash TEXT NOT NULL,
                    material_hash_version TEXT NOT NULL, resource_policy_ref TEXT NOT NULL,
                    comparison_policy_json TEXT NOT NULL, manifest_id TEXT NOT NULL UNIQUE,
                    manifest_hash TEXT NOT NULL, sealed_at TEXT NOT NULL,
                    PRIMARY KEY(asset_id,asset_version),
                    UNIQUE(problem_id,problem_version),
                    FOREIGN KEY(asset_id,asset_version) REFERENCES verifier_assets(asset_id,asset_version)
                );
            """)

    def build(self, manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            validate("TrustedAssetBuildManifest", manifest)
        except ValidationError as exc:
            field = ".".join(str(item) for item in exc.absolute_path) or "$"
            raise StorageFailure(
                "TRUSTED_MANIFEST_INVALID",
                "The trusted-build manifest does not match its schema.",
                422,
                [{"field": field, "code": "schemaInvalid", "message": "The field has an invalid shape or value."}],
            ) from exc
        self._validate_manifest_semantics(manifest)
        problem_id = manifest["problemRef"]["problemId"]
        problem_version = manifest["problemRef"]["problemVersion"]
        record = ProblemRepository(self.path).get(problem_id, problem_version)
        if record["lifecycle"]["status"] != "frozen":
            raise StorageFailure("TRUSTED_MATERIAL_PROBLEM_NOT_FROZEN", "Trusted tests require a frozen problem version.", 409, [])

        manifest_hash = _sha256(_canonical(manifest))
        existing = self._find_material(manifest["manifestId"], problem_id, problem_version)
        if existing is not None:
            if existing["manifest_hash"] != manifest_hash:
                raise StorageFailure("TRUSTED_MATERIAL_ALREADY_SEALED", "Trusted material is already sealed for this problem version.", 409, [])
            material = self._material_record(existing)
            self._advance_asset_if_needed(material)
            return material

        public_repository = PublicTestCaseRepository(self.path, clock=self.clock, transaction_guard=self.transaction_guard)
        public_cases = public_repository.list_for_problem(problem_id, problem_version)
        if not public_cases:
            public_cases = public_repository.register_examples(problem_id, problem_version)
        asset = VerifierAssetRepository(self.path, clock=self.clock).get(problem_id, problem_version)
        if asset["status"] != "building":
            concurrent = self._find_material(manifest["manifestId"], problem_id, problem_version)
            if concurrent is not None:
                if concurrent["manifest_hash"] != manifest_hash:
                    raise StorageFailure(
                        "TRUSTED_MATERIAL_ALREADY_SEALED",
                        "Trusted material is already sealed for this problem version.",
                        409,
                        [],
                    )
                concurrent_material = self._material_record(concurrent)
                self._advance_asset_if_needed(concurrent_material)
                return concurrent_material
            raise StorageFailure("TRUSTED_MATERIAL_ASSET_NOT_BUILDING", "Trusted material can only be sealed while the asset is building.", 409, [])

        oracle_cases = [item for item in manifest["privateTests"] if item["answerSource"] == "referenceOracle"]
        oracle: dict[str, Any] | None = None
        oracle_outputs: dict[str, str] = {}
        if oracle_cases:
            if self.evaluator is None:
                raise OracleEvaluationFailure("ORACLE_EVALUATOR_UNAVAILABLE", "The internal Oracle evaluator is not configured.")
            source = manifest["referenceOracle"]["sourceCode"]
            if len(source.encode("utf-8")) > ORACLE_SOURCE_MAX_BYTES:
                raise StorageFailure("ORACLE_SOURCE_TOO_LARGE", "The reference Oracle exceeds 256 KiB.", 422, [])
            inputs = tuple(item["stdin"].encode("utf-8") for item in oracle_cases)
            outputs = self.evaluator.evaluate(source, inputs)
            if len(outputs) != len(inputs):
                raise OracleEvaluationFailure("ORACLE_RESULT_COUNT_INVALID", "The Oracle returned an unexpected number of outputs.")
            oracle_id = self.oracle_id_factory()
            oracle = self._oracle_record(asset, manifest["referenceOracle"], oracle_id, timestamp(self.clock()))
            for item, output in zip(oracle_cases, outputs, strict=True):
                if len(output) > MAX_TEXT_BYTES:
                    raise OracleEvaluationFailure("ORACLE_OUTPUT_TOO_LARGE", "An Oracle output exceeds the runner byte limit.")
                try:
                    oracle_outputs[item["caseKey"]] = output.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise OracleEvaluationFailure("ORACLE_OUTPUT_NOT_UTF8", "An Oracle output is not valid UTF-8.") from exc

        now = timestamp(self.clock())
        private_records = self._private_records(manifest, asset, oracle, oracle_outputs, now)
        self._validate_sealed_size(public_cases, private_records, oracle)
        material_hash = self._material_hash(asset, public_cases, private_records, oracle)
        sources = {item["answerSource"]["type"] for item in private_records}
        source_method = {"expectedOutput": "expectedOutputs", "referenceOracle": "referenceOracle"}
        build_method = source_method[next(iter(sources))] if len(sources) == 1 else "hybrid"
        material = {
            "schemaVersion": "1.0.0", "problemRef": asset["problemRef"],
            "verifierAssetRef": {"assetId": asset["assetId"], "assetVersion": asset["assetVersion"]},
            "publicTestCount": len(public_cases), "privateTestCount": len(private_records),
            "testCount": len(public_cases) + len(private_records), "buildMethod": build_method,
            "materialHashVersion": "material-v1", "materialHash": material_hash,
            "resourcePolicyRef": "python-default-v1", "comparisonPolicy": COMPARISON_POLICY,
            "sealedAt": now,
        }
        validate("TrustedTestMaterial", material)
        concurrent_material: dict[str, Any] | None = None
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self.transaction_guard(db)
                concurrent = db.execute(
                    "SELECT * FROM trusted_test_materials WHERE problem_id=? AND problem_version=?",
                    (problem_id, problem_version),
                ).fetchone()
                if concurrent is not None:
                    if concurrent["manifest_hash"] != manifest_hash:
                        raise StorageFailure(
                            "TRUSTED_MATERIAL_ALREADY_SEALED",
                            "Trusted material is already sealed for this problem version.",
                            409,
                            [],
                        )
                    concurrent_material = self._material_record(concurrent)
                    db.rollback()
                else:
                    current = db.execute(
                        "SELECT status FROM verifier_assets WHERE asset_id=? AND asset_version=?",
                        (asset["assetId"], asset["assetVersion"]),
                    ).fetchone()
                    if current is None or current["status"] != "building":
                        raise StorageFailure(
                            "TRUSTED_MATERIAL_ASSET_NOT_BUILDING",
                            "The asset changed while material was being built.", 409, [],
                        )
                    if oracle is not None:
                        self._insert_oracle(db, oracle)
                    for private in private_records:
                        self._insert_private(db, private)
                    db.execute("""INSERT INTO trusted_test_materials(
                        asset_id,asset_version,schema_version,problem_id,problem_version,
                        public_test_count,private_test_count,test_count,build_method,material_hash,
                        material_hash_version,resource_policy_ref,comparison_policy_json,
                        manifest_id,manifest_hash,sealed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        asset["assetId"], asset["assetVersion"], "1.0.0", problem_id,
                        problem_version, len(public_cases), len(private_records),
                        len(public_cases) + len(private_records), build_method, material_hash,
                        "material-v1", "python-default-v1", json.dumps(COMPARISON_POLICY, separators=(",", ":")),
                        manifest["manifestId"], manifest_hash, now,
                    ))
                    self.transaction_guard(db)
                    db.commit()
            except Exception:
                db.rollback()
                raise
        if concurrent_material is not None:
            self._advance_asset_if_needed(concurrent_material)
            return concurrent_material
        self._advance_asset_if_needed(material)
        return material

    def list_private(self, problem_id: str, problem_version: int) -> list[dict[str, Any]]:
        """Internal-only read used by the trusted builder and tests; never expose via HTTP."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM private_test_cases WHERE problem_id=? AND problem_version=? ORDER BY ordinal",
                              (problem_id, problem_version)).fetchall()
        return [self._private_record(row) for row in rows]

    def get_oracle(self, problem_id: str, problem_version: int) -> dict[str, Any] | None:
        """Internal-only read; the returned source code is secret material."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM reference_oracles WHERE problem_id=? AND problem_version=?",
                             (problem_id, problem_version)).fetchone()
        return None if row is None else self._oracle_row_record(row)

    def _advance_asset_if_needed(self, material: dict[str, Any]) -> None:
        ref = material["problemRef"]
        assets = VerifierAssetRepository(self.path, clock=self.clock, transaction_guard=self.transaction_guard)
        asset = assets.get(ref["problemId"], ref["problemVersion"])
        if asset["status"] == "building":
            try:
                assets.submit_material(ref["problemId"], ref["problemVersion"], {
                    "testCount": material["testCount"], "buildMethod": material["buildMethod"],
                    "materialHash": material["materialHash"],
                })
            except StorageFailure as exc:
                if exc.code == "BUILD_JOB_CLAIM_LOST":
                    raise
                latest = assets.get(ref["problemId"], ref["problemVersion"])
                if latest["status"] not in {"crossChecking", "ready"}:
                    raise exc
        elif asset["status"] not in {"crossChecking", "ready"}:
            raise StorageFailure("TRUSTED_MATERIAL_ASSET_STATE_INVALID", "The asset cannot accept sealed material in its current state.", 409, [])

    def _find_material(self, manifest_id: str, problem_id: str, version: int) -> sqlite3.Row | None:
        with closing(self._connect()) as db:
            by_manifest = db.execute("SELECT * FROM trusted_test_materials WHERE manifest_id=?", (manifest_id,)).fetchone()
            if by_manifest is not None:
                if (by_manifest["problem_id"], by_manifest["problem_version"]) != (problem_id, version):
                    raise StorageFailure("TRUSTED_MANIFEST_CONTEXT_CONFLICT", "This manifest ID belongs to another problem version.", 409, [])
                return by_manifest
            return db.execute("SELECT * FROM trusted_test_materials WHERE problem_id=? AND problem_version=?",
                              (problem_id, version)).fetchone()

    @staticmethod
    def _validate_manifest_semantics(manifest: dict[str, Any]) -> None:
        keys = [item["caseKey"] for item in manifest["privateTests"]]
        if any(not key.strip() for key in keys) or len(keys) != len(set(keys)):
            raise StorageFailure("PRIVATE_TEST_KEY_INVALID", "Private test keys must be unique and non-blank.", 422, [])
        uses_oracle = any(item["answerSource"] == "referenceOracle" for item in manifest["privateTests"])
        if uses_oracle != ("referenceOracle" in manifest):
            raise StorageFailure("REFERENCE_ORACLE_CONTEXT_INVALID", "A reference Oracle is required exactly when a private test uses it.", 422, [])
        stdin_values = [item["stdin"] for item in manifest["privateTests"]]
        if len(stdin_values) != len(set(stdin_values)):
            raise StorageFailure("PRIVATE_TEST_INPUT_DUPLICATE", "Private test inputs must be unique within a manifest.", 422, [])
        total_bytes = 0
        for index, item in enumerate(manifest["privateTests"]):
            TrustedAssetBuilder._check_text_size(item["stdin"], f"privateTests.{index}.stdin")
            total_bytes += len(item["stdin"].encode("utf-8"))
            if item["answerSource"] == "expectedOutput":
                TrustedAssetBuilder._check_text_size(item["expectedOutput"], f"privateTests.{index}.expectedOutput")
                total_bytes += len(item["expectedOutput"].encode("utf-8"))
        if total_bytes > MATERIAL_MAX_BYTES:
            raise StorageFailure("TRUSTED_MATERIAL_TOO_LARGE", "The private-test manifest exceeds 16 MiB.", 422, [])

    @staticmethod
    def _check_text_size(value: str, field: str) -> None:
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise StorageFailure("TRUSTED_MATERIAL_TEXT_TOO_LARGE", "Trusted material exceeds the runner byte limit.", 422,
                                 [{"field": field, "code": "tooLarge", "message": "UTF-8 content exceeds 1 MiB."}])

    @staticmethod
    def _validate_sealed_size(
        public: list[dict[str, Any]],
        private: list[dict[str, Any]],
        oracle: dict[str, Any] | None,
    ) -> None:
        total = sum(
            len(item["stdin"].encode("utf-8"))
            + len(item["expectedOutput"].encode("utf-8"))
            for item in [*public, *private]
        )
        if oracle is not None:
            total += len(oracle["sourceCode"].encode("utf-8"))
        if total > MATERIAL_MAX_BYTES:
            raise StorageFailure(
                "TRUSTED_MATERIAL_TOO_LARGE",
                "The sealed trusted-test material exceeds 16 MiB.",
                422,
                [],
            )

    @staticmethod
    def _oracle_record(asset: dict[str, Any], source: dict[str, Any], oracle_id: str, now: str) -> dict[str, Any]:
        source_code = source["sourceCode"]
        result = {"oracleId": oracle_id, "schemaVersion": "1.0.0", "oracleVersion": 1,
                  "problemRef": asset["problemRef"],
                  "verifierAssetRef": {"assetId": asset["assetId"], "assetVersion": asset["assetVersion"]},
                  "language": "python3", "sourceCode": source_code,
                  "contentHash": _sha256(source_code.encode("utf-8")), "origin": source["origin"],
                  "resourcePolicyRef": "python-default-v1", "createdAt": now}
        validate("ReferenceOracle", result)
        return result

    def _private_records(self, manifest: dict[str, Any], asset: dict[str, Any], oracle: dict[str, Any] | None,
                         oracle_outputs: dict[str, str], now: str) -> list[dict[str, Any]]:
        records = []
        for ordinal, item in enumerate(manifest["privateTests"]):
            source = item["answerSource"]
            expected = item["expectedOutput"] if source == "expectedOutput" else oracle_outputs[item["caseKey"]]
            answer: dict[str, Any] = {"type": source}
            if source == "referenceOracle":
                assert oracle is not None
                answer["oracleRef"] = {"oracleId": oracle["oracleId"], "oracleVersion": 1,
                                       "codeHash": oracle["contentHash"]}
            semantic_answer = {"type": source}
            if source == "referenceOracle":
                semantic_answer["oracleRef"] = {"oracleVersion": 1, "codeHash": oracle["contentHash"]}
            digest = _sha256(_canonical({"problemRef": asset["problemRef"], "assetVersion": asset["assetVersion"],
                                         "caseKey": item["caseKey"],
                                         "ordinal": ordinal, "stdin": item["stdin"],
                                         "expectedOutput": expected, "answerSource": semantic_answer}))
            record = {"testCaseId": self.test_id_factory(), "schemaVersion": "1.0.0",
                      "problemRef": asset["problemRef"],
                      "verifierAssetRef": {"assetId": asset["assetId"], "assetVersion": asset["assetVersion"]},
                      "caseKey": item["caseKey"], "ordinal": ordinal, "stdin": item["stdin"],
                      "expectedOutput": expected, "answerSource": answer, "contentHash": digest,
                      "visibility": "private", "createdAt": now}
            validate("PrivateTestCase", record)
            records.append(record)
        return records

    @staticmethod
    def _material_hash(asset: dict[str, Any], public: list[dict[str, Any]], private: list[dict[str, Any]],
                       oracle: dict[str, Any] | None) -> str:
        value = {"materialHashVersion": "material-v1", "problemRef": asset["problemRef"],
                 "assetVersion": asset["assetVersion"], "resourcePolicyRef": "python-default-v1",
                 "comparisonPolicy": COMPARISON_POLICY,
                 "publicTests": [{"index": x["exampleIndex"], "stdin": x["stdin"],
                                  "expectedOutput": x["expectedOutput"], "contentHash": x["contentHash"]} for x in public],
                 "privateTests": [{"caseKey": x["caseKey"], "ordinal": x["ordinal"], "stdin": x["stdin"],
                                   "expectedOutput": x["expectedOutput"],
                                   "answerSource": TrustedAssetBuilder._semantic_answer(x["answerSource"]),
                                   "contentHash": x["contentHash"]} for x in private],
                 "oracleContentHash": None if oracle is None else oracle["contentHash"]}
        return _sha256(_canonical(value))

    @staticmethod
    def _insert_oracle(db: sqlite3.Connection, item: dict[str, Any]) -> None:
        db.execute("""INSERT INTO reference_oracles(
            oracle_id,oracle_version,schema_version,problem_id,problem_version,asset_id,
            asset_version,language,source_code,content_hash,origin,resource_policy_ref,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            item["oracleId"], item["oracleVersion"], item["schemaVersion"], item["problemRef"]["problemId"],
            item["problemRef"]["problemVersion"], item["verifierAssetRef"]["assetId"],
            item["verifierAssetRef"]["assetVersion"], item["language"], item["sourceCode"],
            item["contentHash"], item["origin"], item["resourcePolicyRef"], item["createdAt"],
        ))

    @staticmethod
    def _insert_private(db: sqlite3.Connection, item: dict[str, Any]) -> None:
        oracle_ref = item["answerSource"].get("oracleRef")
        db.execute("""INSERT INTO private_test_cases(
            test_case_id,schema_version,problem_id,problem_version,asset_id,asset_version,
            case_key,ordinal,stdin_text,expected_output_text,answer_source,oracle_id,
            oracle_version,oracle_code_hash,content_hash,visibility,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            item["testCaseId"], item["schemaVersion"], item["problemRef"]["problemId"],
            item["problemRef"]["problemVersion"], item["verifierAssetRef"]["assetId"],
            item["verifierAssetRef"]["assetVersion"], item["caseKey"], item["ordinal"], item["stdin"],
            item["expectedOutput"], item["answerSource"]["type"],
            None if oracle_ref is None else oracle_ref["oracleId"],
            None if oracle_ref is None else oracle_ref["oracleVersion"],
            None if oracle_ref is None else oracle_ref["codeHash"], item["contentHash"],
            item["visibility"], item["createdAt"],
        ))

    @staticmethod
    def _material_record(row: sqlite3.Row) -> dict[str, Any]:
        result = {"schemaVersion": row["schema_version"],
                  "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
                  "verifierAssetRef": {"assetId": row["asset_id"], "assetVersion": row["asset_version"]},
                  "publicTestCount": row["public_test_count"], "privateTestCount": row["private_test_count"],
                  "testCount": row["test_count"], "buildMethod": row["build_method"],
                  "materialHashVersion": row["material_hash_version"], "materialHash": row["material_hash"],
                  "resourcePolicyRef": row["resource_policy_ref"],
                  "comparisonPolicy": json.loads(row["comparison_policy_json"]), "sealedAt": row["sealed_at"]}
        validate("TrustedTestMaterial", result)
        return result

    @staticmethod
    def _private_record(row: sqlite3.Row) -> dict[str, Any]:
        answer: dict[str, Any] = {"type": row["answer_source"]}
        if row["oracle_id"] is not None:
            answer["oracleRef"] = {"oracleId": row["oracle_id"], "oracleVersion": row["oracle_version"],
                                   "codeHash": row["oracle_code_hash"]}
        result = {"testCaseId": row["test_case_id"], "schemaVersion": row["schema_version"],
                  "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
                  "verifierAssetRef": {"assetId": row["asset_id"], "assetVersion": row["asset_version"]},
                  "caseKey": row["case_key"], "ordinal": row["ordinal"], "stdin": row["stdin_text"],
                  "expectedOutput": row["expected_output_text"], "answerSource": answer,
                  "contentHash": row["content_hash"], "visibility": row["visibility"], "createdAt": row["created_at"]}
        validate("PrivateTestCase", result)
        return result

    @staticmethod
    def _oracle_row_record(row: sqlite3.Row) -> dict[str, Any]:
        result = {"oracleId": row["oracle_id"], "schemaVersion": row["schema_version"],
                  "oracleVersion": row["oracle_version"],
                  "problemRef": {"problemId": row["problem_id"], "problemVersion": row["problem_version"]},
                  "verifierAssetRef": {"assetId": row["asset_id"], "assetVersion": row["asset_version"]},
                  "language": row["language"], "sourceCode": row["source_code"],
                  "contentHash": row["content_hash"], "origin": row["origin"],
                  "resourcePolicyRef": row["resource_policy_ref"],
                  "createdAt": row["created_at"]}
        validate("ReferenceOracle", result)
        return result

    @staticmethod
    def _semantic_answer(answer: dict[str, Any]) -> dict[str, Any]:
        if answer["type"] == "expectedOutput":
            return {"type": "expectedOutput"}
        oracle = answer["oracleRef"]
        return {"type": "referenceOracle",
                "oracleRef": {"oracleVersion": oracle["oracleVersion"], "codeHash": oracle["codeHash"]}}

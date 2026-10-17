"""Load durable trusted tests only after independently checking their seal."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from jsonschema.exceptions import ValidationError

from src.focusmate.contracts.schema_validation import validate

from .judge import JudgeFailure, TrustedTestBundle, TrustedTestCase


MATERIAL_HASH_VERSION = "material-v1"
RESOURCE_POLICY_REF = "python-default-v1"
COMPARISON_POLICY = {
    "normalizeLineEndings": True,
    "ignoreOneFinalNewline": True,
    "caseSensitive": True,
    "otherWhitespaceSensitive": True,
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class SQLiteTrustedAssetProvider:
    """Judge-side reader; hidden inputs and outputs never cross an HTTP boundary."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self, problem_id: str, problem_version: int) -> TrustedTestBundle:
        try:
            rows = self._load(problem_id, problem_version)
            return self._verified_bundle(problem_id, problem_version, **rows)
        except JudgeFailure:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Never echo SQLite data here: it may contain private input, expected
            # output, or the reference implementation.
            raise JudgeFailure(
                "Trusted tests could not be verified for this problem version."
            ) from exc

    def get_on_connection(self, db, problem_id: str, problem_version: int) -> TrustedTestBundle:
        """Validate inside the caller's transaction; never open another connection."""
        try:
            return self._verified_bundle(problem_id, problem_version,
                **self._load_on_connection(db, problem_id, problem_version))
        except JudgeFailure:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise JudgeFailure("Trusted tests could not be verified.") from exc

    def _load(self, problem_id: str, problem_version: int) -> dict[str, Any]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            return self._load_on_connection(db, problem_id, problem_version)

    def _load_on_connection(self, db, problem_id, problem_version):
        problem = db.execute(
            """SELECT lifecycle_status,verifier_asset_status,content_hash,confirmed_feature_set_id
               FROM problem_versions WHERE problem_id=? AND version=?""",
            (problem_id, problem_version),
        ).fetchone()
        asset = db.execute(
            "SELECT * FROM verifier_assets WHERE problem_id=? AND problem_version=?",
            (problem_id, problem_version),
        ).fetchone()
        if problem is None or asset is None:
            raise JudgeFailure("Trusted tests are unavailable for this problem version.")
        material = db.execute(
            "SELECT * FROM trusted_test_materials WHERE asset_id=? AND asset_version=?",
            (asset["asset_id"], asset["asset_version"]),
        ).fetchone()
        public = db.execute(
            """SELECT * FROM public_test_cases
               WHERE asset_id=? AND asset_version=? ORDER BY example_index""",
            (asset["asset_id"], asset["asset_version"]),
        ).fetchall()
        private = db.execute(
            """SELECT * FROM private_test_cases
               WHERE asset_id=? AND asset_version=? ORDER BY ordinal""",
            (asset["asset_id"], asset["asset_version"]),
        ).fetchall()
        oracles = db.execute(
            """SELECT *
               FROM reference_oracles
               WHERE asset_id=? AND asset_version=?""",
            (asset["asset_id"], asset["asset_version"]),
        ).fetchall()
        automatic_evidence = None
        if any(row["origin"] == "generatedCandidate" for row in oracles):
            automatic_evidence = db.execute(
                """SELECT c.candidate_json,c.prechecked_json,r.record_json FROM verifier_build_jobs j
                   JOIN verifier_candidate_sets c ON c.job_id=j.job_id
                   JOIN verifier_cross_check_runs r ON r.job_id=j.job_id
                   WHERE j.problem_id=? AND j.problem_version=? AND r.run_id=?""",
                (problem_id, problem_version,
                 json.loads(asset["cross_check_report_json"] or "{}").get("reportId")),
            ).fetchone()
        return {
            "problem": problem, "asset": asset, "material": material,
            "public": public, "private": private, "oracles": oracles,
            "automatic_evidence": automatic_evidence,
        }

    def _verified_bundle(
        self,
        problem_id: str,
        problem_version: int,
        *,
        problem: sqlite3.Row,
        asset: sqlite3.Row,
        material: sqlite3.Row | None,
        public: list[sqlite3.Row],
        private: list[sqlite3.Row],
        oracles: list[sqlite3.Row],
        automatic_evidence: sqlite3.Row | None = None,
    ) -> TrustedTestBundle:
        if (
            problem["lifecycle_status"] != "frozen"
            or problem["verifier_asset_status"] != "ready"
            or asset["status"] != "ready"
        ):
            raise JudgeFailure("Trusted tests are not ready for this problem version.")
        if (
            material is None
            or not asset["material_summary_json"]
            or not asset["cross_check_report_json"]
        ):
            raise JudgeFailure("The ready asset is missing its verification records.")
        if (
            material["problem_id"] != problem_id
            or material["problem_version"] != problem_version
            or material["asset_id"] != asset["asset_id"]
            or material["asset_version"] != asset["asset_version"]
        ):
            raise JudgeFailure("The trusted-test material belongs to another context.")

        summary = json.loads(asset["material_summary_json"])
        report = json.loads(asset["cross_check_report_json"])
        comparison_policy = json.loads(material["comparison_policy_json"])
        checks_run = report.get("checksRun")
        if (
            report.get("outcome") != "passed"
            or not isinstance(report.get("reportId"), str)
            or not report["reportId"].strip()
            or not isinstance(checks_run, list)
            or not checks_run
            or any(not isinstance(check, str) or not check.strip() for check in checks_run)
            or report.get("checkedMaterialHash") != material["material_hash"]
            or summary.get("materialHash") != material["material_hash"]
            or summary.get("testCount") != material["test_count"]
            or summary.get("buildMethod") != material["build_method"]
        ):
            raise JudgeFailure("The trusted-test report does not match the sealed material.")
        if (
            material["material_hash_version"] != MATERIAL_HASH_VERSION
            or material["resource_policy_ref"] != RESOURCE_POLICY_REF
            or comparison_policy != COMPARISON_POLICY
            or material["build_method"] not in {"expectedOutputs", "referenceOracle", "hybrid"}
        ):
            raise JudgeFailure("The trusted-test policy is unsupported or incomplete.")

        expected_counts = (
            material["public_test_count"], material["private_test_count"],
            material["test_count"],
        )
        actual_counts = (len(public), len(private), len(public) + len(private))
        if not public or not private or expected_counts != actual_counts:
            raise JudgeFailure("The sealed trusted-test counts do not match their records.")
        answer_sources = {row["answer_source"] for row in private}
        methods = {"expectedOutput": "expectedOutputs", "referenceOracle": "referenceOracle"}
        if not answer_sources.issubset(methods):
            raise JudgeFailure("The sealed trusted-test answer sources are unsupported.")
        derived_method = (
            methods[next(iter(answer_sources))] if len(answer_sources) == 1 else "hybrid"
        )
        if material["build_method"] != derived_method:
            raise JudgeFailure("The trusted-test build method does not match its cases.")

        self._verify_case_hashes(problem_id, problem_version, asset, public, private, oracles)
        # The original manifest seal includes Oracle origin, which material-v1
        # intentionally omitted. Verify it before choosing the report policy.
        self._verify_manifest(material, private, oracles)
        self._verify_automatic_report(report, private, oracles)
        if any(row["origin"] == "generatedCandidate" for row in oracles):
            self._verify_automatic_evidence(report, automatic_evidence, problem_id, problem_version,
                                            problem["content_hash"], problem["confirmed_feature_set_id"])
        calculated = self._material_hash(asset, public, private, oracles)
        if calculated != material["material_hash"]:
            raise JudgeFailure("The trusted-test material seal does not match its records.")

        cases = tuple(
            [
                TrustedTestCase(
                    row["test_case_id"], row["stdin_text"].encode("utf-8"),
                    row["expected_output_text"].encode("utf-8"),
                )
                for row in public
            ]
            + [
                TrustedTestCase(
                    row["test_case_id"], row["stdin_text"].encode("utf-8"),
                    row["expected_output_text"].encode("utf-8"),
                )
                for row in private
            ]
        )
        return TrustedTestBundle(
            asset["asset_id"], asset["asset_version"], problem_id,
            problem_version, "ready", cases, assurance=report.get("assurance", "full"),
        )

    @staticmethod
    def _verify_manifest(material, private, oracles):
        """Reconstruct the exact accepted T032 input, preserving bytes and order."""
        manifest = {
            "manifestId": material["manifest_id"], "schemaVersion": material["schema_version"],
            "problemRef": {"problemId": material["problem_id"], "problemVersion": material["problem_version"]},
            "privateTests": [],
        }
        for row in private:
            case = {"caseKey": row["case_key"], "stdin": row["stdin_text"], "answerSource": row["answer_source"]}
            if row["answer_source"] == "expectedOutput":
                case["expectedOutput"] = row["expected_output_text"]
            manifest["privateTests"].append(case)
        if oracles:
            oracle = oracles[0]
            manifest["referenceOracle"] = {
                "language": oracle["language"], "sourceCode": oracle["source_code"],
                "origin": oracle["origin"], "resourcePolicyRef": oracle["resource_policy_ref"],
            }
        if _sha256(_canonical(manifest)) != material["manifest_hash"]:
            raise JudgeFailure("The trusted-test provenance does not match its sealed manifest.")

    @staticmethod
    def _verify_automatic_evidence(report, evidence, problem_id, problem_version, problem_hash, feature_set_id):
        try:
            if evidence is None: raise ValueError("missing run")
            candidate = json.loads(evidence["candidate_json"])
            checked = json.loads(evidence["prechecked_json"])
            run = json.loads(evidence["record_json"])
            validate("VerifierCrossCheckRun", run)
            expected = {key: value for key, value in run.items()
                        if key not in {"runId", "schemaVersion", "problemRef", "featureSetId"}}
            if (run["runId"] != report["reportId"]
                    or run["problemRef"] != {"problemId": problem_id, "problemVersion": problem_version}
                    or candidate["problem_id"] != problem_id or candidate["problem_version"] != problem_version
                    or candidate["problem_content_hash"] != problem_hash
                    or checked["problem_id"] != problem_id or checked["problem_version"] != problem_version
                    or checked["problem_content_hash"] != problem_hash
                    or checked["feature_set_id"] != feature_set_id or candidate["feature_set_id"] != feature_set_id
                    or run["featureSetId"] != candidate["feature_set_id"]
                    or any(report.get(key) != value for key, value in expected.items())):
                raise ValueError("run mismatch")
            if report["checkPolicyVersion"] in {"verifier-cross-check-v2", "verifier-cross-check-v3", "verifier-cross-check-v4"}:
                for public, stored in (("assurance", "assurance"), ("warningCodes", "warning_codes"),
                                       ("mutationRepairCount", "mutation_repair_count")):
                    if checked.get(stored) != report[public]:
                        raise ValueError("prechecked assurance mismatch")
            mutants = candidate["mutants"]
            if not isinstance(mutants, list) or len(mutants) != report["mutationAttempted"]:
                raise ValueError("mutant count mismatch")
            keys = [item["mutant_key"] for item in mutants]
            if (any(not isinstance(key, str) or not key.strip() for key in keys)
                    or len(set(keys)) != len(keys)):
                raise ValueError("invalid mutant keys")
            for field, role in (("generatorHash", "generator"), ("slowOracleHash", "slow_oracle"),
                                ("fastReferenceHash", "fast_reference")):
                if report[field] != _sha256(candidate[role]["source"].encode("utf-8")):
                    raise ValueError("artifact mismatch")
        except (ValidationError, KeyError, TypeError, ValueError):
            raise JudgeFailure("Automatic trusted tests lack matching persisted evidence.") from None

    @staticmethod
    def _verify_automatic_report(report: dict, private: list, oracles: list) -> None:
        # Historical internally supplied materials retain their T030 report shape.
        # Automatically generated sources must always carry full T033 evidence.
        automatic = any(row["origin"] == "generatedCandidate" for row in oracles)
        try:
            validate("VerifierAssetCrossCheckReport", report)
            if not automatic and "checkPolicyVersion" not in report:
                return
            checks = {"compile", "generatorDeterminism", "publicExamples", "smallDifferential",
                      "largeDeterminism", "mutation", "sealedMaterialIntegrity"}
            v2 = report["checkPolicyVersion"] in {"verifier-cross-check-v2", "verifier-cross-check-v3", "verifier-cross-check-v4"}
            limited = v2 and report["assurance"] == "limited"
            mutation_valid = (
                report["mutationEligible"] == report["mutationKilled"] == 0
                and report["mutationRepairCount"] == (0 if report['checkPolicyVersion']=='verifier-cross-check-v4' else 1)
                and report["warningCodes"] == ["MUTATION_CHALLENGE_UNAVAILABLE"]
                and 0 <= report["mutationAttempted"] <= 3
            ) if limited else (
                1 <= report["mutationEligible"] <= report["mutationAttempted"] <= 3
                and report["mutationKilled"] == report["mutationEligible"]
                and (not v2 or report["warningCodes"] == [])
            )
            if (report["smallCount"] < 8
                    or report["agreementCount"] != report["smallCount"]
                    or report["smallCount"] + report["largeCount"] != len(private)
                    or not mutation_valid
                    or report["elapsedMs"] >= (report["buildBudgetMs"] if v2 else 120000)
                    or not checks.issubset(set(report["checksRun"]))
                    or len(oracles) != 1 or report["fastReferenceHash"] != oracles[0]["content_hash"]):
                raise ValueError("cross-check relationships")
        except (ValidationError, KeyError, TypeError, ValueError):
            raise JudgeFailure("Automatic trusted tests lack matching cross-check evidence.") from None

    @staticmethod
    def _verify_case_hashes(
        problem_id: str,
        problem_version: int,
        asset: sqlite3.Row,
        public: list[sqlite3.Row],
        private: list[sqlite3.Row],
        oracles: list[sqlite3.Row],
    ) -> None:
        oracle_by_ref = {
            (row["oracle_id"], row["oracle_version"]): row for row in oracles
        }
        for row in oracles:
            if (
                row["problem_id"] != problem_id
                or row["problem_version"] != problem_version
                or row["asset_id"] != asset["asset_id"]
                or row["asset_version"] != asset["asset_version"]
                or row["language"] != "python3"
                or row["resource_policy_ref"] != RESOURCE_POLICY_REF
            ):
                raise JudgeFailure("A trusted reference implementation has invalid context.")
            if _sha256(row["source_code"].encode("utf-8")) != row["content_hash"]:
                raise JudgeFailure("A trusted reference implementation failed integrity checking.")

        for expected_index, row in enumerate(public):
            if (
                row["problem_id"] != problem_id
                or row["problem_version"] != problem_version
                or row["asset_id"] != asset["asset_id"]
                or row["asset_version"] != asset["asset_version"]
                or row["example_index"] != expected_index
                or row["visibility"] != "public"
            ):
                raise JudgeFailure("A public trusted test has invalid context.")
            value = {
                "problemId": problem_id, "problemVersion": problem_version,
                "exampleIndex": row["example_index"], "stdin": row["stdin_text"],
                "expectedOutput": row["expected_output_text"],
            }
            if _sha256(_canonical(value)) != row["content_hash"]:
                raise JudgeFailure("A public trusted test failed integrity checking.")

        referenced_oracles: set[tuple[str, int]] = set()
        for expected_ordinal, row in enumerate(private):
            if (
                row["problem_id"] != problem_id
                or row["problem_version"] != problem_version
                or row["asset_id"] != asset["asset_id"]
                or row["asset_version"] != asset["asset_version"]
                or row["ordinal"] != expected_ordinal
                or row["visibility"] != "private"
            ):
                raise JudgeFailure("A private trusted test has invalid context.")
            answer: dict[str, Any] = {"type": row["answer_source"]}
            if row["answer_source"] == "referenceOracle":
                ref = (row["oracle_id"], row["oracle_version"])
                oracle = oracle_by_ref.get(ref)
                if oracle is None or row["oracle_code_hash"] != oracle["content_hash"]:
                    raise JudgeFailure("A private trusted test has an invalid answer source.")
                referenced_oracles.add(ref)
                answer["oracleRef"] = {
                    "oracleVersion": row["oracle_version"],
                    "codeHash": row["oracle_code_hash"],
                }
            elif row["answer_source"] != "expectedOutput" or row["oracle_id"] is not None:
                raise JudgeFailure("A private trusted test has an invalid answer source.")
            value = {
                "problemRef": {
                    "problemId": problem_id, "problemVersion": problem_version,
                },
                "assetVersion": asset["asset_version"], "caseKey": row["case_key"],
                "ordinal": row["ordinal"], "stdin": row["stdin_text"],
                "expectedOutput": row["expected_output_text"], "answerSource": answer,
            }
            if _sha256(_canonical(value)) != row["content_hash"]:
                raise JudgeFailure("A private trusted test failed integrity checking.")

        if len(referenced_oracles) != len(oracles):
            raise JudgeFailure("The trusted reference implementation set is inconsistent.")

    @staticmethod
    def _material_hash(
        asset: sqlite3.Row,
        public: list[sqlite3.Row],
        private: list[sqlite3.Row],
        oracles: list[sqlite3.Row],
    ) -> str:
        private_values = []
        for row in private:
            answer: dict[str, Any] = {"type": row["answer_source"]}
            if row["answer_source"] == "referenceOracle":
                answer["oracleRef"] = {
                    "oracleVersion": row["oracle_version"],
                    "codeHash": row["oracle_code_hash"],
                }
            private_values.append({
                "caseKey": row["case_key"], "ordinal": row["ordinal"],
                "stdin": row["stdin_text"],
                "expectedOutput": row["expected_output_text"],
                "answerSource": answer, "contentHash": row["content_hash"],
            })
        value = {
            "materialHashVersion": MATERIAL_HASH_VERSION,
            "problemRef": {
                "problemId": asset["problem_id"],
                "problemVersion": asset["problem_version"],
            },
            "assetVersion": asset["asset_version"],
            "resourcePolicyRef": RESOURCE_POLICY_REF,
            "comparisonPolicy": COMPARISON_POLICY,
            "publicTests": [
                {
                    "index": row["example_index"], "stdin": row["stdin_text"],
                    "expectedOutput": row["expected_output_text"],
                    "contentHash": row["content_hash"],
                }
                for row in public
            ],
            "privateTests": private_values,
            "oracleContentHash": None if not oracles else oracles[0]["content_hash"],
        }
        return _sha256(_canonical(value))

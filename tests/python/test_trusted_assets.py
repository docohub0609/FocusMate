from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from apps.api.public_tests import PublicTestCaseRepository
from apps.api.submission_storage import SubmissionRepository
from apps.api.trusted_assets import OracleEvaluationFailure, TrustedAssetBuilder
from apps.api.verifier_assets import VerifierAssetRepository
from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary
from services.oj_worker.judge import JudgeFailure, JudgeSubmissionProcessor, OJResultRepository
from services.oj_worker.queue import WorkerQueue
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.oj_worker.worker import Worker
from tests.python.test_problem_features import INTAKE
from tests.python.test_submission_storage import frozen_problem


def expected_output_manifest(
    *,
    manifest_id: str = "manifest-expected",
    problem_id: str = "problem-submit",
    problem_version: int = 1,
) -> dict[str, object]:
    return {
        "manifestId": manifest_id,
        "schemaVersion": "1.0.0",
        "problemRef": {
            "problemId": problem_id,
            "problemVersion": problem_version,
        },
        "privateTests": [
            {
                "caseKey": "single-negative",
                "stdin": "1\n-7\n",
                "answerSource": "expectedOutput",
                "expectedOutput": "1 1\n",
            },
            {
                "caseKey": "repeated-values",
                "stdin": "4\n2 2 2 2\n",
                "answerSource": "expectedOutput",
                "expectedOutput": "1 4\n",
            },
        ],
    }


def oracle_manifest() -> dict[str, object]:
    return {
        "manifestId": "manifest-oracle",
        "schemaVersion": "1.0.0",
        "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
        "privateTests": [
            {
                "caseKey": "oracle-negative",
                "stdin": "1\n-3\n",
                "answerSource": "referenceOracle",
            },
            {
                "caseKey": "oracle-positive",
                "stdin": "3\n1 2 3\n",
                "answerSource": "referenceOracle",
            },
        ],
        "referenceOracle": {
            "language": "python3",
            "sourceCode": "import sys\nprint('1 1')\n",
            "origin": "internalProvided",
            "resourcePolicyRef": "python-default-v1",
        },
    }


def cross_check_report(material_hash: str) -> dict[str, object]:
    return {
        "reportId": "report-t032",
        "outcome": "passed",
        "checkedMaterialHash": material_hash,
        "checksRun": ["referenceAgreement", "privateCoveragePresent"],
        "createdAt": "2026-09-06T12:00:00Z",
    }


class FakeOracleEvaluator:
    def __init__(self, outputs: tuple[bytes, ...]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, tuple[bytes, ...]]] = []

    def evaluate(self, source_code: str, inputs: tuple[bytes, ...]) -> tuple[bytes, ...]:
        self.calls.append((source_code, inputs))
        return self.outputs


class FakeJudgeRunner:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = iter([b"", *outputs])

    def run(self, request: object) -> PythonRunResult:
        return PythonRunResult(
            next(self.outputs), b"", 0, "exited", False, False, False,
            ResourceSummary(1, 256 * 1024 * 1024, 1024, 1, 2, 32, 16 * 1024 * 1024),
        )


def build_expected_material(path: Path) -> tuple[dict[str, object], TrustedAssetBuilder]:
    frozen_problem(path)
    builder = TrustedAssetBuilder(path)
    return builder.build(expected_output_manifest()), builder


def mark_ready(path: Path, material: dict[str, object]) -> None:
    VerifierAssetRepository(path).complete_cross_check(
        "problem-submit",
        1,
        cross_check_report(str(material["materialHash"])),
    )


def test_expected_outputs_are_sealed_as_private_material_and_survive_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "expected.db"
    material, builder = build_expected_material(path)

    assert material["buildMethod"] == "expectedOutputs"
    assert material["publicTestCount"] == 1
    assert material["privateTestCount"] == 2
    assert material["testCount"] == 3
    assert str(material["materialHash"]).startswith("sha256:")
    assert VerifierAssetRepository(path).get("problem-submit", 1)["status"] == "crossChecking"

    private = builder.list_private("problem-submit", 1)
    assert [(case["caseKey"], case["stdin"], case["expectedOutput"]) for case in private] == [
        ("single-negative", "1\n-7\n", "1 1\n"),
        ("repeated-values", "4\n2 2 2 2\n", "1 4\n"),
    ]
    assert all(case["visibility"] == "private" for case in private)
    assert TrustedAssetBuilder(path).list_private("problem-submit", 1) == private

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM trusted_test_materials"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM private_test_cases"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='oj_results'"
        ).fetchone()[0] == 0


def test_reference_oracle_is_evaluated_once_and_outputs_are_fixed_before_sealing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oracle.db"
    frozen_problem(path)
    evaluator = FakeOracleEvaluator((b"1 1\n", "1 3\n".encode("utf-8")))
    builder = TrustedAssetBuilder(
        path,
        evaluator=evaluator,
        oracle_id_factory=lambda: "oracle-fixed",
    )

    material = builder.build(oracle_manifest())

    assert material["buildMethod"] == "referenceOracle"
    assert evaluator.calls == [(
        "import sys\nprint('1 1')\n",
        (b"1\n-3\n", b"3\n1 2 3\n"),
    )]
    oracle = builder.get_oracle("problem-submit", 1)
    assert oracle is not None
    assert oracle["oracleId"] == "oracle-fixed"
    assert oracle["origin"] == "internalProvided"
    assert oracle["sourceCode"] == "import sys\nprint('1 1')\n"
    private = builder.list_private("problem-submit", 1)
    assert [case["expectedOutput"] for case in private] == ["1 1\n", "1 3\n"]
    assert all(case["answerSource"]["oracleRef"]["oracleId"] == "oracle-fixed" for case in private)
    assert VerifierAssetRepository(path).get("problem-submit", 1)["status"] == "crossChecking"
    mark_ready(path, material)
    assert len(SQLiteTrustedAssetProvider(path).get("problem-submit", 1).cases) == 3


@pytest.mark.parametrize(("outputs", "code"), [
    ((b"only-one\n",), "ORACLE_RESULT_COUNT_INVALID"),
    ((b"ok\n", b"\xff"), "ORACLE_OUTPUT_NOT_UTF8"),
    ((b"ok\n", b"x" * (1024 * 1024 + 1)), "ORACLE_OUTPUT_TOO_LARGE"),
])
def test_invalid_oracle_outputs_leave_no_partial_private_material(
    tmp_path: Path, outputs: tuple[bytes, ...], code: str,
) -> None:
    path = tmp_path / f"oracle-{code}.db"
    frozen_problem(path)
    builder = TrustedAssetBuilder(path, evaluator=FakeOracleEvaluator(outputs))
    with pytest.raises(OracleEvaluationFailure) as caught:
        builder.build(oracle_manifest())
    assert caught.value.code == code
    assert VerifierAssetRepository(path).get("problem-submit", 1)["status"] == "building"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM reference_oracles").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM private_test_cases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 0


def test_build_is_idempotent_but_same_problem_cannot_be_silently_resealed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "idempotent.db"
    material, builder = build_expected_material(path)
    first_private = builder.list_private("problem-submit", 1)

    repeated = TrustedAssetBuilder(path).build(expected_output_manifest())
    assert repeated == material
    assert TrustedAssetBuilder(path).list_private("problem-submit", 1) == first_private

    changed = expected_output_manifest()
    changed["privateTests"][0]["expectedOutput"] = "PRIVATE-CHANGED-ANSWER\n"  # type: ignore[index]
    with pytest.raises(StorageFailure) as caught:
        TrustedAssetBuilder(path).build(changed)
    assert caught.value.code == "TRUSTED_MATERIAL_ALREADY_SEALED"
    assert "PRIVATE-CHANGED-ANSWER" not in str(caught.value)


def test_concurrent_identical_builds_commit_one_material(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    frozen_problem(path)
    PublicTestCaseRepository(path).register_examples("problem-submit", 1)
    barrier = Barrier(2)

    class RacingBuilder(TrustedAssetBuilder):
        def _find_material(self, manifest_id: str, problem_id: str, version: int):
            found = super()._find_material(manifest_id, problem_id, version)
            if not getattr(self, "_barrier_used", False):
                self._barrier_used = True
                barrier.wait(timeout=5)
            return found

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: RacingBuilder(path).build(expected_output_manifest()),
            range(2),
        ))

    assert results[0]["materialHash"] == results[1]["materialHash"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM private_test_cases").fetchone()[0] == 2


def test_invalid_manifest_draft_problem_and_duplicate_private_input_are_rejected(
    tmp_path: Path,
) -> None:
    invalid_path = tmp_path / "invalid.db"
    frozen_problem(invalid_path)
    invalid = expected_output_manifest()
    del invalid["privateTests"][0]["expectedOutput"]  # type: ignore[index]
    with pytest.raises(StorageFailure) as malformed:
        TrustedAssetBuilder(invalid_path).build(invalid)
    assert malformed.value.code == "TRUSTED_MANIFEST_INVALID"

    draft_path = tmp_path / "draft.db"
    ProblemRepository(draft_path).create(
        create_problem_package(INTAKE, id_factory=lambda: "problem-draft")
    )
    with pytest.raises(StorageFailure) as draft:
        TrustedAssetBuilder(draft_path).build(
            expected_output_manifest(problem_id="problem-draft")
        )
    assert draft.value.code == "TRUSTED_MATERIAL_PROBLEM_NOT_FROZEN"

    duplicate_path = tmp_path / "duplicate.db"
    frozen_problem(duplicate_path)
    duplicate = expected_output_manifest()
    secret_input = "PRIVATE-DUPLICATE-INPUT\n"
    duplicate["privateTests"][0]["stdin"] = secret_input  # type: ignore[index]
    duplicate["privateTests"][1]["stdin"] = secret_input  # type: ignore[index]
    with pytest.raises(StorageFailure) as repeated_input:
        TrustedAssetBuilder(duplicate_path).build(duplicate)
    assert repeated_input.value.code == "PRIVATE_TEST_INPUT_DUPLICATE"
    assert secret_input.strip() not in str(repeated_input.value)


def test_provider_refuses_cross_checking_material_even_when_tests_are_present(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-ready.db"
    material, _ = build_expected_material(path)

    with pytest.raises(JudgeFailure) as caught:
        SQLiteTrustedAssetProvider(path).get("problem-submit", 1)

    assert "1\n-7\n" not in str(caught.value)
    assert "1 1\n" not in str(caught.value)
    assert material["privateTestCount"] == 2


def test_provider_loads_public_and_private_cases_only_after_matching_passed_report(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ready.db"
    material, _ = build_expected_material(path)
    mark_ready(path, material)

    bundle = SQLiteTrustedAssetProvider(path).get("problem-submit", 1)

    assert bundle.status == "ready"
    assert (bundle.problem_id, bundle.problem_version) == ("problem-submit", 1)
    assert (bundle.asset_id, bundle.asset_version) == (
        material["verifierAssetRef"]["assetId"],  # type: ignore[index]
        1,
    )
    assert [(case.stdin, case.expected_output) for case in bundle.cases] == [
        (INTAKE["examples"][0]["input"].encode("utf-8"), INTAKE["examples"][0]["output"].encode("utf-8")),
        (b"1\n-7\n", b"1 1\n"),
        (b"4\n2 2 2 2\n", b"1 4\n"),
    ]


def test_production_worker_uses_sqlite_material_and_finalizes_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.db"
    material, _ = build_expected_material(path)
    mark_ready(path, material)
    submissions = SubmissionRepository(
        path,
        goal_id_factory=lambda: "goal-worker",
        snapshot_id_factory=lambda: "snapshot-worker",
        submission_id_factory=lambda: "submission-worker",
    )
    submissions.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    submissions.create_code_snapshot("goal-worker", "print('answer')\n", "python3")
    submission, _ = submissions.create_submission({
        "goalContext": {"goalId": "goal-worker", "scope": "goal"},
        "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
        "codeVersion": 1,
        "language": "python3",
        "idempotencyKey": "worker-submit-once",
    })
    assert submission["status"] == "queued"

    queue = WorkerQueue(path, token_factory=lambda: "claim-worker")
    processor = JudgeSubmissionProcessor(
        FakeJudgeRunner([b"1 2\n", b"1 1\n", b"1 4\n"]),
        SQLiteTrustedAssetProvider(path),
        OJResultRepository(path, id_factory=lambda: "oj-worker"),
    )
    workspace_root = tmp_path / "worker-spaces"
    assert Worker(queue, processor, "worker-1", workspace_root).process_once() == "submission-worker"

    assert submissions.get_submission("submission-worker")["status"] == "finished"
    result = OJResultRepository(path).get_for_submission("submission-worker")
    assert result is not None and result["verdict"] == "AC"
    assert "expectedOutput" not in json.dumps(result)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM submission_claims").fetchone()[0] == 0
    assert list(workspace_root.iterdir()) == []


def test_internal_build_cli_reports_only_safe_summary(tmp_path: Path) -> None:
    path = tmp_path / "cli.db"
    frozen_problem(path)
    manifest = expected_output_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "tools/build-trusted-assets.py",
            str(manifest_path),
            "--database",
            str(path),
            "--workspace-root",
            str(tmp_path / "oracle-spaces"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0
    summary = json.loads(completed.stdout)
    assert summary["status"] == "crossChecking"
    assert summary["privateTestCount"] == 2
    assert "1\n-7\n" not in completed.stdout
    assert "1 1\n" not in completed.stdout


def test_provider_rejects_tampered_private_case_without_leaking_hidden_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tampered.db"
    material, _ = build_expected_material(path)
    mark_ready(path, material)
    secret = "PRIVATE-TAMPERED-EXPECTED-OUTPUT"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE private_test_cases SET expected_output_text=? WHERE case_key='single-negative'",
            (secret,),
        )
        db.commit()

    with pytest.raises(JudgeFailure) as caught:
        SQLiteTrustedAssetProvider(path).get("problem-submit", 1)

    assert secret not in str(caught.value)


def test_provider_rejects_test_record_with_wrong_problem_context(tmp_path: Path) -> None:
    path = tmp_path / "wrong-context.db"
    material, builder = build_expected_material(path)
    mark_ready(path, material)
    secret = builder.list_private("problem-submit", 1)[0]["stdin"]
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE private_test_cases SET problem_id='wrong-problem' WHERE ordinal=0",
        )
        db.commit()

    with pytest.raises(JudgeFailure) as caught:
        SQLiteTrustedAssetProvider(path).get("problem-submit", 1)
    assert secret not in str(caught.value)


def test_provider_derives_build_method_instead_of_trusting_coordinated_labels(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wrong-method.db"
    material, _ = build_expected_material(path)
    mark_ready(path, material)
    with sqlite3.connect(path) as db:
        summary = json.loads(db.execute(
            "SELECT material_summary_json FROM verifier_assets",
        ).fetchone()[0])
        summary["buildMethod"] = "hybrid"
        db.execute(
            "UPDATE verifier_assets SET material_summary_json=?",
            (json.dumps(summary, separators=(",", ":")),),
        )
        db.execute("UPDATE trusted_test_materials SET build_method='hybrid'")
        db.commit()

    with pytest.raises(JudgeFailure):
        SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


@pytest.mark.parametrize(
    "report_damage", ["missing", "wrongHash", "failedOutcome", "emptyChecks"],
)
def test_provider_rejects_missing_or_untrusted_cross_check_report(
    tmp_path: Path,
    report_damage: str,
) -> None:
    path = tmp_path / f"report-{report_damage}.db"
    material, builder = build_expected_material(path)
    mark_ready(path, material)
    private_secret = builder.list_private("problem-submit", 1)[0]["stdin"]

    with sqlite3.connect(path) as db:
        if report_damage == "missing":
            db.execute("UPDATE verifier_assets SET cross_check_report_json=NULL")
        else:
            damaged = cross_check_report(str(material["materialHash"]))
            if report_damage == "wrongHash":
                damaged["checkedMaterialHash"] = "sha256:" + "f" * 64
            elif report_damage == "emptyChecks":
                damaged["checksRun"] = []
            else:
                damaged["outcome"] = "failed"
            db.execute(
                "UPDATE verifier_assets SET cross_check_report_json=?",
                (json.dumps(damaged, separators=(",", ":")),),
            )
        db.commit()

    with pytest.raises(JudgeFailure) as caught:
        SQLiteTrustedAssetProvider(path).get("problem-submit", 1)

    assert private_secret not in str(caught.value)

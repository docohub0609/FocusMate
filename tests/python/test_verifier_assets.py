from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
import json
import threading
from urllib.request import urlopen

import pytest
from jsonschema import ValidationError

from apps.api.problem_intake import create_problem_package
from apps.api.__main__ import create_server
from apps.api.problem_storage import ProblemRepository, StorageFailure
from apps.api.verifier_assets import VerifierAssetRepository
from apps.api.submission_storage import SubmissionRepository
from tests.python.test_problem_features import INTAKE
from tests.python.test_submission_storage import frozen_problem, repository, submission_request


HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def prepared(tmp_path: Path, name: str = "asset.db"):
    path = tmp_path / name
    frozen_problem(path)
    submissions = repository(path)
    submissions.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    submissions.create_code_snapshot("goal-submit", "print(input())\n", "python3")
    assets = VerifierAssetRepository(path, clock=lambda: NOW, id_factory=lambda: "asset-test")
    assets.create_missing("problem-submit", 1)
    return path, submissions, assets


def summary(method: str = "hybrid", count: int = 3, material_hash: str = HASH):
    return {"testCount": count, "buildMethod": method, "materialHash": material_hash}


def report(outcome: str = "passed", checked_hash: str = HASH):
    return {"reportId": "report-test", "outcome": outcome,
            "checkedMaterialHash": checked_hash, "checksRun": ["referenceAgreement"],
            "createdAt": "2026-08-29T12:00:00Z"}


def test_full_ready_lifecycle_persists_and_releases_waiting_submission(tmp_path: Path) -> None:
    path, submissions, assets = prepared(tmp_path)
    submission, _ = submissions.create_submission(submission_request())
    assert submission["status"] == "awaitingAssets"
    assert assets.start_build("problem-submit", 1)["status"] == "building"
    assert assets.submit_material("problem-submit", 1, summary())["status"] == "crossChecking"
    ready = assets.complete_cross_check("problem-submit", 1, report())
    assert ready["status"] == "ready"
    assert ProblemRepository(path).get("problem-submit", 1)["problemPackage"]["verifierAssetStatus"] == "ready"
    assert SubmissionRepository(path).get_submission("submission-submit")["status"] == "queued"
    assert VerifierAssetRepository(path).get("problem-submit", 1) == ready
    assert assets.verification_availability("problem-submit", 1)["availability"] == "available"
    assert [item["toStatus"] for item in assets.transitions("asset-test")] == ["building", "crossChecking", "ready"]


def test_ready_releases_only_matching_problem_version(tmp_path: Path) -> None:
    path, submissions, assets = prepared(tmp_path)
    submissions.create_submission(submission_request())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE submissions SET problem_version=99 WHERE submission_id='submission-submit'")
        db.commit()
    assets.start_build("problem-submit", 1)
    assets.submit_material("problem-submit", 1, summary())
    assets.complete_cross_check("problem-submit", 1, report())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM submissions WHERE submission_id='submission-submit'").fetchone()[0] == "awaitingAssets"


def test_rejected_asset_fails_waiting_submission_without_oj_result(tmp_path: Path) -> None:
    path, submissions, assets = prepared(tmp_path)
    submissions.create_submission(submission_request())
    assets.start_build("problem-submit", 1)
    rejected = assets.reject("problem-submit", 1, "oracleDisagreement")
    saved = SubmissionRepository(path).get_submission("submission-submit")
    assert rejected["status"] == "rejected"
    assert saved["status"] == "failed"
    assert saved["statusReason"] == "verifierAssetRejected"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oj_results'").fetchone() is None


def test_failed_cross_check_rejects_and_preserves_safe_reason(tmp_path: Path) -> None:
    _, _, assets = prepared(tmp_path)
    assets.start_build("problem-submit", 1)
    assets.submit_material("problem-submit", 1, summary())
    assert assets.complete_cross_check("problem-submit", 1, report("failed"))["rejectionReason"] == "crossCheckFailed"


@pytest.mark.parametrize("operation", ["skip", "repeat", "terminal"])
def test_illegal_state_transitions_are_rejected(tmp_path: Path, operation: str) -> None:
    _, _, assets = prepared(tmp_path, f"{operation}.db")
    with pytest.raises(StorageFailure) as caught:
        if operation == "skip": assets.submit_material("problem-submit", 1, summary())
        elif operation == "repeat":
            assets.start_build("problem-submit", 1); assets.start_build("problem-submit", 1)
        else:
            assets.start_build("problem-submit", 1); assets.reject("problem-submit", 1, "bad"); assets.start_build("problem-submit", 1)
    assert caught.value.code == "ASSET_STATE_TRANSITION_INVALID"


def test_draft_wrong_version_and_duplicate_asset_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "draft.db"
    ProblemRepository(path).create(create_problem_package(INTAKE, id_factory=lambda: "problem-submit"))
    assets = VerifierAssetRepository(path, id_factory=lambda: "asset-test")
    with pytest.raises(StorageFailure) as draft:
        assets.create_missing("problem-submit", 1)
    assert draft.value.code == "ASSET_PROBLEM_NOT_FROZEN"
    with pytest.raises(StorageFailure) as missing:
        assets.create_missing("problem-submit", 99)
    assert missing.value.code == "PROBLEM_NOT_FOUND"
    frozen_problem(tmp_path / "duplicate.db")
    duplicate = VerifierAssetRepository(tmp_path / "duplicate.db", id_factory=lambda: "asset-test")
    duplicate.create_missing("problem-submit", 1)
    with pytest.raises(StorageFailure) as caught:
        duplicate.create_missing("problem-submit", 1)
    assert caught.value.code == "ASSET_ALREADY_EXISTS"


@pytest.mark.parametrize("bad", [
    {"testCount": 0, "buildMethod": "hybrid", "materialHash": HASH},
    {"testCount": 1, "buildMethod": "hybrid", "materialHash": "bad"},
])
def test_invalid_material_summary_is_rejected(tmp_path: Path, bad: dict[str, object]) -> None:
    _, _, assets = prepared(tmp_path)
    assets.start_build("problem-submit", 1)
    with pytest.raises(ValidationError): assets.submit_material("problem-submit", 1, bad)


def test_report_must_match_material_and_public_examples_cannot_be_ready(tmp_path: Path) -> None:
    _, _, assets = prepared(tmp_path, "mismatch.db")
    assets.start_build("problem-submit", 1); assets.submit_material("problem-submit", 1, summary())
    with pytest.raises(StorageFailure) as mismatch: assets.complete_cross_check("problem-submit", 1, report(checked_hash=OTHER_HASH))
    assert mismatch.value.code == "ASSET_REPORT_MISMATCH"

    _, _, public = prepared(tmp_path, "public.db")
    public.start_build("problem-submit", 1); public.submit_material("problem-submit", 1, summary("publicExamplesOnly"))
    with pytest.raises(StorageFailure) as insufficient: public.complete_cross_check("problem-submit", 1, report())
    assert insufficient.value.code == "ASSET_PUBLIC_ONLY"


def test_missing_asset_reports_verification_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "unavailable.db"
    frozen_problem(path)
    status = VerifierAssetRepository(path).verification_availability("problem-submit", 1)
    assert status == {"problemRef": {"problemId": "problem-submit", "problemVersion": 1},
                      "availability": "verificationUnavailable", "assetStatus": "missing"}


def test_browser_can_read_availability_but_has_no_asset_mutation_route(tmp_path: Path) -> None:
    path = tmp_path / "api.db"
    frozen_problem(path)
    problems = ProblemRepository(path)
    assets = VerifierAssetRepository(path)
    server = create_server(port=0, problem_repository=problems,
                           submission_repository=SubmissionRepository(path),
                           verifier_asset_repository=assets)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/problems/problem-submit/versions/1/verifier-asset"
        with urlopen(url, timeout=2) as response:
            assert response.status == 200
            assert json.load(response)["availability"] == "verificationUnavailable"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

from __future__ import annotations

from copy import deepcopy

import pytest

from apps.api.problem_features import build_human_feature_set, extract_problem_features
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from apps.api.schema_validation import validate
from tests.python.test_problem_features import FakeProvider, INTAKE, base_features
from tests.python.test_problem_lifecycle_api import request, running_api


def test_human_created_features_skip_llm_semantic_number_check_and_freeze(tmp_path) -> None:
    with running_api(tmp_path) as base:
        status, created = request(base, "POST", "/api/v1/problems", INTAKE)
        assert status == 201
        problem_id = created["problemPackage"]["problemId"]
        features = base_features()
        features[0]["value"] = {"variable": "n", "maximum": 999999}

        status, confirmed = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions/1/confirm-and-freeze",
            {"expectedDraftRevision": 1, "features": features},
        )

        assert status == 200
        report = confirmed["featureSet"]
        validate("ProblemFeatures", report)
        assert report["provenance"]["type"] == "humanCreated"
        assert "extractor" not in report
        assert report["features"][0]["value"]["maximum"] == 999999
        assert report["features"][0]["evidence"][0]["evidenceId"] == "constraints:0"
        assert confirmed["problemRecord"]["lifecycle"]["status"] == "frozen"
        assert confirmed["problemRecord"]["lifecycle"]["confirmedFeatureSetId"] == report["featureSetId"]


def test_human_edited_report_supersedes_llm_report_atomically(tmp_path) -> None:
    repo = ProblemRepository(tmp_path / "review.db")
    record = repo.create(create_problem_package(INTAKE, id_factory=lambda: "problem-review"))
    llm = extract_problem_features(
        {"problemPackage": record["problemPackage"]},
        FakeProvider([{"features": base_features()}]),
    )
    repo.save_feature_set(llm)
    request_body = {
        "expectedDraftRevision": 1,
        "basedOnFeatureSetId": llm["featureSetId"],
        "features": base_features(),
    }
    reviewed = build_human_feature_set(
        record["problemPackage"], request_body, base_feature_set=llm,
    )

    frozen = repo.confirm_and_freeze(
        "problem-review", 1, reviewed, 1, llm["featureSetId"],
    )

    assert frozen["lifecycle"]["confirmedFeatureSetId"] == reviewed["featureSetId"]
    assert repo.current_feature_set("problem-review", 1)["featureSetId"] == reviewed["featureSetId"]
    assert reviewed["provenance"]["type"] == "humanEdited"
    assert reviewed["provenance"]["basedOnFeatureSetId"] == llm["featureSetId"]
    assert reviewed["extractor"] == llm["extractor"]


def test_invalid_human_report_does_not_freeze_or_replace_active_llm_report(tmp_path) -> None:
    with running_api(tmp_path) as base:
        status, created = request(base, "POST", "/api/v1/problems", INTAKE)
        assert status == 201
        problem_id = created["problemPackage"]["problemId"]
        status, llm = request(
            base, "POST", "/api/v1/problem-feature-extractions",
            {"problemRef": {"problemId": problem_id, "problemVersion": 1}},
        )
        assert status == 201
        missing_category = [
            feature for feature in base_features() if feature["category"] != "outputStructure"
        ]

        status, error = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions/1/confirm-and-freeze",
            {
                "expectedDraftRevision": 1,
                "basedOnFeatureSetId": llm["featureSetId"],
                "features": missing_category,
            },
        )

        assert status == 422
        assert error["code"] == "INVALID_FEATURE_CONFIRMATION"

        wrong_evidence = deepcopy(base_features())
        wrong_evidence[2]["evidenceRefs"] = ["inputSpec:0"]
        status, error = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions/1/confirm-and-freeze",
            {"expectedDraftRevision": 1, "features": wrong_evidence},
        )
        assert status == 422
        assert error["code"] == "INVALID_FEATURE_CONFIRMATION"

        conflict = deepcopy(base_features())
        conflict[0]["status"] = "conflict"
        status, error = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions/1/confirm-and-freeze",
            {"expectedDraftRevision": 1, "features": conflict},
        )
        assert status == 422
        assert error["code"] == "INVALID_FEATURE_CONFIRMATION"
        status, problem = request(base, "GET", f"/api/v1/problems/{problem_id}/versions/1")
        assert status == 200
        assert problem["lifecycle"]["status"] == "draft"
        status, current = request(
            base, "GET", f"/api/v1/problems/{problem_id}/versions/1/features/current",
        )
        assert status == 200
        assert current["featureSetId"] == llm["featureSetId"]


def test_human_confirmation_rejects_forged_text_wrong_evidence_and_conflict(tmp_path) -> None:
    with running_api(tmp_path) as base:
        status, created = request(base, "POST", "/api/v1/problems", INTAKE)
        assert status == 201
        problem_id = created["problemPackage"]["problemId"]

        forged = deepcopy(base_features())
        forged[0]["sourceText"] = "forged"
        status, error = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions/1/confirm-and-freeze",
            {"expectedDraftRevision": 1, "features": forged},
        )
        assert status == 422
        assert error["code"] == "INVALID_FEATURE_CONFIRMATION"


def test_stale_revision_rolls_back_human_report_and_freeze(tmp_path) -> None:
    repo = ProblemRepository(tmp_path / "rollback.db")
    record = repo.create(create_problem_package(INTAKE, id_factory=lambda: "problem-rollback"))
    llm = extract_problem_features(
        {"problemPackage": record["problemPackage"]},
        FakeProvider([{"features": base_features()}]),
    )
    repo.save_feature_set(llm)
    request_body = {
        "expectedDraftRevision": 2,
        "basedOnFeatureSetId": llm["featureSetId"],
        "features": base_features(),
    }
    reviewed = build_human_feature_set(
        record["problemPackage"], request_body, base_feature_set=llm,
    )

    with pytest.raises(StorageFailure) as caught:
        repo.confirm_and_freeze(
            "problem-rollback", 1, reviewed, 2, llm["featureSetId"],
        )

    assert caught.value.code == "DRAFT_REVISION_CONFLICT"
    assert repo.get("problem-rollback", 1)["lifecycle"]["status"] == "draft"
    assert repo.current_feature_set("problem-rollback", 1)["featureSetId"] == llm["featureSetId"]
    with pytest.raises(StorageFailure) as missing:
        repo.get_feature_set(reviewed["featureSetId"])
    assert missing.value.code == "FEATURE_NOT_FOUND"

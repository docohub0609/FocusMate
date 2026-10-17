from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from apps.api.problem_features import extract_problem_features
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from tests.python.test_problem_features import FakeProvider, INTAKE, base_features


def repository(tmp_path: Path) -> ProblemRepository:
    return ProblemRepository(tmp_path / "focusmate-test.db")


def create_record(repo: ProblemRepository) -> dict[str, object]:
    return repo.create(create_problem_package(INTAKE, id_factory=lambda: "problem-stored"))


def feature_result(problem: dict[str, object], features: list[dict[str, object]] | None = None) -> dict[str, object]:
    return extract_problem_features(
        {"problemPackage": problem},
        FakeProvider([{"features": features or base_features()}]),
    )


def test_problem_survives_repository_reconnect_and_examples_are_reassembled(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    created = create_record(repo)
    reopened = ProblemRepository(repo.path)
    loaded = reopened.get("problem-stored", 1)
    assert loaded == created
    assert loaded["problemPackage"]["examples"] == INTAKE["examples"]
    assert loaded["lifecycle"]["status"] == "draft"


def test_draft_update_changes_revision_and_hash_and_rejects_stale_writer(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record = create_record(repo)
    original_hash = record["lifecycle"]["contentHash"]
    updated = deepcopy(record["problemPackage"])
    updated["constraints"] = "1 <= n <= 200000"
    saved = repo.update_draft("problem-stored", 1, updated, 1)
    assert saved["lifecycle"]["draftRevision"] == 2
    assert saved["lifecycle"]["contentHash"] != original_hash
    with pytest.raises(StorageFailure, match="") as caught:
        repo.update_draft("problem-stored", 1, updated, 1)
    assert caught.value.code == "DRAFT_REVISION_CONFLICT"


def test_new_feature_supersedes_old_and_edit_makes_current_report_unavailable(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record = create_record(repo)
    problem = record["problemPackage"]
    first = feature_result(problem)
    second = feature_result(problem)
    repo.save_feature_set(first)
    repo.save_feature_set(second)
    assert repo.current_feature_set("problem-stored", 1)["featureSetId"] == second["featureSetId"]
    changed = deepcopy(problem)
    changed["statement"] += " Extra clarification."
    repo.update_draft("problem-stored", 1, changed, 1)
    with pytest.raises(StorageFailure) as caught:
        repo.current_feature_set("problem-stored", 1)
    assert caught.value.code == "FEATURE_NOT_FOUND"


@pytest.mark.parametrize("uncertain_status", ["unknown", "hypothesis"])
def test_unknown_and_hypothesis_allow_freezing(tmp_path: Path, uncertain_status: str) -> None:
    repo = repository(tmp_path)
    record = create_record(repo)
    features = base_features()
    features[3]["status"] = uncertain_status
    if uncertain_status == "hypothesis":
        features[3].update({
            "value": {"constraintPressure": "moderate"}, "confidence": 0.5,
            "reason": "The supplied constraint may imply moderate resource pressure.",
            "evidenceRefs": ["constraints:0"],
        })
    result = feature_result(record["problemPackage"], features)
    repo.save_feature_set(result)
    frozen = repo.freeze("problem-stored", 1, result["featureSetId"], 1)
    assert frozen["lifecycle"]["status"] == "frozen"
    assert frozen["problemPackage"]["verifierAssetStatus"] == "missing"
    assert next(item for item in result["features"] if item["category"] == "resourceConstraint")["status"] == uncertain_status


def test_conflict_blocks_freeze_and_frozen_version_is_immutable(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record = create_record(repo)
    features = base_features()
    features[0] = {
        "featureKey": "dataScale.n", "category": "dataScale", "status": "conflict",
        "reason": "Two limits disagree.", "evidenceRefs": [],
        "candidates": [
            {"value": 1, "evidenceRefs": ["constraints:0"]},
            {"value": 100000, "evidenceRefs": ["constraints:0"]},
        ],
    }
    result = feature_result(record["problemPackage"], features)
    repo.save_feature_set(result)
    with pytest.raises(StorageFailure) as caught:
        repo.freeze("problem-stored", 1, result["featureSetId"], 1)
    assert caught.value.code == "FEATURE_CONFLICT"

    clean = feature_result(record["problemPackage"])
    repo.save_feature_set(clean)
    repo.freeze("problem-stored", 1, clean["featureSetId"], 1)
    with pytest.raises(StorageFailure) as immutable:
        repo.update_draft("problem-stored", 1, record["problemPackage"], 1)
    assert immutable.value.code == "PROBLEM_VERSION_FROZEN"


def test_frozen_latest_can_create_one_traceable_draft_revision(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    record = create_record(repo)
    result = feature_result(record["problemPackage"])
    repo.save_feature_set(result)
    repo.freeze("problem-stored", 1, result["featureSetId"], 1)
    version_two = repo.create_revision("problem-stored", 1)
    assert version_two["problemPackage"]["problemId"] == "problem-stored"
    assert version_two["problemPackage"]["version"] == 2
    assert version_two["lifecycle"]["status"] == "draft"
    assert [item["version"] for item in repo.list_versions("problem-stored")] == [1, 2]
    with pytest.raises(StorageFailure) as caught:
        repo.create_revision("problem-stored", 1)
    assert caught.value.code == "DRAFT_ALREADY_EXISTS"

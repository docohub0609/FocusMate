from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.problem_features import extract_problem_features
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository
from apps.api.submission_storage import SubmissionFailure, SubmissionRepository
from tests.python.test_problem_features import FakeProvider, INTAKE, base_features


def frozen_problem(path: Path, asset_status: str = "missing") -> tuple[ProblemRepository, dict[str, object]]:
    problems = ProblemRepository(path)
    package = create_problem_package(INTAKE, id_factory=lambda: "problem-submit")
    package["verifierAssetStatus"] = asset_status
    record = problems.create(package)
    features = extract_problem_features(
        {"problemPackage": record["problemPackage"]},
        FakeProvider([{"features": base_features()}]),
    )
    problems.save_feature_set(features)
    return problems, problems.freeze("problem-submit", 1, features["featureSetId"], 1)


def repository(path: Path) -> SubmissionRepository:
    snapshot_ids = iter(["snapshot-submit", "snapshot-submit-2", "snapshot-submit-3"])
    return SubmissionRepository(
        path,
        goal_id_factory=lambda: "goal-submit",
        snapshot_id_factory=lambda: next(snapshot_ids),
        submission_id_factory=lambda: "submission-submit",
    )


def submission_request(code_version: int = 1, key: str = "click-1") -> dict[str, object]:
    return {
        "goalContext": {"goalId": "goal-submit", "scope": "goal"},
        "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
        "codeVersion": code_version, "language": "python3", "idempotencyKey": key,
    }


def prepared_repository(tmp_path: Path, asset_status: str = "missing") -> SubmissionRepository:
    path = tmp_path / f"{asset_status}.db"
    frozen_problem(path, asset_status)
    repo = repository(path)
    repo.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    repo.create_code_snapshot("goal-submit", "print(input())\n", "python3")
    return repo


def test_goal_requires_frozen_problem_and_records_real_reference(tmp_path: Path) -> None:
    path = tmp_path / "draft.db"
    problems = ProblemRepository(path)
    problems.create(create_problem_package(INTAKE, id_factory=lambda: "problem-submit"))
    repo = repository(path)
    with pytest.raises(SubmissionFailure) as caught:
        repo.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    assert caught.value.code == "PROBLEM_VERSION_NOT_FROZEN"

    frozen_problem(tmp_path / "frozen.db")
    frozen_repo = repository(tmp_path / "frozen.db")
    goal = frozen_repo.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    assert goal["goalId"] == "goal-submit"
    assert frozen_repo.get_goal("goal-submit") == goal


def test_code_snapshots_are_immutable_versions_and_survive_reconnect(tmp_path: Path) -> None:
    repo = prepared_repository(tmp_path)
    first = repo.get_code_snapshot("goal-submit", 1)
    second = repo.create_code_snapshot("goal-submit", "print('changed')\n", "python3")
    assert first["codeVersion"] == 1
    assert second["codeVersion"] == 2
    assert first["contentHash"] != second["contentHash"]
    reopened = SubmissionRepository(repo.path)
    assert reopened.get_code_snapshot("goal-submit", 1)["sourceCode"] == "print(input())\n"


@pytest.mark.parametrize("asset_status", ["missing", "building", "crossChecking"])
def test_unready_assets_create_awaiting_submission(tmp_path: Path, asset_status: str) -> None:
    repo = prepared_repository(tmp_path, asset_status)
    result, created = repo.create_submission(submission_request())
    assert created is True
    assert result["status"] == "awaitingAssets"


def test_ready_assets_queue_and_rejected_assets_block_submission(tmp_path: Path) -> None:
    ready = prepared_repository(tmp_path, "ready")
    result, _ = ready.create_submission(submission_request())
    assert result["status"] == "queued"

    rejected = prepared_repository(tmp_path, "rejected")
    with pytest.raises(SubmissionFailure) as caught:
        rejected.create_submission(submission_request())
    assert caught.value.code == "VERIFIER_ASSET_REJECTED"


def test_idempotency_returns_first_submission_and_reconnect_reads_it(tmp_path: Path) -> None:
    repo = prepared_repository(tmp_path)
    first, first_created = repo.create_submission(submission_request())
    second, second_created = repo.create_submission(submission_request())
    assert first_created is True
    assert second_created is False
    assert second == first
    assert SubmissionRepository(repo.path).get_submission("submission-submit") == first


def test_submission_rejects_goal_problem_and_code_context_mismatches(tmp_path: Path) -> None:
    repo = prepared_repository(tmp_path)
    wrong_problem = submission_request()
    wrong_problem["problemRef"] = {"problemId": "other", "problemVersion": 1}
    with pytest.raises(SubmissionFailure) as goal_error:
        repo.create_submission(wrong_problem)
    assert goal_error.value.code == "GOAL_CONTEXT_MISMATCH"

    with pytest.raises(SubmissionFailure) as code_error:
        repo.create_submission(submission_request(code_version=99))
    assert code_error.value.code == "CODE_SNAPSHOT_NOT_FOUND"


def test_state_machine_allows_only_declared_transitions_and_terminal_is_immutable(tmp_path: Path) -> None:
    repo = prepared_repository(tmp_path, "ready")
    repo.create_submission(submission_request())
    with pytest.raises(SubmissionFailure) as skipped:
        repo.transition("submission-submit", "running")
    assert skipped.value.code == "SUBMISSION_STATE_TRANSITION_INVALID"

    assert repo.transition("submission-submit", "preparing")["status"] == "preparing"
    assert repo.transition("submission-submit", "running")["status"] == "running"
    assert repo.transition("submission-submit", "finished")["status"] == "finished"
    with pytest.raises(SubmissionFailure) as terminal:
        repo.transition("submission-submit", "running")
    assert terminal.value.code == "SUBMISSION_STATE_TRANSITION_INVALID"


def test_awaiting_submission_can_queue_then_complete_normal_lifecycle(tmp_path: Path) -> None:
    repo = prepared_repository(tmp_path, "missing")
    repo.create_submission(submission_request())
    for status in ["queued", "preparing", "running", "finished"]:
        result = repo.transition("submission-submit", status)
    assert result["status"] == "finished"


@pytest.mark.parametrize(
    ("asset_status", "path"),
    [
        ("missing", ["failed"]),
        ("ready", ["failed"]),
        ("ready", ["expired"]),
        ("ready", ["preparing", "failed"]),
        ("ready", ["preparing", "expired"]),
        ("ready", ["preparing", "running", "failed"]),
        ("ready", ["preparing", "running", "expired"]),
    ],
)
def test_declared_failure_and_expiry_paths(tmp_path: Path, asset_status: str, path: list[str]) -> None:
    repo = prepared_repository(tmp_path, asset_status)
    repo.create_submission(submission_request())
    for status in path:
        result = repo.transition("submission-submit", status)
    assert result["status"] == path[-1]

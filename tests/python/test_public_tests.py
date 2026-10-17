from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from apps.api.public_tests import PublicTestCaseRepository
from apps.api.schema_validation import validate
from apps.api.verifier_assets import VerifierAssetRepository
from apps.api.submission_storage import SubmissionRepository
from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary
from services.oj_worker.public_samples import (
    PublicSampleExecutor, PublicSampleRunRequest, SQLitePublicTestProvider,
)
from tests.python.test_problem_features import INTAKE
from tests.python.test_submission_storage import frozen_problem, repository, submission_request


SUMMARY = ResourceSummary(1, 256 * 1024 * 1024, 1024, 1, 2, 32, 16 * 1024 * 1024)


def run_result(stdout: bytes = b"", *, exit_code: int | None = 0,
               reason: str = "exited", stderr: bytes = b"") -> PythonRunResult:
    return PythonRunResult(stdout, stderr, exit_code, reason, False, False, False, SUMMARY)


class FakeRunner:
    def __init__(self, results: list[PythonRunResult]) -> None:
        self.results = iter(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return next(self.results)


def test_registers_exact_multiline_unicode_examples_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "public.db"
    problems, _ = frozen_problem(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM problem_examples WHERE problem_id='problem-submit'")
        db.executemany("INSERT INTO problem_examples VALUES('problem-submit',1,?,?,?)", [
            (0, "2\n你好 世界\n", "你好\n 世界 \n"),
            (1, "1\n多行\n输入\n", "两行\n输出\n"),
        ])
        db.commit()
    repo = PublicTestCaseRepository(path, id_factory=iter(["case-0", "case-1"]).__next__)
    first = repo.register_examples("problem-submit", 1)
    second = PublicTestCaseRepository(path).register_examples("problem-submit", 1)
    assert first == second
    assert [(x["stdin"], x["expectedOutput"]) for x in first] == [
        ("2\n你好 世界\n", "你好\n 世界 \n"), ("1\n多行\n输入\n", "两行\n输出\n")]
    assert VerifierAssetRepository(path).get("problem-submit", 1)["status"] == "building"


def test_registration_rejects_draft_wrong_version_oversize_and_source_change(tmp_path: Path) -> None:
    draft = tmp_path / "draft.db"
    ProblemRepository(draft).create(create_problem_package(INTAKE, id_factory=lambda: "draft"))
    with pytest.raises(StorageFailure) as caught:
        PublicTestCaseRepository(draft).register_examples("draft", 1)
    assert caught.value.code == "PUBLIC_TEST_PROBLEM_NOT_FROZEN"
    with pytest.raises(StorageFailure):
        PublicTestCaseRepository(draft).register_examples("draft", 99)

    path = tmp_path / "frozen.db"
    frozen_problem(path)
    repo = PublicTestCaseRepository(path)
    repo.register_examples("problem-submit", 1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE problem_examples SET input_text='changed' WHERE problem_id='problem-submit' AND version=1 AND example_index=0")
        db.commit()
    with pytest.raises(StorageFailure) as changed:
        repo.register_examples("problem-submit", 1)
    assert changed.value.code == "PUBLIC_TEST_SOURCE_CHANGED"

    large = tmp_path / "large.db"
    frozen_problem(large)
    with sqlite3.connect(large) as db:
        db.execute("UPDATE problem_examples SET input_text=? WHERE problem_id='problem-submit'", ("你" * 400_000,))
        db.commit()
    with pytest.raises(StorageFailure) as too_large:
        PublicTestCaseRepository(large).register_examples("problem-submit", 1)
    assert too_large.value.code == "PUBLIC_TEST_TEXT_TOO_LARGE"


def prepared_executor(tmp_path: Path, execution_results: list[PythonRunResult]):
    path = tmp_path / "execute.db"
    frozen_problem(path)
    PublicTestCaseRepository(path, id_factory=lambda: "case-0").register_examples("problem-submit", 1)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('x')\n", encoding="utf-8")
    runner = FakeRunner([run_result(), *execution_results])
    executor = PublicSampleExecutor(runner, SQLitePublicTestProvider(path), id_factory=lambda: "run-1")
    request = PublicSampleRunRequest("goal-1", "problem-submit", 1, 1, workspace)
    return path, runner, executor, request


@pytest.mark.parametrize(("result", "outcome", "status"), [
    (run_result(b"wrong\n"), "outputMismatch", "failed"),
    (run_result(exit_code=1), "runtimeError", "failed"),
    (run_result(exit_code=None, reason="wallTimeExceeded"), "timeLimitExceeded", "failed"),
    (run_result(exit_code=None, reason="infrastructureError"), "infrastructureError", "infrastructureError"),
])
def test_public_sample_execution_outcomes(tmp_path: Path, result, outcome: str, status: str) -> None:
    _, runner, executor, request = prepared_executor(tmp_path, [result])
    response = executor.run(request)
    validate("PublicSampleRunResult", response)
    assert response["status"] == status
    assert response["caseResults"][0]["outcome"] == outcome
    assert runner.requests[1].stdin == INTAKE["examples"][0]["input"].encode("utf-8")
    assert INTAKE["examples"][0]["output"].encode("utf-8") not in runner.requests[1].stdin
    assert "verdict" not in response and "ojResultId" not in response


def test_compile_error_stops_before_samples_and_pass_does_not_advance_asset(tmp_path: Path) -> None:
    path, runner, executor, request = prepared_executor(tmp_path, [])
    runner.results = iter([run_result(exit_code=1, stderr=b"syntax")])
    failed = executor.run(request)
    assert failed["caseResults"][0]["outcome"] == "compileError"
    assert len(runner.requests) == 1

    submissions = repository(path)
    original_goal = submissions.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    submissions.create_code_snapshot("goal-submit", "print('sample')\n", "python3")
    submission, _ = submissions.create_submission(submission_request())
    assert submission["status"] == "awaitingAssets"

    passing = FakeRunner([run_result(), run_result(INTAKE["examples"][0]["output"].encode("utf-8"))])
    result = PublicSampleExecutor(passing, SQLitePublicTestProvider(path)).run(request)
    assert result["status"] == "passed"
    assert VerifierAssetRepository(path).get("problem-submit", 1)["status"] == "building"
    assert SubmissionRepository(path).get_submission("submission-submit")["status"] == "awaitingAssets"
    assert SubmissionRepository(path).get_goal("goal-submit") == original_goal
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='oj_results'").fetchone()[0] == 0

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary
from services.oj_worker.judge import (
    InMemoryTrustedAssetProvider,
    JudgeFailure,
    JudgeSubmissionProcessor,
    OJResultRepository,
    TrustedTestBundle,
    TrustedTestCase,
    outputs_equal,
)
from services.oj_worker.queue import WorkItem, WorkerQueue
from services.oj_worker.worker import Worker
from tests.python.test_submission_storage import prepared_repository, submission_request


SUMMARY = ResourceSummary(7, 256 * 1024 * 1024, 1024, 1.0, 2, 32, 16 * 1024 * 1024)


def run_result(
    stdout: bytes = b"", *, stderr: bytes = b"", exit_code: int | None = 0,
    reason: str = "exited",
) -> PythonRunResult:
    return PythonRunResult(
        stdout, stderr, exit_code, reason,
        reason == "outputLimitExceeded", False,
        reason == "memoryLimitExceeded", SUMMARY,
    )


class FakeRunner:
    def __init__(self, results: list[PythonRunResult]) -> None:
        self.results = iter(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return next(self.results)


def item() -> WorkItem:
    return WorkItem(
        "submission-submit", "goal-submit", "problem-submit", 1, 1,
        "python3", "python-default-v1", "print(input())\n", "hash",
    )


def bundle(*cases: tuple[bytes, bytes]) -> TrustedTestBundle:
    return TrustedTestBundle(
        "asset-001", 1, "problem-submit", 1, "ready",
        tuple(
            TrustedTestCase(f"private-{index}", stdin, expected)
            for index, (stdin, expected) in enumerate(cases, start=1)
        ),
    )


def judge(tmp_path: Path, runner: FakeRunner, tests: TrustedTestBundle):
    repo = prepared_repository(tmp_path, "ready")
    repo.create_submission(submission_request())
    results = OJResultRepository(repo.path, id_factory=lambda: "oj-result-test")
    processor = JudgeSubmissionProcessor(
        runner, InMemoryTrustedAssetProvider([tests]), results,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(input())\n", encoding="utf-8")
    return repo, results, processor, workspace


@pytest.mark.parametrize(
    ("case_results", "expected_verdict", "category"),
    [
        ([run_result(), run_result(b"3\n")], "AC", None),
        ([run_result(), run_result(b"4\n")], "WA", None),
        ([run_result(), run_result(reason="wallTimeExceeded", exit_code=-9)], "TLE", "resourceLimit"),
        ([run_result(), run_result(exit_code=137)], "TLE", "resourceLimit"),
        ([run_result(), run_result(stderr=b"boom", exit_code=1)], "RE", "runtimeError"),
        ([run_result(stderr=b"SyntaxError", exit_code=1)], "CE", "syntaxError"),
    ],
)
def test_each_verdict_is_persisted(
    tmp_path: Path, case_results: list[PythonRunResult], expected_verdict: str,
    category: str | None,
) -> None:
    runner = FakeRunner(case_results)
    _, results, processor, workspace = judge(tmp_path, runner, bundle((b"1 2\n", b"3\n")))
    processor.process(item(), workspace)
    saved = results.get_for_submission("submission-submit")
    assert saved is not None
    assert saved["verdict"] == expected_verdict
    assert saved["stderrCategory"] == category
    assert runner.requests[0].operation == "compile"
    if len(runner.requests) > 1:
        assert runner.requests[1].stdin == b"1 2\n"


def test_output_comparison_is_only_tolerant_at_declared_boundaries() -> None:
    assert outputs_equal(b"3 4\r\n5 6", b"3 4\n5 6\n")
    assert not outputs_equal(b"3   4\n5 6\n", b"3 4\n5 6\n")
    assert not outputs_equal(b"3 4 5 6\n", b"3 4\n5 6\n")
    assert not outputs_equal(b"answer\n", b"Answer\n")
    assert not outputs_equal(b"3\n\n", b"3\n")


def test_hidden_expected_output_never_enters_runner_and_first_failure_stops(tmp_path: Path) -> None:
    runner = FakeRunner([run_result(), run_result(b"wrong")])
    tests = bundle((b"public-input", b"secret-answer"), (b"never-run", b"more-secret"))
    _, results, processor, workspace = judge(tmp_path, runner, tests)
    processor.process(item(), workspace)
    assert [request.stdin for request in runner.requests] == [b"", b"public-input"]
    assert all(b"secret" not in request.stdin for request in runner.requests)
    saved = results.get_for_submission("submission-submit")
    assert saved["testSummary"] == {"passed": 0, "total": 2, "firstFailedCase": 1}
    assert "secret" not in str(saved)


@pytest.mark.parametrize("reason", ["infrastructureError", "startupTimeExceeded"])
def test_infrastructure_failure_creates_no_verdict(tmp_path: Path, reason: str) -> None:
    runner = FakeRunner([run_result(reason=reason, exit_code=None)])
    _, results, processor, workspace = judge(tmp_path, runner, bundle((b"x", b"x")))
    with pytest.raises(JudgeFailure):
        processor.process(item(), workspace)
    assert results.get_for_submission("submission-submit") is None


def test_unready_empty_and_mismatched_assets_create_no_verdict(tmp_path: Path) -> None:
    for name, tests in [
        ("unready", replace(bundle((b"x", b"x")), status="building")),
        ("empty", bundle()),
        ("mismatch", replace(bundle((b"x", b"x")), problem_version=2)),
    ]:
        case = tmp_path / name
        runner = FakeRunner([])
        _, results, processor, workspace = judge(case, runner, tests)
        with pytest.raises(JudgeFailure):
            processor.process(item(), workspace)
        assert runner.requests == []
        assert results.get_for_submission("submission-submit") is None


def test_existing_result_is_reused_without_running_again(tmp_path: Path) -> None:
    runner = FakeRunner([run_result(), run_result(b"3")])
    _, results, processor, workspace = judge(tmp_path, runner, bundle((b"1 2", b"3")))
    processor.process(item(), workspace)
    processor.process(item(), workspace)
    assert len(runner.requests) == 2
    assert results.get_for_submission("submission-submit")["ojResultId"] == "oj-result-test"
    assert OJResultRepository(results.path).get_for_submission("submission-submit") is not None


def test_worker_saves_result_before_finished_and_fails_if_save_fails(tmp_path: Path) -> None:
    repo = prepared_repository(tmp_path, "ready")
    repo.create_submission(submission_request())
    tests = bundle((b"hello\n", b"hello\n"))
    result_repo = OJResultRepository(repo.path, id_factory=lambda: "oj-result-test")
    processor = JudgeSubmissionProcessor(
        FakeRunner([run_result(), run_result(b"hello\n")]),
        InMemoryTrustedAssetProvider([tests]), result_repo,
    )
    Worker(WorkerQueue(repo.path), processor, "worker", tmp_path / "work").process_once()
    assert repo.get_submission("submission-submit")["status"] == "finished"
    assert result_repo.get_for_submission("submission-submit")["verdict"] == "AC"

    failed = prepared_repository(tmp_path / "failed", "ready")
    failed.create_submission(submission_request())
    failing_repo = OJResultRepository(failed.path)
    def fail_save(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    failing_repo.save = fail_save  # type: ignore[method-assign]
    failing_processor = JudgeSubmissionProcessor(
        FakeRunner([run_result(), run_result(b"hello\n")]),
        InMemoryTrustedAssetProvider([tests]), failing_repo,
    )
    Worker(
        WorkerQueue(failed.path), failing_processor, "worker", tmp_path / "failed-work",
    ).process_once()
    assert failed.get_submission("submission-submit")["status"] == "failed"
    assert failing_repo.get_for_submission("submission-submit") is None

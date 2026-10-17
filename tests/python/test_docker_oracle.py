from __future__ import annotations

from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from apps.api.trusted_assets import OracleEvaluationFailure
from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary
from services.verifier_builder.docker_oracle import DockerOracleEvaluator


SUMMARY = ResourceSummary(1, 256 * 1024 * 1024, 1024, 1, 2, 32, 16 * 1024 * 1024)


def result(
    stdout: bytes = b"", *, reason: str = "exited", exit_code: int | None = 0,
) -> PythonRunResult:
    return PythonRunResult(stdout, b"", exit_code, reason, False, False, False, SUMMARY)


class FakeRunner:
    def __init__(self, results: list[PythonRunResult]) -> None:
        self.results = iter(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return next(self.results)


class CoordinatedRunner:
    def __init__(self) -> None:
        self.requests = []
        self.active = 0
        self.peak_active = 0
        self.lock = Lock()
        self.release = Event()

    def run(self, request):
        self.requests.append(request)
        if request.operation == "compile":
            return result()
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            if self.active >= 2:
                self.release.set()
        self.release.wait(timeout=1)
        with self.lock:
            self.active -= 1
        return result(b"output:" + request.stdin)


def test_oracle_compiles_once_runs_each_input_and_cleans_workspace(tmp_path: Path) -> None:
    runner = FakeRunner([result(), result(b"4\n"), result(b"9\n")])
    root = tmp_path / "oracle-spaces"
    outputs = DockerOracleEvaluator(runner, root, max_workers=1).evaluate(
        "n = int(input())\nprint(n * n)\n", (b"2\n", b"3\n"),
    )

    assert outputs == (b"4\n", b"9\n")
    assert runner.requests[0].operation == "compile"
    assert [request.stdin for request in runner.requests[1:]] == [b"2\n", b"3\n"]
    assert list(root.iterdir()) == []


def test_oracle_runs_private_inputs_concurrently_and_returns_original_order(
    tmp_path: Path,
) -> None:
    runner = CoordinatedRunner()
    inputs = tuple(f"case-{index}".encode() for index in range(6))

    outputs = DockerOracleEvaluator(runner, tmp_path / "parallel-oracle-spaces").evaluate(
        "print(input())\n", inputs,
    )

    assert runner.peak_active > 1
    assert outputs == tuple(b"output:" + value for value in inputs)
    assert sum(request.operation == "compile" for request in runner.requests) == 1
    assert sorted(request.stdin for request in runner.requests if request.operation == "execute") == sorted(inputs)
    assert list((tmp_path / "parallel-oracle-spaces").iterdir()) == []


def test_oracle_parallelism_is_configurable_but_never_exceeds_fixed_cap(
    tmp_path: Path,
) -> None:
    runner = CoordinatedRunner()
    inputs = tuple(f"case-{index}".encode() for index in range(6))

    DockerOracleEvaluator(
        runner, tmp_path / "two-worker-oracle-spaces", max_workers=2,
    ).evaluate("print(input())\n", inputs)

    assert runner.peak_active == 2
    with pytest.raises(ValueError, match="max_workers"):
        DockerOracleEvaluator(runner, tmp_path / "too-many-workers", max_workers=5)


def test_oracle_keeps_first_input_failure_semantics_and_waits_for_parallel_cleanup(
    tmp_path: Path,
) -> None:
    all_started = Barrier(3)
    slow_finished = Event()

    class FailingRunner:
        def run(self, request):
            if request.operation == "compile":
                return result()
            all_started.wait(timeout=1)
            if request.stdin == b"first":
                return result(exit_code=1)
            if request.stdin == b"second":
                raise RuntimeError("runner unavailable")
            Event().wait(timeout=0.05)
            slow_finished.set()
            return result(b"unused")

    root = tmp_path / "failed-parallel-oracle-spaces"
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(FailingRunner(), root).evaluate(
            "print(input())\n", (b"first", b"second", b"slow"),
        )

    assert caught.value.code == "ORACLE_RUNTIME_ERROR"
    assert slow_finished.is_set()
    assert list(root.iterdir()) == []


def test_oracle_parallel_batch_honors_absolute_deadline_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    now = [10.0]
    started = []
    lock = Lock()
    release = Event()

    class DeadlineRunner:
        def run(self, request):
            if request.operation == "compile":
                return result()
            with lock:
                started.append(request)
                if len(started) == 4:
                    now[0] = 12.0
                    release.set()
            release.wait(timeout=1)
            return result(b"unused")

    root = tmp_path / "deadline-parallel-oracle-spaces"
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(
            DeadlineRunner(), root, deadline=12.0, monotonic=lambda: now[0],
        ).evaluate("print(input())\n", tuple(f"case-{index}".encode() for index in range(8)))

    assert caught.value.code == "ORACLE_BUILD_BUDGET_EXCEEDED"
    assert len(started) == 4
    assert all(request.policy.wall_time_seconds == 2.0 for request in started)
    assert list(root.iterdir()) == []


def test_oracle_checks_total_output_while_consuming_bounded_batch(
    tmp_path: Path,
) -> None:
    executed = []

    class LargeOutputRunner:
        def run(self, request):
            if request.operation == "compile":
                return result()
            executed.append(request.stdin)
            return result(b"ab")

    root = tmp_path / "bounded-batch-oracle-spaces"
    inputs = tuple(f"case-{index}".encode() for index in range(8))
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(
            LargeOutputRunner(), root, max_total_output_bytes=3, max_workers=2,
        ).evaluate("print(input())\n", inputs)

    assert caught.value.code == "ORACLE_MATERIAL_TOO_LARGE"
    assert set(executed) <= set(inputs[:3])
    assert len(executed) <= 3
    assert list(root.iterdir()) == []


def test_oracle_stops_when_combined_outputs_exceed_material_limit(tmp_path: Path) -> None:
    runner = FakeRunner([result(), result(b"ab"), result(b"cd")])
    root = tmp_path / "bounded-oracle-spaces"
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(
            runner, root, max_total_output_bytes=3,
        ).evaluate("print('answer')\n", (b"one\n", b"two\n"))
    assert caught.value.code == "ORACLE_MATERIAL_TOO_LARGE"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(("run_result", "code"), [
    (result(reason="infrastructureError", exit_code=None), "ORACLE_INFRASTRUCTURE_ERROR"),
    (result(reason="wallTimeExceeded", exit_code=None), "ORACLE_TIMEOUT"),
    (result(reason="outputLimitExceeded", exit_code=-9), "ORACLE_OUTPUT_TOO_LARGE"),
    (result(reason="memoryLimitExceeded", exit_code=137), "ORACLE_RESOURCE_LIMIT"),
    (result(exit_code=1), "ORACLE_RUNTIME_ERROR"),
])
def test_oracle_execution_failures_are_safe_and_cleanup(
    tmp_path: Path, run_result: PythonRunResult, code: str,
) -> None:
    runner = FakeRunner([result(), run_result])
    root = tmp_path / code
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(runner, root).evaluate("print(input())\n", (b"hidden\n",))
    assert caught.value.code == code
    assert "hidden" not in caught.value.message
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(("compile_result", "code"), [
    (result(reason="infrastructureError", exit_code=None), "ORACLE_INFRASTRUCTURE_ERROR"),
    (result(exit_code=1), "ORACLE_COMPILE_ERROR"),
])
def test_oracle_compile_failure_never_runs_private_input(
    tmp_path: Path, compile_result: PythonRunResult, code: str,
) -> None:
    runner = FakeRunner([compile_result])
    root = tmp_path / code
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(runner, root).evaluate("broken", (b"private\n",))
    assert caught.value.code == code
    assert len(runner.requests) == 1
    assert list(root.iterdir()) == []

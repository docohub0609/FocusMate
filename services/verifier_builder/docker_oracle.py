"""Evaluate a candidate reference Oracle in isolated one-input Docker runs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math
import shutil
import tempfile
import time
from typing import Callable

from apps.api.trusted_assets import OracleEvaluationFailure
from services.oj_worker.docker_runner import DockerPythonRunner, PythonRunRequest, ResourcePolicy


MAX_ORACLE_WORKERS = 4


class DockerOracleEvaluator:
    def __init__(
        self,
        runner: DockerPythonRunner,
        workspace_root: str | Path,
        *,
        max_total_output_bytes: int = 16 * 1024 * 1024,
        max_workers: int = MAX_ORACLE_WORKERS,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_workers) is not int or not 1 <= max_workers <= MAX_ORACLE_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {MAX_ORACLE_WORKERS}.")
        self.runner = runner
        self.workspace_root = Path(workspace_root)
        self.max_total_output_bytes = max_total_output_bytes
        self.max_workers = max_workers
        self.deadline = deadline
        self.monotonic = monotonic

    def _remaining(self) -> float:
        remaining = 3.0 if self.deadline is None else self.deadline - self.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise OracleEvaluationFailure("ORACLE_BUILD_BUDGET_EXCEEDED", "The verifier asset build exceeded its time budget.")
        return min(3.0, remaining)

    def _run(self, workspace: Path, stdin: bytes, *, operation: str = "execute"):
        policy = ResourcePolicy(wall_time_seconds=self._remaining())
        try:
            result = self.runner.run(PythonRunRequest(workspace, stdin, policy, operation))
        except Exception:
            raise OracleEvaluationFailure("ORACLE_INFRASTRUCTURE_ERROR", "The Oracle sandbox is unavailable.") from None
        self._remaining()
        return result

    @staticmethod
    def _checked_output(result) -> bytes:
        if result.termination_reason == "infrastructureError":
            raise OracleEvaluationFailure("ORACLE_INFRASTRUCTURE_ERROR", "The Oracle sandbox is unavailable.")
        if result.termination_reason == "wallTimeExceeded":
            raise OracleEvaluationFailure("ORACLE_TIMEOUT", "The reference Oracle exceeded its time limit.")
        if result.termination_reason == "outputLimitExceeded":
            raise OracleEvaluationFailure("ORACLE_OUTPUT_TOO_LARGE", "A reference Oracle output exceeded its limit.")
        if result.termination_reason == "memoryLimitExceeded":
            raise OracleEvaluationFailure("ORACLE_RESOURCE_LIMIT", "The reference Oracle exceeded its memory limit.")
        if result.termination_reason != "exited" or result.exit_code != 0:
            raise OracleEvaluationFailure("ORACLE_RUNTIME_ERROR", "The reference Oracle failed for a private input.")
        return result.stdout

    def evaluate(self, source_code: str, inputs: tuple[bytes, ...]) -> tuple[bytes, ...]:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="focusmate-oracle-", dir=self.workspace_root))
        try:
            (workspace / "main.py").write_text(source_code, encoding="utf-8")
            compiled = self._run(workspace, b"", operation="compile")
            if compiled.termination_reason == "infrastructureError":
                raise OracleEvaluationFailure("ORACLE_INFRASTRUCTURE_ERROR", "The Oracle sandbox is unavailable.")
            if compiled.termination_reason != "exited" or compiled.exit_code != 0:
                raise OracleEvaluationFailure("ORACLE_COMPILE_ERROR", "The reference Oracle does not compile.")
            outputs: list[bytes] = []
            total_output_bytes = 0
            if inputs:
                executor = ThreadPoolExecutor(max_workers=min(self.max_workers, len(inputs)))
                futures = {}
                next_input = 0
                try:
                    while next_input < min(self.max_workers, len(inputs)):
                        futures[next_input] = executor.submit(self._run, workspace, inputs[next_input])
                        next_input += 1
                    for index in range(len(inputs)):
                        output = self._checked_output(futures.pop(index).result())
                        total_output_bytes += len(output)
                        if total_output_bytes > self.max_total_output_bytes:
                            raise OracleEvaluationFailure(
                                "ORACLE_MATERIAL_TOO_LARGE",
                                "The combined reference Oracle output exceeded the material limit.",
                            )
                        outputs.append(output)
                        if next_input < len(inputs):
                            futures[next_input] = executor.submit(self._run, workspace, inputs[next_input])
                            next_input += 1
                finally:
                    for future in futures.values():
                        future.cancel()
                    executor.shutdown(wait=True, cancel_futures=True)
            return tuple(outputs)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

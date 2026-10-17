"""Run public samples without creating a Submission, Verdict, or OJResult."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable, Protocol
from uuid import uuid4

from .docker_runner import DEFAULT_POLICY, PythonRunRequest, PythonRunResult
from .judge import outputs_equal
from .queue import timestamp


@dataclass(frozen=True)
class PublicTestCase:
    test_case_id: str
    stdin: bytes
    expected_output: bytes


@dataclass(frozen=True)
class PublicTestBundle:
    asset_id: str
    asset_version: int
    problem_id: str
    problem_version: int
    cases: tuple[PublicTestCase, ...]


@dataclass(frozen=True)
class PublicSampleRunRequest:
    goal_id: str
    problem_id: str
    problem_version: int
    code_version: int
    workspace: Path
    resource_policy_ref: str = "python-default-v1"


class PythonRunner(Protocol):
    def run(self, request: PythonRunRequest) -> PythonRunResult: ...


class PublicTestProvider(Protocol):
    def get(self, problem_id: str, problem_version: int) -> PublicTestBundle: ...


class PublicSampleFailure(Exception):
    pass


class SQLitePublicTestProvider:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self, problem_id: str, problem_version: int) -> PublicTestBundle:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM public_test_cases WHERE problem_id=? AND problem_version=? ORDER BY example_index",
                              (problem_id, problem_version)).fetchall()
        if not rows:
            raise PublicSampleFailure("Public samples are unavailable for this problem version.")
        first = rows[0]
        return PublicTestBundle(first["asset_id"], first["asset_version"], problem_id, problem_version,
                                tuple(PublicTestCase(row["test_case_id"], row["stdin_text"].encode("utf-8"),
                                                     row["expected_output_text"].encode("utf-8")) for row in rows))


class PublicSampleExecutor:
    def __init__(self, runner: PythonRunner, tests: PublicTestProvider, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 id_factory: Callable[[], str] = lambda: f"sample-run-{uuid4()}") -> None:
        self.runner, self.tests, self.clock, self.id_factory = runner, tests, clock, id_factory

    def run(self, request: PublicSampleRunRequest) -> dict[str, object]:
        request_object = {"goalContext": {"goalId": request.goal_id, "scope": "goal"},
                          "problemRef": {"problemId": request.problem_id, "problemVersion": request.problem_version},
                          "codeVersion": request.code_version, "resourcePolicyRef": request.resource_policy_ref}
        if not request.goal_id or request.problem_version < 1 or request.code_version < 1:
            raise PublicSampleFailure("The public sample run context is incomplete.")
        if request.resource_policy_ref != DEFAULT_POLICY.policy_ref:
            raise PublicSampleFailure("The public sample resource policy is unsupported.")
        bundle = self.tests.get(request.problem_id, request.problem_version)
        compile_result = self.runner.run(PythonRunRequest(request.workspace, b"", operation="compile"))
        if compile_result.termination_reason == "infrastructureError":
            outcomes = [(case.test_case_id, "infrastructureError") for case in bundle.cases]
        elif compile_result.termination_reason != "exited" or compile_result.exit_code != 0:
            outcomes = [(case.test_case_id, "compileError") for case in bundle.cases]
        else:
            outcomes = [(case.test_case_id, self._run_case(request.workspace, case)) for case in bundle.cases]
        values = [outcome for _, outcome in outcomes]
        status = "infrastructureError" if "infrastructureError" in values else ("passed" if all(x == "passed" for x in values) else "failed")
        result: dict[str, object] = {"runId": self.id_factory(), "schemaVersion": "1.0.0",
            "createdAt": timestamp(self.clock()), "goalContext": request_object["goalContext"],
            "problemRef": request_object["problemRef"], "codeVersion": request.code_version,
            "verifierAssetRef": {"assetId": bundle.asset_id, "assetVersion": bundle.asset_version},
            "status": status, "caseResults": [{"testCaseId": key, "outcome": value} for key, value in outcomes]}
        return result

    def _run_case(self, workspace: Path, case: PublicTestCase) -> str:
        result = self.runner.run(PythonRunRequest(workspace, case.stdin))
        if result.termination_reason == "infrastructureError": return "infrastructureError"
        if result.termination_reason == "wallTimeExceeded": return "timeLimitExceeded"
        if result.termination_reason != "exited" or result.exit_code != 0: return "runtimeError"
        return "passed" if outputs_equal(result.stdout, case.expected_output) else "outputMismatch"

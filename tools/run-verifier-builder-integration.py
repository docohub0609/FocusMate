"""Run T033 through the production Worker with a real Docker boundary.

The default run uses deterministic local candidates and never calls a model.
Set RUN_DEEPSEEK_ASSET_INTEGRATION=1 for the explicit paid provider run.
Only a fixed, non-secret summary is written to the console.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from textwrap import dedent
import time
from typing import Any

# A directly executed script starts with ``tools`` on sys.path.  Resolve the
# repository root from this file instead of relying on pytest or the caller's
# current directory to make application packages importable.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deepseek_provider import DeepSeekConfig, StructuredChatTransport
from apps.api.problem_features import build_human_feature_set
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository
from apps.api.verifier_assets import VerifierAssetRepository
from services.oj_worker.docker_runner import DockerPythonRunner
from services.verifier_builder.build_jobs import VerifierBuildJobRepository
from services.verifier_builder.candidates import (
    CandidateFailure,
    CandidateProvider,
    CandidateSet,
    CoverageClaim,
    MutantCandidate,
    ProgramCandidate,
)
from services.verifier_builder.worker import VerifierBuilderWorker

IMAGE = "focusmate-python-runner:v1"
PROBLEM_REF = {
    "problemId": "problem-verifier-integration",
    "problemVersion": 1,
}


@dataclass(frozen=True)
class IntegrationSummary:
    problem_ref: dict[str, Any]
    model: str
    job_status: str
    phase: str | None
    issue_code: str | None
    elapsed_ms: int
    small_count: int
    large_count: int
    agreement_count: int
    mutation_attempted: int
    mutation_eligible: int
    mutation_killed: int
    final_asset_status: str

    def to_public_dict(self) -> dict[str, Any]:
        """Return an explicit allow-list; internal records never reach stdout."""

        return {
            "problemRef": dict(self.problem_ref),
            "model": self.model,
            "phase": {
                "jobStatus": self.job_status,
                "name": self.phase,
                "issueCode": self.issue_code,
            },
            "elapsedMs": self.elapsed_ms,
            "counts": {
                "small": self.small_count,
                "large": self.large_count,
                "agreement": self.agreement_count,
                "mutationAttempted": self.mutation_attempted,
                "mutationEligible": self.mutation_eligible,
                "mutationKilled": self.mutation_killed,
            },
            "finalAssetStatus": self.final_asset_status,
        }


class DeterministicCandidateProvider:
    """A no-charge provider used only to test the real Docker/SQLite boundary."""

    model = "fake-deterministic-v1"

    def generate(self, problem_record: dict[str, Any], feature_set: dict[str, Any], deadline: float) -> CandidateSet:
        if time.monotonic() >= deadline:
            raise CandidateFailure(
                "BUILD_BUDGET_EXCEEDED",
                "The verifier asset build exceeded its time budget.",
                True,
            )
        problem = problem_record["problemPackage"]
        lifecycle = problem_record["lifecycle"]
        confirmed = tuple(
            item["featureKey"]
            for item in feature_set["features"]
            if item["status"] == "confirmed" and "review" not in item
        )
        generator = dedent(
            """
            import json
            import random
            import sys

            control = json.load(sys.stdin)
            rng = random.Random(control["seed"])
            cases = [{
                "caseKey": "small-single-negative",
                "scale": "small",
                "tags": ["minimumSize", "singleElement", "negativeValues"],
                "stdin": "1\\n-5\\n",
            }]
            for index in range(1, control["smallTarget"]):
                n = 2 + ((index - 1) % 7)
                values = [rng.randint(-9, 9) for _ in range(n)]
                # The seeded suffix remains pseudo-random while the first value
                # gives every generated stdin a deterministic unique marker.
                values[0] = -index
                cases.append({
                    "caseKey": f"small-random-{index}",
                    "scale": "small",
                    "tags": ["randomSmall", "negativeValues"],
                    "stdin": f"{n}\\n{' '.join(map(str, values))}\\n",
                })
            print(json.dumps({"cases": cases}, separators=(",", ":")))
            """
        ).lstrip()
        slow = dedent(
            """
            import sys

            values = list(map(int, sys.stdin.buffer.read().split()))
            n = values[0]
            answer = 0
            for index in range(n):
                answer += values[index + 1]
            print(answer)
            """
        ).lstrip()
        fast = dedent(
            """
            import sys

            data = list(map(int, sys.stdin.buffer.read().split()))
            n = data[0]
            print(sum(data[1:1 + n]))
            """
        ).lstrip()
        mutant = dedent(
            """
            import sys

            data = list(map(int, sys.stdin.buffer.read().split()))
            n = data[0]
            print(sum(value for value in data[1:1 + n] if value > 0))
            """
        ).lstrip()

        def program(source: str, role: str) -> ProgramCandidate:
            return ProgramCandidate(
                source,
                "fake",
                self.model,
                f"fake-response-{role}",
                f"fake-{role}-v1",
            )

        return CandidateSet(
            problem["problemId"],
            problem["version"],
            lifecycle["contentHash"],
            feature_set["featureSetId"],
            confirmed,
            program(generator, "generator"),
            (
                CoverageClaim("randomSmall", ("dataScale.n",)),
                CoverageClaim("minimumSize", ("dataScale.n", "input.primary")),
            ),
            program(slow, "slow-oracle"),
            program(fast, "fast-reference"),
            (MutantCandidate("positive-only", "wrongInitialization", mutant),),
        )


def ensure_runner_image() -> None:
    """Check Docker and build the fixed runner image when it is absent."""

    if shutil.which("docker") is None:
        raise RuntimeError("The Docker CLI is unavailable.")
    info = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
        shell=False,
    )
    if info.returncode != 0:
        raise RuntimeError("The Docker daemon is unavailable.")
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
        shell=False,
    )
    if inspect.returncode == 0:
        return
    build = subprocess.run(
        [
            "docker", "build", "-t", IMAGE,
            str(ROOT / "services" / "oj_worker" / "runner-image"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=300,
        check=False,
        shell=False,
    )
    if build.returncode != 0:
        raise RuntimeError("The fixed Python runner image could not be built.")


def real_candidate_provider() -> tuple[CandidateProvider, str]:
    """Load the existing local provider configuration without exposing its key."""

    config = DeepSeekConfig.from_environment()
    provider = CandidateProvider(StructuredChatTransport(config))
    return provider, _safe_model_label(config.model)


def _safe_model_label(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._:/")
    if 1 <= len(value) <= 128 and set(value) <= allowed:
        return value
    return "configured-model"


def _create_frozen_problem(database: Path) -> dict[str, Any]:
    package = create_problem_package(
        {
            "statement": "Given n signed integers, output the sum of all n values.",
            "inputSpec": "The first line contains integer n. The second line contains n signed integers.",
            "outputSpec": "Output one integer: the sum of all input values.",
            "constraints": "1 <= n <= 8 and -100 <= ai <= 100.",
            "examples": [{"input": "3\n1 2 3\n", "output": "6\n"}],
            "language": "python3",
            "ioMode": "standardInputOutput",
        },
        id_factory=lambda: PROBLEM_REF["problemId"],
    )
    repository = ProblemRepository(database)
    draft = repository.create(package)
    request = {
        "expectedDraftRevision": 1,
        "features": [
            {
                "featureKey": "dataScale.n",
                "category": "dataScale",
                "status": "confirmed",
                "value": {
                    "variable": "n", "minimum": 1, "maximum": 8,
                    "elementMinimum": -100, "elementMaximum": 100,
                },
                "confidence": 1,
                "reason": "The input size and value bounds are explicit.",
                "evidenceRefs": ["constraints:0"],
            },
            {
                "featureKey": "input.primary",
                "category": "inputStructure",
                "status": "confirmed",
                "value": {
                    "kind": "integerSequence", "lengthVariable": "n",
                    "elementType": "integer",
                },
                "confidence": 1,
                "reason": "The two-line integer input is explicit.",
                "evidenceRefs": ["inputSpec:0"],
            },
            {
                "featureKey": "output.primary",
                "category": "outputStructure",
                "status": "confirmed",
                "value": {"valueCount": 1, "valueType": "integer", "meaning": "sum"},
                "confidence": 1,
                "reason": "The single integer output is explicit.",
                "evidenceRefs": ["outputSpec:0"],
            },
            {
                "featureKey": "resource.primary",
                "category": "resourceConstraint",
                "status": "unknown",
                "reason": "The integration problem does not state time or memory limits.",
                "evidenceRefs": [],
            },
            {
                "featureKey": "domain.array",
                "category": "domainTag",
                "status": "confirmed",
                "value": "array",
                "confidence": 1,
                "reason": "The statement describes a sequence of signed integers.",
                "evidenceRefs": ["statement:0"],
            },
        ],
    }
    features = build_human_feature_set(
        draft["problemPackage"],
        request,
        id_factory=lambda: "features-verifier-integration",
    )
    return repository.confirm_and_freeze(
        PROBLEM_REF["problemId"], 1, features, 1, None,
    )


def run_integration(provider: Any, model: str, root: Path) -> IntegrationSummary:
    """Use the real Worker, repositories, state gates and Docker runner."""

    root.mkdir(parents=True, exist_ok=True)
    database = root / "verifier-builder-integration.db"
    workspace = root / "workspaces"
    workspace.mkdir(parents=True, exist_ok=True)
    _create_frozen_problem(database)
    jobs = VerifierBuildJobRepository(database)
    worker = VerifierBuilderWorker(
        jobs,
        provider,
        DockerPythonRunner(image=IMAGE),
        "builder-integration",
        workspace,
    )
    started = time.monotonic()
    worker.run_once()
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    job = jobs.get(PROBLEM_REF["problemId"], 1)
    asset = VerifierAssetRepository(database).get(PROBLEM_REF["problemId"], 1)
    counts = {
        "smallCount": 0,
        "largeCount": 0,
        "agreementCount": 0,
        "mutationAttempted": 0,
        "mutationEligible": 0,
        "mutationKilled": 0,
    }
    # sqlite3's context manager commits/rolls back but does not close.  Close
    # explicitly so Windows can remove the one-shot integration directory.
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT record_json FROM verifier_cross_check_runs ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
    if row is not None:
        record = json.loads(row[0])
        counts = {name: int(record.get(name, 0)) for name in counts}
    return IntegrationSummary(
        dict(PROBLEM_REF),
        _safe_model_label(model),
        job["status"],
        job["phase"],
        job["issueCode"],
        elapsed_ms,
        counts["smallCount"],
        counts["largeCount"],
        counts["agreementCount"],
        counts["mutationAttempted"],
        counts["mutationEligible"],
        counts["mutationKilled"],
        asset["status"],
    )


def _failure_summary(model: str, issue_code: str) -> IntegrationSummary:
    return IntegrationSummary(
        dict(PROBLEM_REF), _safe_model_label(model), "unavailable", None,
        issue_code, 0, 0, 0, 0, 0, 0, 0, "unavailable",
    )


def main() -> int:
    model = "fake-deterministic-v1"
    try:
        ensure_runner_image()
        if os.getenv("RUN_DEEPSEEK_ASSET_INTEGRATION") == "1":
            provider, model = real_candidate_provider()
        else:
            provider = DeterministicCandidateProvider()
        with tempfile.TemporaryDirectory(prefix="focusmate-verifier-integration-") as directory:
            summary = run_integration(provider, model, Path(directory))
    except CandidateFailure as error:
        summary = _failure_summary(model, error.code)
    except Exception:
        summary = _failure_summary(model, "VERIFIER_BUILDER_INTERNAL_ERROR")
    print(json.dumps(summary.to_public_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0 if (
        summary.final_asset_status == "ready"
        and summary.job_status == "completed"
        and summary.elapsed_ms < 120_000
        and summary.mutation_eligible >= 1
        and summary.mutation_killed == summary.mutation_eligible
    ) else 1


if __name__ == "__main__":
    sys.exit(main())

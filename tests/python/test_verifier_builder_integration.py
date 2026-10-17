from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tools" / "run-verifier-builder-integration.py"


def load_harness():
    """Load the user-facing integration command without making tools a package."""

    spec = importlib.util.spec_from_file_location(
        "focusmate_verifier_builder_integration", TOOL_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The verifier-builder integration harness is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_script_bootstraps_repository_imports_outside_pytest(tmp_path: Path) -> None:
    """A direct tool launch cannot rely on pytest adding the repo to sys.path."""

    probe = (
        "import runpy; "
        f"runpy.run_path({str(TOOL_PATH)!r}, run_name='integration_import_probe')"
    )
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=clean_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""


def test_harness_console_contains_only_the_safe_summary(monkeypatch, capsys) -> None:
    harness = load_harness()
    summary = harness.IntegrationSummary(
        problem_ref={"problemId": "problem-integration", "problemVersion": 1},
        model="fake-deterministic-v1",
        job_status="completed",
        phase=None,
        issue_code=None,
        elapsed_ms=1234,
        small_count=8,
        large_count=0,
        agreement_count=8,
        mutation_attempted=1,
        mutation_eligible=1,
        mutation_killed=1,
        final_asset_status="ready",
    )
    monkeypatch.delenv("RUN_DEEPSEEK_ASSET_INTEGRATION", raising=False)
    monkeypatch.setattr(
        harness,
        "real_candidate_provider",
        lambda: (_ for _ in ()).throw(AssertionError("paid provider was selected")),
    )
    monkeypatch.setattr(harness, "ensure_runner_image", lambda: None)
    monkeypatch.setattr(harness, "run_integration", lambda *_args, **_kwargs: summary)

    assert harness.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "problemRef", "model", "phase", "elapsedMs", "counts",
        "finalAssetStatus",
    }
    assert set(payload["phase"]) == {"jobStatus", "name", "issueCode"}
    assert set(payload["counts"]) == {
        "small", "large", "agreement", "mutationAttempted",
        "mutationEligible", "mutationKilled",
    }
    assert "source" not in json.dumps(payload).lower()
    assert "stdin" not in json.dumps(payload).lower()
    assert "expected" not in json.dumps(payload).lower()


def test_fake_provider_generates_24_unique_small_inputs() -> None:
    harness = load_harness()
    candidates = harness.DeterministicCandidateProvider().generate(
        {
            "problemPackage": {"problemId": "problem", "version": 1},
            "lifecycle": {"contentHash": "sha256:" + "0" * 64},
        },
        {
            "featureSetId": "features",
            "features": [
                {"featureKey": "dataScale.n", "status": "confirmed"},
                {"featureKey": "input.primary", "status": "confirmed"},
            ],
        },
        time.monotonic() + 5,
    )
    completed = subprocess.run(
        [sys.executable, "-c", candidates.generator.source],
        input=json.dumps(
            {
                "schemaVersion": "1.0.0",
                "seed": 123,
                "smallTarget": 24,
                "largeTarget": 0,
            },
        ),
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    cases = json.loads(completed.stdout)["cases"]

    assert len(cases) == 24
    assert {case["scale"] for case in cases} == {"small"}
    assert len({case["caseKey"] for case in cases}) == 24
    assert len({case["stdin"] for case in cases}) == 24
    assert "randomSmall" not in cases[0]["tags"]
    assert all("randomSmall" in case["tags"] for case in cases[1:])


@pytest.mark.skipif(
    os.getenv("RUN_DOCKER_VERIFIER_BUILDER_INTEGRATION") != "1",
    reason="set RUN_DOCKER_VERIFIER_BUILDER_INTEGRATION=1 for the real Docker boundary",
)
def test_default_cli_reaches_ready_through_real_worker_and_docker() -> None:
    environment = os.environ.copy()
    environment.pop("RUN_DEEPSEEK_ASSET_INTEGRATION", None)
    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=130,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)

    assert summary["model"] == "fake-deterministic-v1"
    assert summary["finalAssetStatus"] == "ready"
    assert summary["phase"]["jobStatus"] == "completed"
    assert summary["elapsedMs"] < 120_000
    assert summary["counts"]["small"] == summary["counts"]["agreement"] == 24
    assert summary["counts"]["large"] == 0
    assert summary["counts"]["mutationAttempted"] == 1
    assert summary["counts"]["mutationEligible"] == summary["counts"]["mutationKilled"] == 1


@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_ASSET_INTEGRATION") != "1",
    reason="set RUN_DEEPSEEK_ASSET_INTEGRATION=1 for the explicit paid integration",
)
def test_real_deepseek_and_docker_build_a_ready_asset_within_budget(tmp_path: Path) -> None:
    harness = load_harness()
    harness.ensure_runner_image()
    provider, model = harness.real_candidate_provider()

    summary = harness.run_integration(provider, model, tmp_path)

    assert summary.final_asset_status == "ready"
    assert summary.job_status == "completed"
    assert summary.elapsed_ms < 120_000
    assert summary.small_count >= 8
    assert summary.small_count == summary.agreement_count
    assert 0 <= summary.large_count <= 4
    assert summary.mutation_eligible >= 1
    assert summary.mutation_killed == summary.mutation_eligible

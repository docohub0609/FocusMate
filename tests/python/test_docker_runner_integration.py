from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from services.oj_worker.docker_runner import DockerPythonRunner, PythonRunRequest
from services.oj_worker.judge import (
    InMemoryTrustedAssetProvider,
    JudgeSubmissionProcessor,
    OJResultRepository,
    TrustedTestBundle,
    TrustedTestCase,
)
from services.oj_worker.queue import WorkItem
from apps.api.public_tests import PublicTestCaseRepository
from apps.api.trusted_assets import TrustedAssetBuilder
from apps.api.verifier_assets import VerifierAssetRepository
from services.oj_worker.public_samples import (
    PublicSampleExecutor, PublicSampleRunRequest, SQLitePublicTestProvider,
)
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.verifier_builder.docker_oracle import DockerOracleEvaluator
from tests.python.test_submission_storage import prepared_repository, submission_request
from tests.python.test_submission_storage import frozen_problem


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_OJ_INTEGRATION") != "1",
    reason="set RUN_DOCKER_OJ_INTEGRATION=1 to run real Docker isolation tests",
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "focusmate-python-runner:v1"


@pytest.fixture(scope="session", autouse=True)
def docker_image() -> None:
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required for the explicit OJ integration test.")
    info = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=15, check=False,
    )
    if info.returncode != 0:
        pytest.fail("Docker daemon is not available. Start Docker Desktop first.")
    build = subprocess.run(
        [
            "docker", "build", "-t", IMAGE,
            str(ROOT / "services" / "oj_worker" / "runner-image"),
        ],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if build.returncode != 0:
        pytest.fail("The FocusMate runner image could not be built.")


def run_code(tmp_path: Path, source: str, stdin: bytes = b""):
    (tmp_path / "main.py").write_text(source, encoding="utf-8")
    return DockerPythonRunner(image=IMAGE).run(PythonRunRequest(tmp_path, stdin))


def test_real_container_runs_one_stdin_and_captures_output(tmp_path: Path) -> None:
    result = run_code(
        tmp_path,
        "a, b = map(int, input().split())\nprint(a + b)\n",
        b"2 3\n",
    )
    assert result.termination_reason == "exited"
    assert result.exit_code == 0
    assert result.stdout == b"5\n"
    assert result.stderr == b""


def test_real_container_runs_and_compares_one_registered_public_sample(tmp_path: Path) -> None:
    database = tmp_path / "public-sample.db"
    frozen_problem(database)
    PublicTestCaseRepository(database).register_examples("problem-submit", 1)
    workspace = tmp_path / "public-workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text(
        "input()\nvalues = list(map(int, input().split()))\nprint('1 2')\n",
        encoding="utf-8",
    )
    request = PublicSampleRunRequest("goal-public", "problem-submit", 1, 1, workspace)
    result = PublicSampleExecutor(
        DockerPythonRunner(image=IMAGE), SQLitePublicTestProvider(database),
    ).run(request)
    assert result["status"] == "passed"
    assert result["caseResults"][0]["outcome"] == "passed"


def test_real_oracle_precomputes_private_answers_before_material_is_sealed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "trusted-material.db"
    frozen_problem(database)
    oracle_workspaces = tmp_path / "oracle-workspaces"
    builder = TrustedAssetBuilder(
        database,
        evaluator=DockerOracleEvaluator(
            DockerPythonRunner(image=IMAGE), oracle_workspaces,
        ),
    )
    material = builder.build({
        "manifestId": "manifest-real-oracle",
        "schemaVersion": "1.0.0",
        "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
        "privateTests": [
            {
                "caseKey": "two-values",
                "stdin": "2\n8 9\n",
                "answerSource": "referenceOracle",
            },
        ],
        "referenceOracle": {
            "language": "python3",
            "sourceCode": "input()\ninput()\nprint('1 2')\n",
            "origin": "internalProvided",
            "resourcePolicyRef": "python-default-v1",
        },
    })

    assert builder.list_private("problem-submit", 1)[0]["expectedOutput"] == "1 2\n"
    assert VerifierAssetRepository(database).get("problem-submit", 1)["status"] == "crossChecking"
    assert list(oracle_workspaces.iterdir()) == []

    # T033 will create this report in production. Here it lets us prove that
    # the real SQLite provider can reopen the exact material built above.
    VerifierAssetRepository(database).complete_cross_check(
        "problem-submit", 1, {
            "reportId": "report-real-oracle",
            "outcome": "passed",
            "checkedMaterialHash": material["materialHash"],
            "checksRun": ["referenceAgreement"],
            "createdAt": "2026-09-06T12:00:00Z",
        },
    )
    bundle = SQLiteTrustedAssetProvider(database).get("problem-submit", 1)
    assert len(bundle.cases) == 2
    assert bundle.cases[-1].stdin == b"2\n8 9\n"
    assert bundle.cases[-1].expected_output == b"1 2\n"


def test_real_container_stops_infinite_loop(tmp_path: Path) -> None:
    result = run_code(tmp_path, "while True:\n    pass\n")
    assert result.resource_summary.wall_time_ms < 6000
    assert result.exit_code != 0 or result.termination_reason == "wallTimeExceeded"


def test_real_container_limits_memory(tmp_path: Path) -> None:
    result = run_code(tmp_path, "x = []\nwhile True:\n    x.append(bytearray(10_000_000))\n")
    assert result.exit_code != 0
    assert result.oom_killed or b"MemoryError" in result.stderr


def test_real_container_limits_output(tmp_path: Path) -> None:
    result = run_code(tmp_path, "while True:\n    print('x' * 1000)\n")
    assert result.termination_reason == "outputLimitExceeded"
    assert result.stdout_truncated is True
    assert len(result.stdout) == 1024 * 1024


def test_real_container_limits_processes(tmp_path: Path) -> None:
    result = run_code(
        tmp_path,
        """import os
children = []
try:
    while True:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        children.append(pid)
except OSError:
    print('process-limit-reached')
for pid in children:
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
""",
    )
    assert b"process-limit-reached" in result.stdout


def test_real_container_is_non_root_offline_and_cannot_see_windows_host(tmp_path: Path) -> None:
    result = run_code(
        tmp_path,
        """import os
from pathlib import Path
import socket
print('uid', os.getuid())
print('host-visible', Path('/mnt/c/Users').exists())
try:
    socket.create_connection(('1.1.1.1', 80), timeout=0.5)
    print('network-connected')
except OSError:
    print('network-blocked')
""",
    )
    assert b"uid 0" not in result.stdout
    assert b"host-visible False" in result.stdout
    assert b"network-blocked" in result.stdout
    assert b"network-connected" not in result.stdout


def test_real_container_limits_temporary_storage(tmp_path: Path) -> None:
    result = run_code(
        tmp_path,
        """try:
    with open('/tmp/large', 'wb') as output:
        output.write(b'x' * 20_000_000)
    print('unexpected-write')
except OSError:
    print('temporary-limit-reached')
""",
    )
    assert b"temporary-limit-reached" in result.stdout
    assert b"unexpected-write" not in result.stdout


def test_real_container_captures_python_exception_and_leaves_no_container(tmp_path: Path) -> None:
    result = run_code(tmp_path, "raise RuntimeError('safe-test-error')\n")
    assert result.exit_code != 0
    assert b"RuntimeError: safe-test-error" in result.stderr
    remaining = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=focusmate-run-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert remaining.returncode == 0
    assert remaining.stdout.strip() == ""


@pytest.mark.parametrize(
    ("source", "expected_verdict"),
    [
        ("a,b=map(int,input().split())\nprint(a+b)\n", "AC"),
        ("print(999)\n", "WA"),
        ("while True:\n    pass\n", "TLE"),
        ("raise RuntimeError('boom')\n", "RE"),
        ("if True print('broken')\n", "CE"),
    ],
)
def test_real_judge_maps_all_v1_verdicts(
    tmp_path: Path, source: str, expected_verdict: str,
) -> None:
    repo = prepared_repository(tmp_path, "ready")
    repo.create_submission(submission_request())
    workspace = tmp_path / "judge-workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text(source, encoding="utf-8")
    tests = TrustedTestBundle(
        "asset-real", 1, "problem-submit", 1, "ready",
        (TrustedTestCase("private-1", b"2 3\n", b"5\n"),),
    )
    processor = JudgeSubmissionProcessor(
        DockerPythonRunner(image=IMAGE), InMemoryTrustedAssetProvider([tests]),
        OJResultRepository(repo.path, id_factory=lambda: "oj-result-real"),
    )
    processor.process(
        WorkItem(
            "submission-submit", "goal-submit", "problem-submit", 1, 1,
            "python3", "python-default-v1", source, "unused-by-judge",
        ),
        workspace,
    )
    saved = OJResultRepository(repo.path).get_for_submission("submission-submit")
    assert saved is not None
    assert saved["verdict"] == expected_verdict

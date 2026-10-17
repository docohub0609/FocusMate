from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess

import pytest

from services.oj_worker.docker_runner import (
    DockerPythonRunner,
    LOCAL_POLICY,
    PythonRunRequest,
    ResourcePolicy,
)


class FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", exit_code: int | None = 0):
        self.stdin = BytesIO()
        self.stdout = BytesIO(stdout)
        self.stderr = BytesIO(stderr)
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        return -9 if self.exit_code is None else self.exit_code


class FakeDocker:
    def __init__(self, process: FakeProcess | None = None, *, oom: bool = False):
        self.process = process or FakeProcess()
        self.oom = oom
        self.started: list[list[str]] = []
        self.controlled: list[list[str]] = []

    def start(self, command: list[str]) -> FakeProcess:
        self.started.append(command)
        return self.process

    def control(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.controlled.append(command)
        if command[1] == "kill":
            self.process.exit_code = -9
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, json.dumps({"OOMKilled": self.oom}), "")
        if command[1] == "stats":
            return subprocess.CompletedProcess(command, 0, "12MiB / 256MiB\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class MissingDocker(FakeDocker):
    def start(self, command: list[str]) -> FakeProcess:
        raise FileNotFoundError("docker")

    def control(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")


def request(tmp_path: Path, *, stdin: bytes = b"", policy: ResourcePolicy | None = None):
    (tmp_path / "main.py").write_text("print(input())\n", encoding="utf-8")
    return PythonRunRequest(tmp_path, stdin, policy or ResourcePolicy())


def test_command_contains_all_security_and_resource_boundaries(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = DockerPythonRunner(
        docker=docker, container_name_factory=lambda: "focusmate-run-test",
    )
    result = runner.run(request(tmp_path, stdin=b"hello\n"))
    command = docker.started[0]

    required_pairs = [
        ("--network", "none"), ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges=true"),
        ("--log-driver", "none"), ("--ipc", "none"),
        ("--memory", str(256 * 1024 * 1024)),
        ("--memory-swap", str(256 * 1024 * 1024)),
        ("--cpus", "1.0"), ("--ulimit", "cpu=2:2"),
        ("--pids-limit", "32"), ("--user", "10001:10001"),
    ]
    for flag, value in required_pairs:
        position = command.index(flag)
        assert command[position + 1] == value
    assert "--read-only" in command
    assert "--interactive" in command
    assert "--pull" in command and command[command.index("--pull") + 1] == "never"
    assert command[command.index("--tmpfs") + 1].endswith(f"size={16 * 1024 * 1024}")
    mount = command[command.index("--mount") + 1]
    assert mount.endswith("dst=/workspace,readonly")
    assert command[-5:] == [
        "focusmate-python-runner:v1", "python", "-I", "-B", "/workspace/main.py",
    ]
    assert result.termination_reason == "exited"
    assert result.resource_summary.peak_memory_bytes in (None, 12 * 1024 * 1024)
    assert ["docker", "rm", "-f", "focusmate-run-test"] in docker.controlled


def test_command_is_an_argument_list_not_a_shell_string(tmp_path: Path) -> None:
    workspace = tmp_path / "code & docker run --privileged"
    workspace.mkdir()
    docker = FakeDocker()
    runner = DockerPythonRunner(docker=docker, container_name_factory=lambda: "safe-name")
    runner.run(request(workspace))
    assert isinstance(docker.started[0], list)
    assert "--privileged" not in docker.started[0]
    assert any("code & docker run --privileged" in argument for argument in docker.started[0])


def test_compile_operation_uses_a_fixed_non_executing_command(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = DockerPythonRunner(docker=docker, container_name_factory=lambda: "compile-test")
    runner.run(PythonRunRequest(request(tmp_path).workspace, b"", operation="compile"))
    command = docker.started[0]
    assert command[-3:-1] == ["-B", "-c"]
    assert command[-1].startswith("compile(open('/workspace/main.py'")
    assert "/workspace/main.py" not in command[-1].split("compile(", 1)[0]


def test_compile_operation_rejects_stdin_and_unknown_operations(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = DockerPythonRunner(docker=docker)
    workspace = request(tmp_path).workspace
    with pytest.raises(ValueError, match="cannot receive stdin"):
        runner.run(PythonRunRequest(workspace, b"unexpected", operation="compile"))
    with pytest.raises(ValueError, match="operation"):
        runner.run(PythonRunRequest(workspace, b"", operation="shell"))
    assert docker.started == []


def test_stdout_is_bounded_and_container_is_killed_and_removed(tmp_path: Path) -> None:
    docker = FakeDocker(FakeProcess(stdout=b"x" * 64, exit_code=None))
    policy = ResourcePolicy(output_bytes=16)
    runner = DockerPythonRunner(docker=docker, container_name_factory=lambda: "output-test")
    result = runner.run(request(tmp_path, policy=policy))
    assert result.stdout == b"x" * 16
    assert result.stdout_truncated is True
    assert result.termination_reason == "outputLimitExceeded"
    assert ["docker", "kill", "output-test"] in docker.controlled
    assert ["docker", "rm", "-f", "output-test"] in docker.controlled


def test_wall_timeout_kills_and_removes_container(tmp_path: Path) -> None:
    current = [0.0]

    def sleep(seconds: float) -> None:
        current[0] += seconds

    docker = FakeDocker(FakeProcess(exit_code=None))
    policy = ResourcePolicy(wall_time_seconds=0.02)
    runner = DockerPythonRunner(
        docker=docker, container_name_factory=lambda: "timeout-test",
        monotonic=lambda: current[0], sleep=sleep,
    )
    result = runner.run(request(tmp_path, policy=policy))
    assert result.termination_reason == "wallTimeExceeded"
    assert ["docker", "kill", "timeout-test"] in docker.controlled
    assert ["docker", "rm", "-f", "timeout-test"] in docker.controlled


def test_oom_state_is_execution_fact_not_verdict(tmp_path: Path) -> None:
    docker = FakeDocker(FakeProcess(exit_code=137), oom=True)
    result = DockerPythonRunner(docker=docker).run(request(tmp_path))
    assert result.oom_killed is True
    assert result.termination_reason == "memoryLimitExceeded"
    assert not hasattr(result, "verdict")


def test_missing_docker_returns_safe_infrastructure_result(tmp_path: Path) -> None:
    result = DockerPythonRunner(docker=MissingDocker()).run(request(tmp_path))
    assert result.termination_reason == "infrastructureError"
    assert result.exit_code is None
    assert b"docker" not in result.stderr


def test_docker_cli_exit_125_is_infrastructure_failure(tmp_path: Path) -> None:
    result = DockerPythonRunner(docker=FakeDocker(FakeProcess(exit_code=125))).run(request(tmp_path))
    assert result.termination_reason == "infrastructureError"


def test_invalid_workspace_stdin_and_policy_are_rejected_before_docker(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = DockerPythonRunner(docker=docker)
    with pytest.raises(ValueError, match="main.py"):
        runner.run(PythonRunRequest(tmp_path, b""))

    valid = tmp_path / "valid"
    valid.mkdir()
    with pytest.raises(ValueError, match="stdin"):
        runner.run(request(valid, stdin=b"12", policy=ResourcePolicy(stdin_bytes=1)))

    with pytest.raises(ValueError, match="policy"):
        runner.run(request(valid, policy=ResourcePolicy(policy_ref="unknown")))
    with pytest.raises(ValueError, match="ceiling"):
        runner.run(request(valid, policy=ResourcePolicy(memory_bytes=512 * 1024 * 1024)))
    assert docker.started == []


def test_local_policy_only_extends_wall_time_and_each_policy_keeps_own_ceiling(tmp_path: Path) -> None:
    docker=FakeDocker();runner=DockerPythonRunner(docker=docker)
    local=tmp_path/'local';local.mkdir()
    result=runner.run(request(local,policy=LOCAL_POLICY))
    assert result.resource_summary.cpu_time_limit_seconds==2
    assert result.resource_summary.memory_limit_bytes==256*1024*1024
    default_too_long=tmp_path/'default-long';default_too_long.mkdir()
    with pytest.raises(ValueError,match='ceiling'):
        runner.run(request(default_too_long,policy=ResourcePolicy(wall_time_seconds=4)))
    local_too_long=tmp_path/'local-long';local_too_long.mkdir()
    with pytest.raises(ValueError,match='ceiling'):
        runner.run(request(local_too_long,policy=ResourcePolicy(policy_ref='python-local-v1',wall_time_seconds=11)))


def test_workspace_rejects_extra_files_and_main_symlink(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner = DockerPythonRunner(docker=docker)
    extra = tmp_path / "extra"
    extra.mkdir()
    request(extra)
    (extra / "secret.txt").write_text("host data", encoding="utf-8")
    with pytest.raises(ValueError, match="only"):
        runner.run(PythonRunRequest(extra, b""))

    target = tmp_path / "target.py"
    target.write_text("print(1)", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    try:
        (linked / "main.py").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows configuration")
    with pytest.raises(ValueError, match="only"):
        runner.run(PythonRunRequest(linked, b""))
    assert docker.started == []


def test_builder_startup_marker_is_removed_and_execution_output_is_preserved(tmp_path):
    docker = FakeDocker(FakeProcess(stdout=b"42\n", stderr=b"FM_STARTED:builder-test\n"))
    runner = DockerPythonRunner(docker=docker, container_name_factory=lambda: "builder-test",
                                startup_timeout_seconds=10)
    result = runner.run(request(tmp_path))
    assert result.termination_reason == "exited"
    assert result.stdout == b"42\n" and result.stderr == b""
    assert runner.last_timing["startupMs"] is not None


def test_builder_missing_startup_marker_cannot_be_accepted(tmp_path):
    docker = FakeDocker(FakeProcess(stdout=b"42\n"))
    runner = DockerPythonRunner(docker=docker, startup_timeout_seconds=10)
    assert runner.run(request(tmp_path)).termination_reason == "infrastructureError"


def test_builder_startup_timeout_is_not_code_timeout(tmp_path):
    clock = [0.0]
    docker = FakeDocker(FakeProcess(exit_code=None))
    runner = DockerPythonRunner(docker=docker, startup_timeout_seconds=0.1,
        monotonic=lambda: clock[0], sleep=lambda _: clock.__setitem__(0, clock[0]+0.05))
    result = runner.run(request(tmp_path))
    assert result.termination_reason == "startupTimeExceeded"
    assert any(command[1] == "rm" for command in docker.controlled)


def test_builder_runtime_limit_still_kills_after_readiness(tmp_path):
    clock = [0.0]
    docker = FakeDocker(FakeProcess(stderr=b"FM_STARTED:runtime-test\n", exit_code=None))
    runner = DockerPythonRunner(docker=docker, container_name_factory=lambda: "runtime-test",
        startup_timeout_seconds=10, monotonic=lambda: clock[0],
        sleep=lambda _: clock.__setitem__(0, clock[0]+0.05))
    result = runner.run(request(tmp_path, policy=ResourcePolicy(wall_time_seconds=0.1)))
    assert result.termination_reason == "wallTimeExceeded"
    assert any(command[1] == "kill" for command in docker.controlled)


def test_builder_total_deadline_bounds_startup(tmp_path):
    from dataclasses import replace
    clock = [0.0]
    docker = FakeDocker(FakeProcess(exit_code=None))
    runner = DockerPythonRunner(docker=docker, startup_timeout_seconds=10,
        monotonic=lambda: clock[0], sleep=lambda _: clock.__setitem__(0, clock[0]+0.05))
    result = runner.run(replace(request(tmp_path), deadline=0.1))
    assert result.termination_reason == "startupTimeExceeded"
    assert clock[0] < 1


def test_builder_recognizes_marker_after_docker_warning(tmp_path):
    docker = FakeDocker(FakeProcess(stderr=b"WARNING: image platform differs\nFM_STARTED:warning-test\n"))
    runner = DockerPythonRunner(docker=docker, container_name_factory=lambda: "warning-test",
                                startup_timeout_seconds=10)
    result = runner.run(request(tmp_path))
    assert result.termination_reason == "exited"
    assert result.stderr == b"WARNING: image platform differs\n"

"""Run one Python program with one stdin inside a restricted Docker container."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
import threading
import time
from typing import BinaryIO, Callable, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class ResourcePolicy:
    policy_ref: str = "python-default-v1"
    wall_time_seconds: float = 3.0
    cpu_count: float = 1.0
    cpu_time_seconds: int = 2
    memory_bytes: int = 256 * 1024 * 1024
    process_limit: int = 32
    output_bytes: int = 1024 * 1024
    stdin_bytes: int = 1024 * 1024
    temporary_bytes: int = 16 * 1024 * 1024


DEFAULT_POLICY = ResourcePolicy()
LOCAL_POLICY = ResourcePolicy(policy_ref="python-local-v1", wall_time_seconds=10.0)
POLICY_CEILINGS = {DEFAULT_POLICY.policy_ref: DEFAULT_POLICY, LOCAL_POLICY.policy_ref: LOCAL_POLICY}


@dataclass(frozen=True)
class PythonRunRequest:
    workspace: Path
    stdin: bytes
    policy: ResourcePolicy = DEFAULT_POLICY
    operation: str = "execute"
    deadline: float | None = None


@dataclass(frozen=True)
class ResourceSummary:
    wall_time_ms: int
    memory_limit_bytes: int
    peak_memory_bytes: int | None
    cpu_count_limit: float
    cpu_time_limit_seconds: int
    process_limit: int
    temporary_bytes: int
    execution_time_ms: int | None = None


@dataclass(frozen=True)
class PythonRunResult:
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    termination_reason: str
    stdout_truncated: bool
    stderr_truncated: bool
    oom_killed: bool
    resource_summary: ResourceSummary
    failure: dict | None = None


class RunningProcess(Protocol):
    stdin: BinaryIO | None
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class DockerCli(Protocol):
    def start(self, command: list[str]) -> RunningProcess: ...
    def control(self, command: list[str]) -> subprocess.CompletedProcess[str]: ...


class SubprocessDockerCli:
    def start(self, command: list[str]) -> RunningProcess:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )

    def control(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )


class DockerPythonRunner:
    """Execution primitive only; it does not compare output or create a Verdict."""

    def __init__(
        self,
        *,
        image: str = "focusmate-python-runner:v1",
        docker: DockerCli | None = None,
        container_name_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout_seconds: float | None = None,
        engine=None,
    ) -> None:
        if startup_timeout_seconds is not None and (not math.isfinite(startup_timeout_seconds) or not 0 < startup_timeout_seconds <= 30):
            raise ValueError("Invalid startup timeout")
        self.startup_timeout_seconds = startup_timeout_seconds
        self.last_timing = {}
        self.image = image
        self.docker = docker or SubprocessDockerCli()
        self._use_engine = docker is None
        self._engine = engine
        self.container_name_factory = container_name_factory or (
            lambda: f"focusmate-run-{uuid4().hex}"
        )
        self.monotonic = monotonic
        self.sleep = sleep

    def build_command(self, request: PythonRunRequest, container_name: str) -> list[str]:
        policy = request.policy
        workspace = request.workspace.resolve()
        command = [
            "docker", "run", "--interactive", "--name", container_name, "--pull", "never",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--log-driver", "none", "--ipc", "none",
            "--security-opt", "no-new-privileges=true",
            "--memory", str(policy.memory_bytes),
            "--memory-swap", str(policy.memory_bytes),
            "--cpus", str(policy.cpu_count),
            "--ulimit", f"cpu={policy.cpu_time_seconds}:{policy.cpu_time_seconds}",
            "--pids-limit", str(policy.process_limit),
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={policy.temporary_bytes}",
            "--mount", f"type=bind,src={workspace},dst=/workspace,readonly",
            "--workdir", "/workspace", "--user", "10001:10001",
        ]
        if self.startup_timeout_seconds is not None:
            target = (["-c", "compile(open('/workspace/main.py', encoding='utf-8').read(), '/workspace/main.py', 'exec')"]
                      if request.operation == "compile" else ["/workspace/main.py"])
            # The fixed launcher reports readiness before replacing itself with Python.
            # Candidate code never controls the host timer or resets this marker.
            launcher = ("import os,sys; os.write(2," + repr(f"FM_STARTED:{container_name}\n".encode())
                        + "); os.execv(sys.executable," + repr(["python", "-I", "-B", *target]) + ")")
            return command + [self.image, "python", "-I", "-B", "-c", launcher]
        if request.operation == "compile":
            return command + [
                self.image, "python", "-I", "-B", "-c",
                "compile(open('/workspace/main.py', encoding='utf-8').read(), "
                "'/workspace/main.py', 'exec')",
            ]
        return command + [self.image, "python", "-I", "-B", "/workspace/main.py"]

    def run(self, request: PythonRunRequest) -> PythonRunResult:
        self._validate_request(request)
        if self._use_engine:
            if self._engine is None:
                from .engine_runner import EngineRunner
                self._engine = EngineRunner()
            return self._engine.run(self, request)
        container_name = self.container_name_factory()
        started_at = self.monotonic()
        ready_at: list[float] = []
        self.last_timing = {}
        process: RunningProcess | None = None
        stdout = _BoundedOutput(request.policy.output_bytes)
        stderr = _BoundedOutput(request.policy.output_bytes)
        memory = _MemorySample()
        sampling_stop = threading.Event()
        sampling_thread: threading.Thread | None = None
        termination_reason = "exited"
        exit_code: int | None = None
        oom_killed = False
        try:
            process = self.docker.start(self.build_command(request, container_name))
            sampling_thread = threading.Thread(
                target=self._sample_memory,
                args=(container_name, memory, sampling_stop),
                daemon=True,
            )
            sampling_thread.start()
            threads = [
                threading.Thread(target=_read_stream, args=(process.stdout, stdout), daemon=True),
                threading.Thread(target=_read_started_stream if self.startup_timeout_seconds is not None else _read_stream,
                    args=((process.stderr, stderr, f"FM_STARTED:{container_name}\n".encode(),
                           lambda: ready_at.append(self.monotonic())) if self.startup_timeout_seconds is not None
                          else (process.stderr, stderr)), daemon=True),
                threading.Thread(target=_write_stdin, args=(process.stdin, request.stdin), daemon=True),
            ]
            for thread in threads:
                thread.start()

            deadline = started_at + request.policy.wall_time_seconds
            while process.poll() is None:
                if stdout.truncated or stderr.truncated:
                    termination_reason = "outputLimitExceeded"
                    self._kill(container_name)
                    break
                if self.startup_timeout_seconds is not None:
                    deadline = ((ready_at[0] + request.policy.wall_time_seconds) if ready_at
                                else started_at + self.startup_timeout_seconds)
                if request.deadline is not None:
                    deadline = min(deadline, request.deadline)
                if self.monotonic() >= deadline:
                    termination_reason = ("startupTimeExceeded" if self.startup_timeout_seconds is not None and not ready_at
                                          else "wallTimeExceeded")
                    self._kill(container_name)
                    break
                self.sleep(0.01)
            exit_code = process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=1)

            if (stdout.truncated or stderr.truncated) and termination_reason == "exited":
                termination_reason = "outputLimitExceeded"

            if self.startup_timeout_seconds is not None and not ready_at and termination_reason == "exited":
                termination_reason = "infrastructureError"
            state = self._inspect_state(container_name)
            oom_killed = bool(state.get("OOMKilled", False))
            if oom_killed:
                termination_reason = "memoryLimitExceeded"
            elif exit_code == 125:
                termination_reason = "infrastructureError"
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            if process is None or termination_reason == "exited":
                termination_reason = "infrastructureError"
        finally:
            sampling_stop.set()
            if sampling_thread is not None:
                sampling_thread.join(timeout=1)
            self._remove(container_name)

        self.last_timing = {"startupMs": round((ready_at[0]-started_at)*1000) if ready_at else None}
        elapsed_ms = max(0, round((self.monotonic() - started_at) * 1000))
        return PythonRunResult(
            stdout=stdout.value, stderr=stderr.value, exit_code=exit_code,
            termination_reason=termination_reason,
            stdout_truncated=stdout.truncated, stderr_truncated=stderr.truncated,
            oom_killed=oom_killed,
            resource_summary=ResourceSummary(
                wall_time_ms=elapsed_ms,
                memory_limit_bytes=request.policy.memory_bytes,
                peak_memory_bytes=memory.peak_bytes,
                cpu_count_limit=request.policy.cpu_count,
                cpu_time_limit_seconds=request.policy.cpu_time_seconds,
                process_limit=request.policy.process_limit,
                temporary_bytes=request.policy.temporary_bytes,
            ),
        )

    @staticmethod
    def _validate_request(request: PythonRunRequest) -> None:
        if request.deadline is not None and not math.isfinite(request.deadline):
            raise ValueError("Invalid execution deadline")
        if request.operation not in {"compile", "execute"}:
            raise ValueError("Unsupported Python runner operation.")
        if request.operation == "compile" and request.stdin:
            raise ValueError("Compile-only runs cannot receive stdin.")
        ceiling = POLICY_CEILINGS.get(request.policy.policy_ref)
        if ceiling is None:
            raise ValueError("Unsupported resource policy.")
        numeric_limits = (
            (request.policy.wall_time_seconds, ceiling.wall_time_seconds),
            (request.policy.cpu_count, ceiling.cpu_count),
            (request.policy.cpu_time_seconds, ceiling.cpu_time_seconds),
            (request.policy.memory_bytes, ceiling.memory_bytes),
            (request.policy.process_limit, ceiling.process_limit),
            (request.policy.output_bytes, ceiling.output_bytes),
            (request.policy.stdin_bytes, ceiling.stdin_bytes),
            (request.policy.temporary_bytes, ceiling.temporary_bytes),
        )
        if any(value <= 0 or value > maximum for value, maximum in numeric_limits):
            raise ValueError("Resource policy exceeds the python-default-v1 safety ceiling.")
        if len(request.stdin) > request.policy.stdin_bytes:
            raise ValueError("stdin exceeds the configured limit.")
        main = request.workspace / "main.py"
        if not request.workspace.is_dir() or not main.is_file():
            raise ValueError("The workspace must contain main.py.")
        if main.is_symlink() or {item.name for item in request.workspace.iterdir()} != {"main.py"}:
            raise ValueError("The workspace may contain only a regular main.py file.")

    def _kill(self, container_name: str) -> None:
        try:
            self.docker.control(["docker", "kill", container_name])
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass

    def _remove(self, container_name: str) -> None:
        try:
            self.docker.control(["docker", "rm", "-f", container_name])
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass

    def _inspect_state(self, container_name: str) -> dict[str, object]:
        try:
            result = self.docker.control([
                "docker", "inspect", "--format", "{{json .State}}", container_name,
            ])
            if result.returncode == 0:
                value = json.loads(result.stdout)
                return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return {}

    def _sample_memory(
        self, container_name: str, sample: "_MemorySample", stop: threading.Event,
    ) -> None:
        while not stop.is_set():
            try:
                result = self.docker.control([
                    "docker", "stats", "--no-stream", "--format", "{{.MemUsage}}",
                    container_name,
                ])
                if result.returncode == 0:
                    value = _parse_memory_bytes(result.stdout.partition("/")[0].strip())
                    if value is not None:
                        sample.observe(value)
            except (FileNotFoundError, OSError, subprocess.SubprocessError):
                return
            stop.wait(0.05)


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self.lock:
            remaining = self.limit - len(self.buffer)
            self.buffer.extend(chunk[:max(0, remaining)])
            if len(chunk) > remaining:
                self.truncated = True

    @property
    def value(self) -> bytes:
        with self.lock:
            return bytes(self.buffer)


class _MemorySample:
    def __init__(self) -> None:
        self.peak_bytes: int | None = None
        self.lock = threading.Lock()

    def observe(self, value: int) -> None:
        with self.lock:
            self.peak_bytes = max(self.peak_bytes or 0, value)


def _parse_memory_bytes(value: str) -> int | None:
    units = {
        "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3,
        "kib": 1024, "mib": 1024**2, "gib": 1024**3,
    }
    normalized = value.strip().lower()
    for unit in sorted(units, key=len, reverse=True):
        if normalized.endswith(unit):
            try:
                return round(float(normalized[:-len(unit)].strip()) * units[unit])
            except ValueError:
                return None
    return None


def _read_stream(stream: BinaryIO | None, output: _BoundedOutput) -> None:
    if stream is None:
        return
    while chunk := stream.read(64 * 1024):
        output.append(chunk)
        if output.truncated:
            return


def _write_stdin(stream: BinaryIO | None, value: bytes) -> None:
    if stream is None:
        return
    try:
        stream.write(value)
        stream.close()
    except (BrokenPipeError, OSError):
        pass


def _read_started_stream(stream, output, marker, started):
    if stream is None:
        return
    # Docker may print warnings before the container's stderr. Preserve them,
    # but do not mistake them for failure to start. All prefix data stays bounded.
    while not output.truncated:
        line = stream.readline(64 * 1024)
        if not line:
            return
        if line == marker:
            started()
            _read_stream(stream, output)
            return
        output.append(line)

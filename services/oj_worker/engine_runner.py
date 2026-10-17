"""Docker SDK transport: isolated, serial containers without per-call CLI processes."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading

import docker
from docker.errors import APIError, DockerException, NotFound
from requests.exceptions import RequestException
from requests.exceptions import ConnectionError, Timeout

from .docker_runner import PythonRunResult, ResourceSummary, _BoundedOutput


ENGINE_ERRORS = (DockerException, RequestException, OSError, ValueError)
_clients = threading.local()


class _StartupDeadline(Exception):
    pass


class _ExecutionDeadline(Exception):
    pass


@lru_cache(maxsize=1)
def _connection_options():
    options = docker.utils.kwargs_from_env()
    if 'base_url' not in options:
        # The SDK does not select Docker CLI contexts. Resolve once, not per case.
        try:
            host = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                                  capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            if host.startswith(('npipe://', 'unix://')):
                options['base_url'] = host
        except (OSError, subprocess.SubprocessError):
            pass  # SDK's platform default still supports installations without the CLI.
    return options


def engine_client(*, sampling=False, timeout=None):
    key = 'sampling' if sampling else 'control'
    api = getattr(_clients, key, None)
    if api is None:
        api = docker.APIClient(**_connection_options(), version='auto', timeout=.15 if sampling else min(3,timeout) if timeout is not None else 3)
        api.timeout = .15 if sampling else 3
        setattr(_clients, key, api)
    return api


def remove_container(name):
    """Idempotent confirmed cleanup; False leaves the durable cleanup obligation intact."""
    try:
        engine_client().remove_container(name, force=True, v=True)
        return True
    except NotFound:
        return True
    except ENGINE_ERRORS:
        return False


def execution_milliseconds(state):
    try:
        start, end = (state[key] for key in ('StartedAt', 'FinishedAt'))
        if start.startswith('0001-') or end.startswith('0001-'):
            return None
        elapsed = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
                   datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
        return max(0, round(elapsed * 1000)) if elapsed >= 0 else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def memory_sample(stats):
    memory = stats.get('memory_stats') or {}
    values = [memory.get('usage'), memory.get('max_usage')]
    valid = [v for v in values if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    return max(valid) if valid else None


class EngineRunner:
    def __init__(self, *, api=None):
        self.api = api

    def run(self, owner, request):
        clock, sleep = owner.monotonic, owner.sleep
        started = clock()
        name = owner.container_name_factory()
        policy = request.policy
        stdout, stderr = _BoundedOutput(policy.output_bytes), _BoundedOutput(policy.output_bytes)
        container, stream, reader = None, None, None
        state, ready, read_errors = {}, [], []
        peak, exit_code, reason, oom = None, None, 'exited', False
        phase, failure_phase, cleanup_failed, create_attempted = 'connect', None, False, False
        error_type, retry_count = None, 0
        cleanup_error = None
        marker = f'FM_STARTED:{name}\n'.encode()
        prefix = bytearray()
        marker_checked = False

        def read_output():
            nonlocal marker_checked
            try:
                for out, err in stream:
                    if out: stdout.append(out)
                    if err:
                        if not marker_checked:
                            needed = len(marker) - len(prefix)
                            prefix.extend(err[:needed])
                            err = err[needed:]
                            if len(prefix) == len(marker):
                                marker_checked = True
                                if bytes(prefix) == marker:
                                    ready.append(clock())
                                else:
                                    stderr.append(bytes(prefix))
                        if marker_checked and err: stderr.append(err)
                    # Keep draining after the bound is reached until the owner kills
                    # the container. Abandoning attach can block Docker's shutdown.
            except ENGINE_ERRORS as exc:
                read_errors.append(type(exc).__name__)

        api = None
        def read_only(call, *, connect=False):
            nonlocal retry_count
            limit = ready[0] + policy.wall_time_seconds if ready else started + (owner.startup_timeout_seconds or 10)
            if request.deadline is not None: limit = min(limit, request.deadline)
            for attempt in range(2):
                remaining = limit - clock()
                if remaining <= 0:
                    raise _ExecutionDeadline() if ready else _StartupDeadline()
                old_timeout = getattr(api, 'timeout', None)
                try:
                    if old_timeout is not None: api.timeout = min(old_timeout, remaining)
                    value = call(timeout=remaining) if connect else call()
                    if clock() >= limit:
                        if not connect and value.get('State', {}).get('Running') and ready:
                            raise _ExecutionDeadline()
                        raise Timeout('Docker query deadline exceeded')
                    return value
                except ENGINE_ERRORS as exc:
                    # Only transport faults (including SDK-wrapped negotiation
                    # faults) qualify. Never repeat a create/start/attach call.
                    transient = isinstance(exc, (ConnectionError, Timeout))
                    cause = exc.__cause__ or exc.__context__
                    transient = transient or isinstance(cause, (ConnectionError, Timeout))
                    if not transient or attempt or clock() + .05 >= limit: raise
                    retry_count += 1
                    sleep(.05)
                finally:
                    if old_timeout is not None: api.timeout = old_timeout
        def check_startup_deadline():
            limit = started + (owner.startup_timeout_seconds or 10)
            if request.deadline is not None: limit = min(limit, request.deadline)
            if clock() >= limit: raise _StartupDeadline()

        def startup_call(call):
            check_startup_deadline()
            limit = started + (owner.startup_timeout_seconds or 10)
            if request.deadline is not None: limit = min(limit, request.deadline)
            old_timeout = getattr(api, 'timeout', None)
            try:
                if old_timeout is not None: api.timeout = max(.001, limit-clock())
                return call()  # Deliberately once: a lost mutation reply is ambiguous.
            finally:
                if old_timeout is not None: api.timeout = old_timeout

        try:
            if request.deadline is not None and clock() >= request.deadline:
                reason = 'startupTimeExceeded'
            else:
                api = self.api or read_only(engine_client, connect=True)
                # Only this input is mounted, never other tests or expected answers.
                with TemporaryDirectory(prefix='focusmate-stdin-', dir=request.workspace.parent) as temp:
                    input_path = Path(temp)/'stdin'
                    input_path.write_bytes(request.stdin)
                    target = (['-c', "compile(open('/workspace/main.py', encoding='utf-8').read(), '/workspace/main.py', 'exec')"]
                              if request.operation == 'compile' else ['/workspace/main.py'])
                    launcher = ("import os,sys; f=os.open('/run/focusmate-stdin',os.O_RDONLY); "
                                "os.dup2(f,0); os.close(f); os.write(2," + repr(marker) + "); "
                                "os.execv(sys.executable," + repr(['python', '-I', '-B', *target]) + ")")
                    host = api.create_host_config(
                        network_mode='none', read_only=True, cap_drop=['ALL'],
                        security_opt=['no-new-privileges=true'], ipc_mode='none',
                        log_config=docker.types.LogConfig(type='none'),
                        mem_limit=policy.memory_bytes, memswap_limit=policy.memory_bytes,
                        nano_cpus=int(policy.cpu_count*1_000_000_000), pids_limit=policy.process_limit,
                        ulimits=[docker.types.Ulimit(name='cpu', soft=policy.cpu_time_seconds, hard=policy.cpu_time_seconds)],
                        tmpfs={'/tmp': f'rw,noexec,nosuid,nodev,size={policy.temporary_bytes}'},
                        mounts=[docker.types.Mount('/workspace', str(request.workspace.resolve()), type='bind', read_only=True),
                                docker.types.Mount('/run/focusmate-stdin', str(input_path.resolve()), type='bind', read_only=True)],
                    )
                    try:
                        check_startup_deadline()
                        phase, create_attempted = 'create', True
                        try:
                            container = startup_call(lambda: api.create_container(image=owner.image, command=['python', '-I', '-B', '-c', launcher],
                                name=name, user='10001:10001', working_dir='/workspace', network_disabled=True,
                                host_config=host, stdin_open=False, tty=False, use_config_proxy=False))['Id']
                        except APIError as exc:
                            if exc.status_code == 409: create_attempted = False
                            raise
                        check_startup_deadline()
                        phase = 'attach'
                        stream = startup_call(lambda: api.attach(container, stdout=True, stderr=True, stream=True, demux=True))
                        reader = threading.Thread(target=read_output, daemon=True, name='focusmate-output')
                        reader.start()
                        check_startup_deadline()
                        phase = 'start'
                        startup_call(lambda: api.start(container))
                        launched = clock()
                        next_sample = launched
                        while True:
                            if stdout.truncated or stderr.truncated:
                                reason = 'outputLimitExceeded'
                            limit = (ready[0] + policy.wall_time_seconds if ready else
                                     (started + owner.startup_timeout_seconds if owner.startup_timeout_seconds is not None
                                      else launched + policy.wall_time_seconds))
                            if request.deadline is not None: limit = min(limit, request.deadline)
                            if clock() >= limit:
                                reason = 'startupTimeExceeded' if not ready and owner.startup_timeout_seconds is not None else 'wallTimeExceeded'
                            if read_errors:
                                reason, failure_phase = 'infrastructureError', 'attach'
                            if reason != 'exited':
                                phase = 'kill'
                                try:
                                    api.kill(container)
                                except APIError as exc:
                                    # Natural exit may race a host-side output/timeout check.
                                    if exc.status_code != 409: raise
                                    state = api.inspect_container(container)['State']
                                    if state.get('Running', True): raise
                                break
                            phase = 'inspect'
                            try:
                                state = read_only(lambda: api.inspect_container(container))['State']
                            except _ExecutionDeadline:
                                reason = 'wallTimeExceeded'
                                continue
                            if not state['Running']: break
                            if clock() >= next_sample:
                                try:
                                    stats_api = self.api or engine_client(sampling=True)
                                    sample = memory_sample(stats_api.stats(container, stream=False, one_shot=True))
                                    if sample is not None: peak = max(peak or 0, sample)
                                except ENGINE_ERRORS:
                                    pass  # Optional measurement; hard limits remain enforced by Docker.
                                next_sample = clock() + .05
                            sleep(.01)
                        if state.get('Running', True):
                            state = api.inspect_container(container)['State']
                        exit_code = state.get('ExitCode')
                        oom = bool(state.get('OOMKilled'))
                        if oom: reason = 'memoryLimitExceeded'
                        reader.join(timeout=1)
                        if stdout.truncated or stderr.truncated:
                            if reason == 'exited': reason = 'outputLimitExceeded'
                        if reason == 'exited' and (state.get('Running') or read_errors or
                                (owner.startup_timeout_seconds is not None and not ready)):
                            reason = 'infrastructureError'
                    finally:
                        # Confirm deletion before releasing the temporary stdin mount.
                        if create_attempted:
                            try:
                                api.remove_container(container or name, force=True, v=True)
                            except NotFound:
                                pass
                            except ENGINE_ERRORS as exc:
                                cleanup_error = type(exc).__name__
                                cleanup_failed = True
                        if stream is not None and hasattr(stream, 'close'):
                            try: stream.close()
                            except (OSError, ValueError): pass
                        if reader is not None:
                            reader.join(timeout=.25)
                            if reader.is_alive(): reason = 'infrastructureError'
        except _StartupDeadline:
            failure_phase, error_type = phase, 'DeadlineExceeded'
            reason = 'startupTimeExceeded'
        except ENGINE_ERRORS as exc:
            failure_phase = phase
            error_type = type(exc).__name__
            reason = 'infrastructureError'
        if cleanup_failed:
            failure_phase, reason = 'remove', 'infrastructureError'
            error_type = cleanup_error
        owner.last_timing = {'startupMs': round((ready[0]-started)*1000) if ready else None,
                             'executionMs': execution_milliseconds(state), 'transport': 'dockerSdk',
                             'failurePhase': failure_phase, 'retryCount': retry_count}
        failure = None
        if reason in ('infrastructureError', 'startupTimeExceeded'):
            failure = {'stage': failure_phase or phase, 'errorType': error_type or (read_errors[0] if read_errors else reason),
                       'elapsedMs': max(0, round((clock()-started)*1000)), 'retryCount': retry_count}
        return PythonRunResult(stdout.value, stderr.value, exit_code, reason,
            stdout.truncated, stderr.truncated, oom,
            ResourceSummary(max(0, round((clock()-started)*1000)), policy.memory_bytes, peak,
                policy.cpu_count, policy.cpu_time_seconds, policy.process_limit, policy.temporary_bytes,
                execution_milliseconds(state)), failure)

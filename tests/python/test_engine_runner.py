from pathlib import Path

from services.oj_worker.docker_runner import DockerPythonRunner, PythonRunRequest, ResourcePolicy


class FakeAPI:
    def __init__(self, *, output=b'42\n', oom=False, running=False, fail_remove=False):
        self.output, self.oom, self.running, self.fail_remove = output, oom, running, fail_remove
        self.created = None
        self.removed = []
        self.killed = []
        self.stats_options = []

    def create_host_config(self, **kwargs): return kwargs
    def create_container(self, **kwargs):
        self.created = kwargs
        return {'Id': 'test-container'}
    def attach(self, *args, **kwargs): return iter([(self.output, b'')])
    def start(self, container): pass
    def inspect_container(self, container):
        return {'State': {'Running': self.running, 'ExitCode': 137 if self.oom else 0,
                'OOMKilled': self.oom, 'StartedAt': '2026-09-27T01:00:00.000000000Z',
                'FinishedAt': '2026-09-27T01:00:00.025000000Z'}}
    def stats(self, container, **kwargs):
        self.stats_options.append(kwargs)
        return {'memory_stats': {'usage': 0}}
    def kill(self, container):
        self.killed.append(container)
        self.running = False
    def remove_container(self, container, **kwargs):
        self.removed.append(container)
        if self.fail_remove: raise OSError('cannot remove')


def request(tmp_path, **kwargs):
    (tmp_path/'main.py').write_text('print(42)\n', encoding='utf-8')
    return PythonRunRequest(tmp_path, b'private input\n', **kwargs)


def test_engine_preserves_isolation_and_separates_execution_time(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    api = FakeAPI()
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path))
    assert result.stdout == b'42\n' and result.termination_reason == 'exited'
    assert result.resource_summary.execution_time_ms == 25
    assert result.resource_summary.peak_memory_bytes is None
    host = api.created['host_config']
    assert host['network_mode'] == 'none' and host['read_only'] is True
    assert host['cap_drop'] == ['ALL'] and host['security_opt'] == ['no-new-privileges=true']
    assert host['mem_limit'] == host['memswap_limit'] == 256*1024*1024
    assert host['pids_limit'] == 32 and api.created['user'] == '10001:10001'
    assert len(host['mounts']) == 2 and all(m['ReadOnly'] for m in host['mounts'])
    assert not Path(host['mounts'][1]['Source']).exists()
    assert api.removed == ['test-container']


def test_engine_cleanup_failure_is_system_failure(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    result = DockerPythonRunner(engine=EngineRunner(api=FakeAPI(fail_remove=True))).run(request(tmp_path))
    assert result.termination_reason == 'infrastructureError'


def test_engine_oom_is_preserved(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    result = DockerPythonRunner(engine=EngineRunner(api=FakeAPI(oom=True))).run(request(tmp_path))
    assert result.oom_killed and result.termination_reason == 'memoryLimitExceeded'


def test_engine_timeout_kills_and_cleans_up(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    api = FakeAPI(running=True)
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path, policy=ResourcePolicy(wall_time_seconds=.02)))
    assert result.termination_reason == 'wallTimeExceeded'
    assert api.killed and api.removed


def test_engine_output_limit_remains_bounded(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    api = FakeAPI(output=b'x'*10000)
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path, policy=ResourcePolicy(output_bytes=32)))
    assert result.stdout == b'x'*32 and result.stdout_truncated
    assert result.termination_reason == 'outputLimitExceeded'


def test_engine_expired_deadline_does_not_start_container(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    api = FakeAPI()
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path, deadline=0))
    assert result.termination_reason == 'startupTimeExceeded'
    assert api.created is None


def test_only_positive_memory_samples_are_reported():
    from services.oj_worker.engine_runner import memory_sample, execution_milliseconds
    assert memory_sample({'memory_stats': {'usage': 0, 'max_usage': 0}}) is None
    assert memory_sample({'memory_stats': {'usage': 1234, 'max_usage': 5678}}) == 5678
    assert execution_milliseconds({'StartedAt': '0001-01-01T00:00:00Z', 'FinishedAt': '0001-01-01T00:00:00Z'}) is None


def test_judge_execution_summary_excludes_compile_and_never_guesses_missing_time():
    from services.oj_worker.judge import _Resources
    from services.oj_worker.docker_runner import ResourceSummary
    resources = _Resources()
    resources.add(ResourceSummary(400, 100, 90, 1, 2, 32, 100, 20), execution=False)
    resources.add(ResourceSummary(500, 100, 40, 1, 2, 32, 100, 30))
    result = resources.outcome('AC', 1, None, None)
    assert result.total_wall_time_ms == 900
    assert result.total_execution_time_ms == 30 and result.peak_memory_bytes == 40
    resources.add(ResourceSummary(500, 100, None, 1, 2, 32, 100))
    assert resources.outcome('AC', 2, None, None).total_execution_time_ms is None


def test_lost_create_response_is_cleaned_by_owned_name(tmp_path):
    from requests.exceptions import ReadTimeout
    from services.oj_worker.engine_runner import EngineRunner
    class LostCreate(FakeAPI):
        def create_container(self, **kwargs):
            self.created = kwargs
            raise ReadTimeout()
    api = LostCreate()
    runner = DockerPythonRunner(engine=EngineRunner(api=api), container_name_factory=lambda: 'owned-create')
    result = runner.run(request(tmp_path))
    assert result.termination_reason == 'infrastructureError'
    assert api.removed == ['owned-create']


def test_name_conflict_does_not_delete_another_container(tmp_path):
    from docker.errors import APIError
    from requests import Response
    from services.oj_worker.engine_runner import EngineRunner
    class Conflict(FakeAPI):
        def create_container(self, **kwargs):
            response = Response(); response.status_code = 409
            raise APIError('Conflict', response=response)
    api = Conflict()
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path))
    assert result.termination_reason == 'infrastructureError' and not api.removed


def test_deadline_during_create_never_starts_program(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    clock = [0]
    class SlowCreate(FakeAPI):
        def create_container(self, **kwargs):
            clock[0] = 2
            return super().create_container(**kwargs)
        def start(self, container): raise AssertionError('must not start after deadline')
    api = SlowCreate()
    result = DockerPythonRunner(engine=EngineRunner(api=api), monotonic=lambda:clock[0]).run(request(tmp_path, deadline=1))
    assert result.termination_reason == 'startupTimeExceeded' and api.removed


def test_output_limit_survives_already_exited_kill_race(tmp_path):
    from docker.errors import APIError
    from requests import Response
    from services.oj_worker.engine_runner import EngineRunner
    class AlreadyExited(FakeAPI):
        def kill(self, container):
            response = Response(); response.status_code = 409
            raise APIError('container is not running', response=response)
    api = AlreadyExited(output=b'x'*10000)
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path, policy=ResourcePolicy(output_bytes=32)))
    assert result.termination_reason == 'outputLimitExceeded'


def test_attach_failure_is_not_misreported_as_user_timeout(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    class Disconnected(FakeAPI):
        def attach(self, *args, **kwargs):
            yield None, b'FM_STARTED:test\n'
            raise OSError('broken attach')
    api = Disconnected(running=True)
    runner = DockerPythonRunner(engine=EngineRunner(api=api), container_name_factory=lambda:'test', startup_timeout_seconds=10)
    result = runner.run(request(tmp_path, policy=ResourcePolicy(wall_time_seconds=.02)))
    assert result.termination_reason == 'infrastructureError'
    assert api.killed and api.removed


def test_transient_inspect_retries_without_restarting_code(tmp_path):
    from requests.exceptions import ConnectionError
    from services.oj_worker.engine_runner import EngineRunner
    class Once(FakeAPI):
        checks = 0
        starts = 0
        def start(self, container): self.starts += 1
        def inspect_container(self, container):
            self.checks += 1
            if self.checks == 1: raise ConnectionError('secret endpoint')
            return super().inspect_container(container)
    api = Once()
    runner = DockerPythonRunner(engine=EngineRunner(api=api))
    result = runner.run(request(tmp_path))
    assert result.termination_reason == 'exited' and api.starts == 1
    assert runner.last_timing['retryCount'] == 1


def test_persistent_inspect_failure_has_safe_diagnostic(tmp_path):
    from requests.exceptions import ConnectionError
    from services.oj_worker.engine_runner import EngineRunner
    class Broken(FakeAPI):
        checks = 0
        def inspect_container(self, container):
            self.checks += 1
            raise ConnectionError('secret endpoint and hidden answer')
    api = Broken()
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path))
    assert api.checks == 2 and api.removed
    assert result.failure['stage'] == 'inspect'
    assert result.failure['errorType'] == 'ConnectionError'
    assert 'secret' not in str(result.failure)


def test_connection_negotiation_recovers_once(tmp_path, monkeypatch):
    import services.oj_worker.engine_runner as module
    from requests.exceptions import Timeout
    attempts = []
    def connect(**kwargs):
        attempts.append(1)
        if len(attempts) == 1: raise Timeout('secret')
        return FakeAPI()
    monkeypatch.setattr(module, 'engine_client', connect)
    runner = DockerPythonRunner()
    assert runner.run(request(tmp_path)).termination_reason == 'exited'
    assert len(attempts) == 2 and runner.last_timing['retryCount'] == 1


def test_lost_start_response_is_never_replayed(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    from requests.exceptions import Timeout
    class LostStart(FakeAPI):
        starts = 0
        def start(self, container):
            self.starts += 1
            raise Timeout('unknown execution')
    api = LostStart()
    result = DockerPythonRunner(engine=EngineRunner(api=api)).run(request(tmp_path))
    assert api.starts == 1 and api.removed
    assert result.failure['stage'] == 'start' and result.failure['retryCount'] == 0


def test_read_retry_never_accepts_response_after_original_deadline(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    from requests.exceptions import ConnectionError
    clock = [0.0]
    class SlowRead(FakeAPI):
        calls = 0
        timeout = 3
        def inspect_container(self, container):
            self.calls += 1
            if self.calls == 1:
                clock[0] = .9
                raise ConnectionError()
            assert self.timeout <= .051
            clock[0] = 2
            return super().inspect_container(container)
    api = SlowRead()
    runner = DockerPythonRunner(engine=EngineRunner(api=api),monotonic=lambda:clock[0],sleep=lambda delay:clock.__setitem__(0,clock[0]+delay))
    result = runner.run(request(tmp_path,deadline=1))
    assert result.termination_reason == 'infrastructureError'
    assert result.failure['stage'] == 'inspect'


def test_cleanup_failure_type_matches_cleanup_stage(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    from requests.exceptions import ReadTimeout
    class Broken(FakeAPI):
        def create_container(self, **kwargs): raise ReadTimeout()
    result = DockerPythonRunner(engine=EngineRunner(api=Broken(fail_remove=True))).run(request(tmp_path))
    assert result.failure['stage'] == 'remove' and result.failure['errorType'] == 'OSError'


def test_container_create_uses_remaining_startup_budget_without_replay(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    from requests.exceptions import ReadTimeout
    clock = [0.0]
    class ColdDocker(FakeAPI):
        timeout = 3
        creates = 0
        def create_container(self, **kwargs):
            self.creates += 1
            if self.timeout < 4: raise ReadTimeout()
            assert self.timeout <= 6
            clock[0] = 4
            return super().create_container(**kwargs)
    api = ColdDocker()
    runner = DockerPythonRunner(engine=EngineRunner(api=api),monotonic=lambda:clock[0])
    result = runner.run(request(tmp_path,deadline=6))
    assert result.termination_reason == 'exited' and api.creates == 1
    assert api.timeout == 3


def test_execution_deadline_during_inspect_remains_user_timeout(tmp_path):
    from services.oj_worker.engine_runner import EngineRunner
    clock = [0.0]
    class Busy(FakeAPI):
        def attach(self,*args,**kwargs): return iter([(None,b'FM_STARTED:test\n')])
        def start(self,container):
            import time
            time.sleep(.01)
        def inspect_container(self,container):
            clock[0] = .11
            return super().inspect_container(container)
    api = Busy(running=True)
    result= DockerPythonRunner(engine=EngineRunner(api=api),container_name_factory=lambda:'test',monotonic=lambda:clock[0]).run(request(tmp_path,policy=ResourcePolicy(wall_time_seconds=.1)))
    assert result.termination_reason == 'wallTimeExceeded' and api.killed


def test_short_connection_budget_is_not_cached_for_future_tasks(monkeypatch):
    import services.oj_worker.engine_runner as module
    import threading
    monkeypatch.setattr(module,'_clients',threading.local())
    monkeypatch.setattr(module,'_connection_options',lambda:{})
    seen=[]
    class Client:
        def __init__(self,**kwargs): self.timeout=kwargs['timeout'];seen.append(self.timeout)
    monkeypatch.setattr(module.docker,'APIClient',Client)
    assert module.engine_client(timeout=.1).timeout == 3
    assert module.engine_client().timeout == 3 and seen == [.1]

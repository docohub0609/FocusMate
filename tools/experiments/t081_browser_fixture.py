"""Isolated HTTP/storage fixture, with explicit FakeRunner or real Docker mode."""
import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Thread, Event
from apps.api.__main__ import create_server
from apps.api.problem_storage import ProblemRepository
from apps.api.submission_storage import SubmissionRepository
from services.oj_worker.editor_runs import RunRepository, RunWorker
from services.oj_worker.judge import JudgeSubmissionProcessor, InMemoryTrustedAssetProvider, OJResultRepository
from services.oj_worker.queue import WorkerQueue
from services.oj_worker.worker import Worker
from tests.python.test_submission_storage import frozen_problem
from tests.python.test_oj_judge import run_result, bundle

path = Path(os.environ['FOCUSMATE_DB_PATH'])
frozen_problem(path, 'ready')
SubmissionRepository(path)
class Runner:
    def run(self, request):
        text = (request.workspace/'main.py').read_text(encoding='utf-8')
        if '# SYSTEM' in text:
            return replace(run_result(reason='infrastructureError'), failure={'stage':'connect','errorType':'Timeout','elapsedMs':3000,'retryCount':1})
        if request.operation == 'compile':
            return run_result(stderr=b'File "main.py", line 1\nSyntaxError: expected colon',exit_code=1) if '# CE' in text else run_result()
        if '# TLE' in text: return run_result(reason='wallTimeExceeded',exit_code=-9)
        if '# RE' in text: return run_result(stderr=b'ZeroDivisionError',exit_code=1)
        if '# WA' in text: return run_result(b'wrong\n')
        return run_result(b'1 2\n')
from services.oj_worker.docker_runner import DockerPythonRunner
runner= DockerPythonRunner(startup_timeout_seconds=10) if os.getenv('T081_REAL_DOCKER') == '1' else Runner()
runs=RunWorker(RunRepository(path), path.parent/'runs',runner=None if os.getenv('T081_REAL_DOCKER') == '1' else runner)
judge=JudgeSubmissionProcessor(runner,InMemoryTrustedAssetProvider([bundle((b'3\n1 2 3\n',b'1 2\n'))]),OJResultRepository(path))
oj=Worker(WorkerQueue(path),judge,'browser-fixture',path.parent/'oj')
stop=Event()
def work():
    while not stop.wait(.1):
        runs.process_once(); oj.process_once()
Thread(target=work,daemon=True).start()
server=create_server(port=0,problem_repository=ProblemRepository(path))
print(json.dumps({'port':server.server_port}),flush=True)
try: server.serve_forever()
finally: stop.set(); server.server_close()

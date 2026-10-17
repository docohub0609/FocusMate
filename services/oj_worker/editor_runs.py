"""Durable user-requested runs. Never create an OJ verdict or update a goal."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
import traceback
from uuid import uuid4

from .docker_runner import DockerPythonRunner, PythonRunRequest, DEFAULT_POLICY
from .judge import outputs_equal
from .queue import timestamp, utc_now
from src.focusmate.adapters.raw_events import SQLiteEventStore, event_from_record


class RunFailure(Exception):
    def __init__(self, code, message='运行请求无法处理。', status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def report_worker_error(stage, error, run_id=None, *, service="focusmate-run-worker"):
    # Do not log exception messages, source lines or locals: they may contain code/input.
    frames = [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
              for f in traceback.extract_tb(error.__traceback__)]
    print(json.dumps({'service': service, 'stage': stage, 'runId': run_id,
                      'errorType': type(error).__name__, 'frames': frames}), file=sys.stderr, flush=True)


class RunRepository:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS editor_runs (
                run_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_records(goal_id),
                problem_id TEXT NOT NULL, problem_version INTEGER NOT NULL, code_version INTEGER NOT NULL,
                run_mode TEXT NOT NULL, operation_id TEXT NOT NULL, request_json TEXT NOT NULL,
                inputs_json TEXT NOT NULL, code_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, token TEXT, lease_until REAL, container_name TEXT,
                workspace TEXT, result_json TEXT, finished_at TEXT, cleanup_pending INTEGER NOT NULL DEFAULT 0,
                UNIQUE(goal_id,operation_id))''')
            SQLiteEventStore.initialize(db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def view(row):
        return {'runId': row['run_id'], 'goalContext': {'goalId': row['goal_id'], 'scope': 'goal'},
                'problemRef': {'problemId': row['problem_id'], 'problemVersion': row['problem_version']},
                'codeVersion': row['code_version'], 'runMode': row['run_mode'], 'createdAt': row['created_at'],
                'status': row['status'], 'finishedAt': row['finished_at'],
                'result': json.loads(row['result_json']) if row['result_json'] else None}

    def get(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM editor_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None: raise RunFailure('RUN_NOT_FOUND', '运行记录不存在。', 404)
        return self.view(row)

    def enqueue(self, request):
        fields = {'goalContext','problemRef','codeVersion','runMode','operationId'}
        if not isinstance(request, dict): raise RunFailure('INVALID_RUN', status=422)
        custom = request.get('runMode') == 'customInput'
        if (set(request) != fields | ({'stdin'} if custom else set())
                or request.get('runMode') not in ('customInput','publicExample')
                or type(request.get('codeVersion')) is not int or request['codeVersion'] < 1
                or not isinstance(request.get('operationId'), str) or not 1 <= len(request['operationId']) <= 256
                or not isinstance(request.get('goalContext'), dict)
                or set(request['goalContext']) != {'goalId','scope'} or request['goalContext'].get('scope') != 'goal'
                or not isinstance(request['goalContext'].get('goalId'), str)
                or not isinstance(request.get('problemRef'), dict)
                or set(request['problemRef']) != {'problemId','problemVersion'}
                or not isinstance(request['problemRef'].get('problemId'), str)
                or type(request['problemRef'].get('problemVersion')) is not int
                or (custom and (not isinstance(request['stdin'], str) or len(request['stdin'].encode()) > DEFAULT_POLICY.stdin_bytes))):
            raise RunFailure('INVALID_RUN', '运行模式、代码版本或输入无效。', 422)
        goal_id = request['goalContext']['goalId']; ref = request['problemRef']
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=False)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM editor_runs WHERE goal_id=? AND operation_id=?', (goal_id, request['operationId'])).fetchone()
            if previous:
                if previous['request_json'] != canonical: raise RunFailure('IDEMPOTENCY_CONFLICT', '同一运行编号不能更换代码或输入。')
                return self.view(previous)
            source = db.execute('''SELECT c.content_hash,c.source_code FROM code_snapshots c
                JOIN goal_records g ON g.goal_id=c.goal_id
                JOIN problem_versions p ON p.problem_id=g.problem_id AND p.version=g.problem_version
                WHERE c.goal_id=? AND c.code_version=? AND g.problem_id=? AND g.problem_version=? AND p.lifecycle_status='frozen' ''',
                (goal_id, request['codeVersion'], ref['problemId'], ref['problemVersion'])).fetchone()
            if source is None or sha256(source['source_code'].encode()).hexdigest() != source['content_hash']:
                raise RunFailure('RUN_CONTEXT_INVALID', '题目或保存的代码版本不匹配。')
            if custom:
                cases = [{'stdin': request['stdin'], 'expectedOutput': None}]
            else:
                cases = [{'stdin': r[0], 'expectedOutput': r[1]} for r in db.execute(
                    'SELECT input_text,output_text FROM problem_examples WHERE problem_id=? AND version=? ORDER BY example_index', (ref['problemId'], ref['problemVersion']))]
            if not cases or any(len(c['stdin'].encode()) > DEFAULT_POLICY.stdin_bytes or (c['expectedOutput'] is not None and len(c['expectedOutput'].encode()) > DEFAULT_POLICY.output_bytes) for c in cases):
                raise RunFailure('RUN_INPUT_TOO_LARGE', '样例为空或输入输出超过执行限制。', 422)
            run_id, now = 'run-' + uuid4().hex, timestamp(utc_now())
            db.execute('''INSERT INTO editor_runs(run_id,goal_id,problem_id,problem_version,code_version,run_mode,
                operation_id,request_json,inputs_json,code_hash,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued')''',
                (run_id, goal_id, ref['problemId'], ref['problemVersion'], request['codeVersion'], request['runMode'], request['operationId'], canonical, json.dumps(cases, ensure_ascii=False), source['content_hash'], now))
            row = db.execute('SELECT * FROM editor_runs WHERE run_id=?', (run_id,)).fetchone()
            self._event(db, row, 'runRequested')
            from focusmate.adapters.coach_events import capture_execution
            capture_execution(db, 'run', run_id, goal_id, request['codeVersion'])
            return self.view(row)

    def _event(self, db, row, name):
        record = self.view(row)
        if name == 'runFinished': record['createdAt'] = row['finished_at']
        if row['run_mode'] == 'customInput': record['inputRef'] = row['run_id']
        event = event_from_record(name, record)
        # The trusted resolver compares against the row in this transaction, not client claims.
        def resolve(candidate):
            return candidate['payload'] == event['payload'] and candidate['goalContext'] == event['goalContext']
        SQLiteEventStore(self.path, initialize=False, reference_resolver=resolve).append_on_connection(db, event, trusted_source=event['source'])

    def claim(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM editor_runs WHERE status='queued' ORDER BY created_at,run_id LIMIT 1").fetchone()
            if row is None: return None
            token = uuid4().hex
            db.execute("UPDATE editor_runs SET status='running',token=?,lease_until=? WHERE run_id=?", (token, time.time()+60, row['run_id']))
            return dict(db.execute('SELECT * FROM editor_runs WHERE run_id=?', (row['run_id'],)).fetchone())

    def owned_update(self, claim, sql, params=()):
        with closing(self.connect()) as db, db:
            changed = db.execute(sql + " WHERE run_id=? AND token=? AND status='running' AND lease_until>?",
                                 (*params, claim['run_id'], claim['token'], time.time())).rowcount
        if changed != 1: raise RunFailure('RUN_OWNERSHIP_LOST', '本次运行已中断。')

    def renew(self, claim):
        self.owned_update(claim, 'UPDATE editor_runs SET lease_until=?', (time.time()+60,))

    def finish(self, claim, status, result):
        if status not in ('finished','failed'): raise ValueError('invalid terminal status')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE editor_runs SET status=?,result_json=?,finished_at=? WHERE run_id=? AND token=? AND status='running' AND lease_until>?",
                (status, json.dumps(result, ensure_ascii=False), timestamp(utc_now()), claim['run_id'], claim['token'], time.time())).rowcount
            if changed != 1: raise RunFailure('RUN_OWNERSHIP_LOST')
            self._event(db, db.execute('SELECT * FROM editor_runs WHERE run_id=?', (claim['run_id'],)).fetchone(), 'runFinished')

    def expire(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM editor_runs WHERE status='running' AND lease_until<=?", (time.time(),)).fetchall()
            for row in rows:
                db.execute("UPDATE editor_runs SET status='interrupted',finished_at=? WHERE run_id=?", (timestamp(utc_now()), row['run_id']))
                self._event(db, db.execute('SELECT * FROM editor_runs WHERE run_id=?', (row['run_id'],)).fetchone(), 'runFinished')
            return [dict(row) for row in rows]


def preview(value, limit=16384):
    raw = value if isinstance(value, bytes) else value.encode()
    return raw[:limit].decode('utf-8', errors='replace'), len(raw) > limit


class RunWorker:
    def __init__(self, repository, root, *, runner=None):
        self.repo, self.root, self.runner = repository, Path(root).resolve(), runner
        self.root.mkdir(parents=True, exist_ok=True)

    def cleanup(self, row):
        if row['container_name'] and self.runner is None:
            name = row['container_name']
            if not name.startswith('focusmate-editor-' + row['run_id'] + '-'):
                return False
            try:
                from .engine_runner import remove_container
                if not remove_container(name): return False
            except (OSError, subprocess.TimeoutExpired): return False
        if row['workspace']:
            workspace = Path(row['workspace']).resolve()
            if workspace.parent != self.root or workspace.name != row['run_id']: return False
            if workspace.exists(): shutil.rmtree(workspace)
        with closing(self.repo.connect()) as db, db:
            db.execute('UPDATE editor_runs SET cleanup_pending=0 WHERE run_id=?', (row['run_id'],))
        return True

    def process_once(self):
        self.repo.expire()
        with closing(self.repo.connect()) as db:
            stale = [dict(r) for r in db.execute("SELECT * FROM editor_runs WHERE cleanup_pending=1 AND status!='running'")]
        for row in stale:
            try: self.cleanup(row)
            except Exception as exc:
                report_worker_error('cleanup', exc, row['run_id'])
        claim = self.repo.claim()
        if claim is None: return None
        workspace = self.root / claim['run_id']
        self.repo.owned_update(claim, 'UPDATE editor_runs SET workspace=?,cleanup_pending=1', (str(workspace),))
        claim['workspace'] = str(workspace)
        stop = Event()
        def heartbeat():
            while not stop.wait(10):
                try: self.repo.renew(claim)
                except (RunFailure, sqlite3.Error): return
        thread = Thread(target=heartbeat, daemon=True); thread.start()
        cases = json.loads(claim['inputs_json']); results = []
        status = 'finished'; deadline = time.monotonic() + 30
        failure = None; phase = 'loadCode'
        try:
            with closing(self.repo.connect()) as db:
                source = db.execute('SELECT source_code,content_hash FROM code_snapshots WHERE goal_id=? AND code_version=?', (claim['goal_id'],claim['code_version'])).fetchone()
            if source is None or sha256(source[0].encode()).hexdigest() != claim['code_hash'] or source[1] != claim['code_hash']:
                raise RunFailure('RUN_CONTEXT_INVALID')
            phase = 'prepareWorkspace'
            workspace.mkdir()
            (workspace/'main.py').write_text(source[0], encoding='utf-8')
            def execute(stdin, operation):
                nonlocal failure, phase
                phase = operation
                self.repo.renew(claim)
                remaining = deadline - time.monotonic()
                if remaining <= 0: return None
                name = 'focusmate-editor-' + claim['run_id'] + '-' + uuid4().hex
                self.repo.owned_update(claim, 'UPDATE editor_runs SET container_name=?', (name,))
                claim['container_name'] = name
                runner = self.runner or DockerPythonRunner(container_name_factory=lambda: name, startup_timeout_seconds=10)
                result = runner.run(PythonRunRequest(workspace, stdin, policy=replace(DEFAULT_POLICY, wall_time_seconds=min(3,remaining)), operation=operation, deadline=deadline))
                failure = result.failure
                self.repo.renew(claim)
                # Do not overwrite an unconfirmed container identity on the next case.
                if self.runner is None:
                    from .engine_runner import remove_container
                    if not remove_container(name): raise RunFailure('RUN_CLEANUP_PENDING')
                return result
            compiled = execute(b'', 'compile')
            compile_outcome = self.outcome(compiled, compile_only=True)
            for index, case in enumerate(cases):
                result = compiled if compile_outcome != 'executed' else execute(case['stdin'].encode(), 'execute')
                outcome = compile_outcome if compile_outcome != 'executed' else self.outcome(result)
                if outcome == 'executed' and case['expectedOutput'] is not None:
                    outcome = 'passed' if outputs_equal(result.stdout, case['expectedOutput'].encode()) else 'outputMismatch'
                stdin, input_cut = preview(case['stdin']); expected, expected_cut = preview(case['expectedOutput'] or '')
                stdout, stdout_cut = preview(result.stdout if result and outcome != 'infrastructureError' else b'')
                stderr, stderr_cut = preview(result.stderr if result and outcome != 'infrastructureError' else b'')
                results.append({'index': index+1, 'stdin': stdin, 'inputTruncated': input_cut,
                    'expectedOutput': expected if case['expectedOutput'] is not None else None, 'expectedTruncated': expected_cut,
                    'stdout': stdout, 'stderr': stderr, 'stdoutTruncated': stdout_cut or bool(result and result.stdout_truncated),
                    'stderrTruncated': stderr_cut or bool(result and result.stderr_truncated), 'outcome': outcome,
                    'wallTimeMs': result.resource_summary.wall_time_ms if result else None,
                    'executionTimeMs': result.resource_summary.execution_time_ms if result and compile_outcome == 'executed' else None,
                    'memoryLimitBytes': result.resource_summary.memory_limit_bytes if result else None,
                    'peakMemoryBytes': result.resource_summary.peak_memory_bytes if result else None})
                if outcome == 'infrastructureError':
                    status = 'failed'
                    break
            phase = 'saveResult'
            self.repo.finish(claim, status, {'cases': results, 'failure': failure})
        except Exception as exc:
            # A task boundary: an unexpected adapter fault must not kill the queue.
            # The original execution is never replayed, even if saving its failure fails.
            report_worker_error(phase, exc, claim['run_id'])
            failure = failure or {'stage': phase, 'errorType': type(exc).__name__, 'retryCount': 0,
                                  'elapsedMs': max(0,round((time.monotonic()-(deadline-30))*1000))}
            try: self.repo.finish(claim, 'failed', {'cases': results, 'failureCode': 'RUN_SYSTEM_ERROR', 'failure': failure})
            except Exception as save_error:
                report_worker_error('saveFailure', save_error, claim['run_id'])
        finally:
            stop.set(); thread.join()
            try: self.cleanup(claim)
            except Exception as exc:
                report_worker_error('cleanup', exc, claim['run_id'])
        return claim['run_id']

    @staticmethod
    def outcome(result, *, compile_only=False):
        if result is None: return 'notExecuted'
        if result.termination_reason in ('infrastructureError', 'startupTimeExceeded'): return 'infrastructureError'
        if result.termination_reason == 'memoryLimitExceeded' or result.oom_killed: return 'memoryLimitExceeded'
        if result.termination_reason == 'outputLimitExceeded' or result.stdout_truncated or result.stderr_truncated: return 'outputLimitExceeded'
        if result.termination_reason in ('wallTimeExceeded','cpuTimeExceeded') or result.exit_code in (137,-9): return 'timeLimitExceeded'
        if result.termination_reason != 'exited' or result.exit_code != 0:
            return 'compileError' if compile_only else 'runtimeError'
        return 'executed'

"""Persist and bound public-only tool runs; reuse the existing sample judge."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from .docker_runner import DockerPythonRunner
from .public_samples import PublicSampleExecutor, PublicSampleRunRequest, SQLitePublicTestProvider, PublicSampleFailure
from src.focusmate.adapters.public_run_events import backfill_public_run_events, capture_contract_version


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _text(value, limit=2048):
    raw = value if isinstance(value, bytes) else value.encode()
    return raw[:limit].decode('utf-8', errors='ignore'), len(raw) > limit


class _Stopped(Exception):
    pass


class _CleanupPending(Exception):
    pass


class PublicRunService:
    def __init__(self, database_path, work_root, runner=None, task_database_path=None):
        self.database_path = Path(database_path)
        self.root = Path(work_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = Path(task_database_path) if task_database_path else self.root / 'tool-tasks.sqlite3'
        self.runner = runner
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_public_tasks (
                run_id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                owner TEXT, deadline REAL NOT NULL, context_json TEXT NOT NULL,
                result_json TEXT, container_name TEXT, workspace TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS tool_public_event_metadata (
                run_id TEXT PRIMARY KEY, finished_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS tool_public_event_context (
                run_id TEXT PRIMARY KEY, contract_version INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS tool_public_comparison (
                run_id TEXT PRIMARY KEY, conditions_hash TEXT NOT NULL)''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS tool_public_terminal_time
                AFTER UPDATE OF status ON tool_public_tasks
                WHEN NEW.status IN ('succeeded','failed','cancelled','interrupted')
                 AND OLD.status IN ('waiting','running')
                BEGIN
                  INSERT OR IGNORE INTO tool_public_event_metadata VALUES
                    (NEW.run_id, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END''')
            db.commit()
        self.event_receipts = []

    def record_events(self, run_id=None):
        self.event_receipts = backfill_public_run_events(self.database_path, self.path, run_id=run_id)
        return self.event_receipts

    def _record(self, run_id):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM tool_public_tasks WHERE run_id=?', (run_id,)).fetchone()
            return dict(row) if row else None

    def _view(self, row):
        if row['result_json']:
            return json.loads(row['result_json'])
        return {'runId': row['run_id'], 'status': row['status'],
                **json.loads(row['context_json'])}

    def run(self, context, code, bundle=None):
        now = datetime.now(timezone.utc).timestamp()
        identity = {key: context[key] for key in ('goalContext', 'problemRef') if key in context}
        if 'codeVersion' in code:
            identity['codeVersion'] = code['codeVersion']
        try:
            parsed_deadline = datetime.fromisoformat(context['deadlineAt'].replace('Z', '+00:00'))
            if parsed_deadline.tzinfo is None:
                raise ValueError('deadline requires timezone')
            deadline = min(parsed_deadline.timestamp(), now + 30)
            source = code['sourceCode']
            if deadline <= now or sha256(source.encode()).hexdigest() != code['contentHash'].removeprefix('sha256:'):
                raise ValueError('invalid snapshot or expired deadline')
            problem = context['problemRef']
            if bundle is None and not self.database_path.is_file():
                raise ValueError('business database unavailable')
            bundle = bundle or SQLitePublicTestProvider(self.database_path).get(problem['problemId'], problem['problemVersion'])
            if not 1 <= len(bundle.cases) <= 8 or (bundle.problem_id, bundle.problem_version) != (problem['problemId'], problem['problemVersion']):
                raise ValueError('invalid public sample bundle')
            identity = {'goalContext': context['goalContext'], 'problemRef': problem, 'codeVersion': code['codeVersion'], 'loopId': context['loopId']}
            key = _digest([context['loopId'], identity, code['contentHash'], bundle.asset_id, bundle.asset_version,
                           [(c.test_case_id, c.stdin.hex(), c.expected_output.hex()) for c in bundle.cases], 'python-default-v1'])
        except (KeyError, ValueError, TypeError, sqlite3.Error, PublicSampleFailure):
            return {'status': 'failed', 'failure': 'PUBLIC_RUN_INPUT_INVALID', **identity}
        run_id, owner = 'tool-run-' + uuid4().hex, uuid4().hex
        workspace = self.root / run_id
        contract_version = capture_contract_version(self.database_path, context['goalContext'])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT run_id FROM tool_public_tasks WHERE dedup_key=?', (key,)).fetchone()
            if row:
                return self._view(self._record(row[0]))
            # A changed snapshot/bundle in the same logical turn is not permission
            # to physically run again. Only an identical request reuses the task.
            previous = db.execute("SELECT run_id FROM tool_public_tasks WHERE json_extract(context_json,'$.loopId')=?", (context['loopId'],)).fetchone()
            if previous:
                return {'status': 'failed', 'failure': 'PUBLIC_RUN_LIMIT_REACHED', **identity}
            db.execute('INSERT INTO tool_public_tasks VALUES (?,?,?,?,?,?,?,?,?)',
                       (run_id, key, 'waiting', None, deadline, json.dumps(identity), None, None, str(workspace)))
            db.execute('INSERT INTO tool_public_event_context VALUES (?,?)', (run_id, contract_version))
            # Capture the actual immutable inputs now: current sample tables may
            # change later, and the dedup key also contains the unrelated loop ID.
            conditions_hash = _digest([code['contentHash'], bundle.asset_id, bundle.asset_version,
                [(c.test_case_id, c.stdin.hex(), c.expected_output.hex()) for c in bundle.cases],
                'python-default-v1'])
            db.execute('INSERT INTO tool_public_comparison VALUES (?,?)', (run_id, conditions_hash))
            db.execute("UPDATE tool_public_tasks SET status='running',owner=? WHERE run_id=? AND status='waiting'", (owner, run_id))
            db.commit()
        captured = []
        service = self

        class BoundedRunner:
            def run(self, request):
                row = service._record(run_id)
                remaining = deadline - datetime.now(timezone.utc).timestamp()
                if row['status'] != 'running' or row['owner'] != owner or remaining <= 0:
                    raise _Stopped()
                name = 'focusmate-tool-' + uuid4().hex
                with closing(sqlite3.connect(service.path)) as db:
                    changed = db.execute("UPDATE tool_public_tasks SET container_name=? WHERE run_id=? AND owner=? AND status='running'", (name, run_id, owner)).rowcount
                    db.commit()
                if not changed:
                    raise _Stopped()
                # Cancellation can race container creation. Recheck immediately before launch,
                # then check ownership again after the bounded runner returns. A process started
                # in the remaining race window is still bounded and removed in final cleanup.
                row = service._record(run_id)
                if row['status'] != 'running' or row['owner'] != owner:
                    raise _Stopped()
                runner = service.runner or DockerPythonRunner(container_name_factory=lambda: name)
                result = runner.run(replace(request, policy=replace(request.policy, wall_time_seconds=min(request.policy.wall_time_seconds, remaining))))
                # Confirm this container is gone before another step may overwrite
                # its durable name. The base runner's best-effort removal is not proof.
                if service.runner is None:
                    try:
                        service._remove_container(name)
                    except OSError as exc:
                        raise _CleanupPending() from exc
                if result.stdout_truncated or result.stderr_truncated:
                    result = replace(result, termination_reason='outputLimitExceeded')
                captured.append((request.operation, result))
                row = service._record(run_id)
                if row['status'] != 'running' or row['owner'] != owner or datetime.now(timezone.utc).timestamp() >= deadline:
                    raise _Stopped()
                return result

        class Tests:
            def get(self, *args):
                return bundle

        result = {'runId': run_id, **identity}
        try:
            workspace.mkdir()
            (workspace / 'main.py').write_text(source, encoding='utf-8')
            execution = PublicSampleExecutor(BoundedRunner(), Tests()).run(PublicSampleRunRequest(
                context['goalContext']['goalId'], bundle.problem_id, bundle.problem_version, code['codeVersion'], workspace))
            result.update(status='failed' if execution['status'] == 'infrastructureError' else 'succeeded',
                          sampleStatus=execution['status'], caseResults=[])
            compile_result = captured[0][1]
            compile_incomplete = compile_result.termination_reason != 'exited'
            if compile_incomplete:
                result.update(status='failed', sampleStatus='infrastructureError', failure='PUBLIC_COMPILE_INCOMPLETE')
            outputs = [r for op, r in captured if op == 'execute']
            for index, (case, outcome) in enumerate(zip(bundle.cases, execution['caseResults'])):
                raw = outputs[index] if index < len(outputs) else captured[0][1]
                item = dict(outcome)
                if compile_incomplete:
                    item['outcome'] = 'notExecuted'
                elif raw.termination_reason == 'outputLimitExceeded':
                    item['outcome'] = 'outputLimitExceeded'
                for field, value in [('input', case.stdin), ('expectedOutput', case.expected_output), ('actualOutput', raw.stdout)]:
                    item[field], item[field + 'Truncated'] = _text(value)
                # Keep only public Python exception summaries; never expose host traceback paths.
                errors = raw.stderr.decode('utf-8', errors='replace').splitlines()
                safe = next((line.split(':', 1)[0] for line in reversed(errors)
                             if re.match(r'^[A-Za-z]+(?:Error|Exception)(?::|$)', line)), '')
                item['error'], item['errorTruncated'] = _text(safe)
                item['terminationReason'] = raw.termination_reason
                item['exitCode'] = raw.exit_code
                result['caseResults'].append(item)
            # Shrink displays only; all samples/outcomes remain and comparisons are already complete.
            # Leave room for the gateway's bounded identity/result envelope.
            while len(json.dumps(result, ensure_ascii=False).encode()) > 14000:
                fields = [(item, field) for item in result['caseResults'] for field in ('input', 'expectedOutput', 'actualOutput', 'error') if item[field]]
                if not fields:
                    break
                item, field = max(fields, key=lambda pair: len(pair[0][pair[1]].encode()))
                item[field], _ = _text(item[field], max(0, len(item[field].encode()) // 2))
                item[field + 'Truncated'] = True
        except _CleanupPending:
            result.update(status='failed', failure='PUBLIC_RUN_CLEANUP_PENDING', cleanupPending=True)
        except _Stopped:
            result.update(status='interrupted', failure='PUBLIC_RUN_STOPPED')
        except Exception:
            result.update(status='failed', failure='PUBLIC_RUN_UNAVAILABLE')
        finally:
            try:
                self._cleanup(self._record(run_id))
            except OSError:
                result.update(status='failed', failure='PUBLIC_RUN_CLEANUP_PENDING', cleanupPending=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            finished_at = datetime.now(timezone.utc).timestamp()
            if finished_at >= deadline:
                result.update(status='interrupted', failure='PUBLIC_RUN_DEADLINE_EXCEEDED')
            # Re-evaluate at the final transaction, after potentially slow cleanup.
            # The owner and deadline predicates guard late writes and cancellation.
            expired = dict(result, status='interrupted', failure='PUBLIC_RUN_DEADLINE_EXCEEDED')
            db.execute("UPDATE tool_public_tasks SET status='interrupted',result_json=?,owner=NULL WHERE run_id=? AND owner=? AND status='running' AND deadline<=?",
                       (json.dumps(expired), run_id, owner, finished_at))
            db.execute("UPDATE tool_public_tasks SET status=?,result_json=?,owner=NULL WHERE run_id=? AND owner=? AND status='running' AND deadline>?",
                       (result['status'], json.dumps(result), run_id, owner, finished_at))
            db.commit()
        self.record_events(run_id)
        return self._view(self._record(run_id))

    def _cleanup(self, row):
        if row['container_name'] and self.runner is None:
            self._remove_container(row['container_name'])
        path = Path(row['workspace']).resolve()
        if path.parent == self.root and path.name == row['run_id'] and path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _remove_container(name):
        try:
            removed = DockerPythonRunner().docker.control(['docker', 'rm', '--force', name])
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError('container cleanup unavailable') from exc
        if removed.returncode and 'No such container' not in removed.stderr:
            raise OSError('container cleanup not confirmed')

    def cancel(self, run_id):
        return self._stop(run_id, 'cancelled')

    def _stop(self, run_id, status):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE tool_public_tasks SET status=?,owner=NULL WHERE run_id=? AND status IN ('waiting','running')", (status, run_id))
            db.commit()
        row = self._record(run_id)
        if row:
            try:
                self._cleanup(row)
            except OSError:
                result = self._view(row)
                result['cleanupPending'] = True
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute('UPDATE tool_public_tasks SET result_json=? WHERE run_id=?', (json.dumps(result), run_id))
                    db.commit()
                return result
            self.record_events(run_id)
            return self._view(row)
        return {'status': 'failed', 'failure': 'PUBLIC_RUN_UNAVAILABLE'}

    def recover(self):
        """Expire abandoned ownership without replaying uncertain work."""
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT run_id FROM tool_public_tasks WHERE status IN ('waiting','running') AND deadline<=?", (datetime.now(timezone.utc).timestamp(),)).fetchall()
            pending = db.execute("SELECT run_id FROM tool_public_tasks WHERE json_extract(result_json, '$.cleanupPending')=1").fetchall()
        results = [self._stop(row[0], 'interrupted') for row in rows]
        for (run_id,) in pending:
            row = self._record(run_id)
            try:
                self._cleanup(row)
            except OSError:
                results.append(self._view(row))
                continue
            result = self._view(row)
            result['cleanupPending'] = False
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('UPDATE tool_public_tasks SET result_json=? WHERE run_id=?', (json.dumps(result), run_id))
                db.commit()
            results.append(result)
        self.record_events()
        return results

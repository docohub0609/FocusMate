"""SQLite organization transactions. Never imports Core orchestration."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from ..contracts.observations import (KINDS, canonical, digest, epoch, expire,
    organize, read_observation, scope_for, timestamp)
from ..contracts.raw_events import read_event


class OrganizationInputError(ValueError):
    """An invalid saved source is quarantined; database errors are not swallowed."""


class SQLiteObservationStore:
    def __init__(self, path, *, public_task_db=None):
        self.path = Path(path)
        if not self.path.is_file():
            raise ValueError('business database does not exist')
        self.public_task_db = Path(public_task_db) if public_task_db else None
        with closing(self._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS observation_cursors (
                    goal_id TEXT PRIMARY KEY, receive_sequence INTEGER NOT NULL,
                    received_time REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS observation_pending (
                    scope_key TEXT PRIMARY KEY, goal_id TEXT NOT NULL, pending_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_id TEXT NOT NULL UNIQUE, goal_id TEXT NOT NULL,
                    observation_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observation_failures (
                    goal_id TEXT NOT NULL, receive_sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(goal_id,receive_sequence));
                CREATE TABLE IF NOT EXISTS observation_summaries (
                    signature TEXT PRIMARY KEY, goal_id TEXT NOT NULL, summary_json TEXT NOT NULL);
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _list(self, table, column, goal_id):
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute(
                f'SELECT {column} FROM {table} WHERE goal_id=? ORDER BY rowid', (goal_id,))]

    def list_observations(self, goal_id):
        return [read_observation(o) for o in self._list('observations', 'observation_json', goal_id)]

    def list_pending(self, goal_id):
        return self._list('observation_pending', 'pending_json', goal_id)

    def list_summaries(self, goal_id):
        return [s for s in self._list('observation_summaries', 'summary_json', goal_id) if s['count'] > 1]

    def list_failures(self, goal_id):
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM observation_failures WHERE goal_id=? ORDER BY receive_sequence', (goal_id,))]

    @staticmethod
    def _snapshot(db, goal, version):
        if version is None:
            return None
        row = db.execute('SELECT code_snapshot_id,source_code,content_hash FROM code_snapshots WHERE goal_id=? AND code_version=?', (goal, version)).fetchone()
        if row is None:
            return None
        return {'ref': row['code_snapshot_id'], 'source': row['source_code'], 'hash': row['content_hash']}

    @staticmethod
    def _context(db, event, goal):
        if event['goalContext']['goalId'] != goal:
            raise OrganizationInputError('EVENT_GOAL_MISMATCH')
        row = db.execute('SELECT problem_id,problem_version FROM goal_records WHERE goal_id=?', (goal,)).fetchone()
        if row is None:
            raise OrganizationInputError('GOAL_UNAVAILABLE')
        problem = {'problemId': row[0], 'problemVersion': row[1]}
        payload = event['payload']
        if payload.get('problemRef', problem) != problem:
            raise OrganizationInputError('EVENT_PROBLEM_MISMATCH')
        if event['goalContext']['scope'] == 'subgoal' and event.get('eventVersion') == 2:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_contract_versions'").fetchone()
            contract = db.execute('SELECT compilation_json,status FROM goal_contract_versions WHERE goal_id=? AND version=?',
                (goal, event.get('contractVersion'))).fetchone() if exists else None
            try:
                valid = contract and contract[1] in ('frozen', 'superseded') and event['goalContext']['subgoalId'] in {
                    s['subgoalId'] for s in json.loads(contract[0])['contract']['subgoals']}
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise OrganizationInputError('CONTRACT_REFERENCE_INVALID')
        elif event['goalContext']['scope'] == 'subgoal':
            raise OrganizationInputError('LEGACY_CONTRACT_UNAVAILABLE')
        if 'codeVersion' in payload:
            code = SQLiteObservationStore._snapshot(db, goal, payload['codeVersion'])
            if code is None or ('artifactRef' in payload and payload['artifactRef'] != code['ref']):
                raise OrganizationInputError('CODE_REFERENCE_INVALID')
        if event['eventName'] in ('submissionRequested', 'ojResultProduced') and 'submissionId' in payload:
            submission = db.execute('SELECT goal_id,code_version,problem_id,problem_version FROM submissions WHERE submission_id=?', (payload['submissionId'],)).fetchone()
            if submission is None or submission['goal_id'] != goal or (submission['problem_id'], submission['problem_version']) != (problem['problemId'], problem['problemVersion']) or payload.get('codeVersion', submission['code_version']) != submission['code_version']:
                raise OrganizationInputError('SUBMISSION_REFERENCE_INVALID')
            # Version is obtained from the exact saved submission for legacy events only.
            payload.setdefault('codeVersion', submission['code_version'])
        if event['eventName'] == 'ojResultProduced':
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oj_results'").fetchone()
            result = db.execute('SELECT submission_id,goal_id,code_version,verdict FROM oj_results WHERE oj_result_id=?', (payload['ojResultId'],)).fetchone() if exists else None
            if result is None or tuple(result) != (payload['submissionId'], goal, payload['codeVersion'], payload['verdict']):
                raise OrganizationInputError('RESULT_REFERENCE_INVALID')
        return scope_for(event, problem)

    def _public_comparison(self, event):
        """Only immutable persisted conditions and complete safe results may compare."""
        name = event['eventName']
        if name not in ('publicRunFinished', 'runRequested') or self.public_task_db is None or 'runId' not in event['payload']:
            return None
        with closing(sqlite3.connect(self.public_task_db.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            has_comparison = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_public_comparison'").fetchone()
            row = db.execute('SELECT context_json,result_json,status FROM tool_public_tasks WHERE run_id=?', (event['payload']['runId'],)).fetchone()
            comparison = db.execute('SELECT conditions_hash FROM tool_public_comparison WHERE run_id=?', (event['payload']['runId'],)).fetchone() if has_comparison else None
        if row is None:
            raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID')
        try:
            context, result = json.loads(row[0]), json.loads(row[1]) if row[1] else {}
            if not isinstance(context, dict) or not isinstance(result, dict):
                raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID')
            payload = event['payload']
            expected = {'goalContext': event['goalContext'], **{k: payload[k] for k in ('codeVersion', 'problemRef')}}
            if name == 'runRequested':
                if any(context.get(k) != v for k, v in expected.items()):
                    raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID')
                return None
            expected['loopId'] = payload['loopId']
            if any(context.get(k) != v for k, v in expected.items()) or row[2] != payload['status']:
                raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID')
            if row[1] is None and row[2] in ('cancelled', 'interrupted') and 'sampleStatus' not in payload:
                return None
            if any(context.get(k) != v or result.get(k) != v for k, v in expected.items()) or result.get('runId') != payload['runId'] or row[2] != payload['status'] or result.get('status') != payload['status'] or result.get('sampleStatus') != payload.get('sampleStatus'):
                raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID')
            if not comparison or row[2] != 'succeeded' or result.get('sampleStatus') not in ('passed', 'failed'):
                return None
            cases = result.get('caseResults')
            if not isinstance(cases, list) or not cases:
                return None
            for case in cases:
                if not isinstance(case, dict):
                    raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID')
                if case.get('outcome') in ('notExecuted', 'outputLimitExceeded', 'infrastructureError') or case.get('terminationReason') != 'exited':
                    return None
                if any(case.get(field + 'Truncated') is not False or not isinstance(case.get(field), str)
                       for field in ('input', 'expectedOutput', 'actualOutput', 'error')):
                    return None
            # The rest of the result remains part of equality, including every case outcome.
            semantic = {k: v for k, v in result.items() if k not in ('runId', 'loopId', 'goalContext', 'problemRef', 'codeVersion')}
            return {'conditionsHash': comparison[0], 'resultHash': digest(semantic)}
        except (ValueError, KeyError, TypeError) as error:
            if isinstance(error, OrganizationInputError):
                raise
            raise OrganizationInputError('PUBLIC_RESULT_REFERENCE_INVALID') from None

    @staticmethod
    def _link_request(db, observation):
        if observation['kind'] not in ('publicRunFinished', 'ojResultProduced'):
            return
        field = 'runId' if observation['kind'] == 'publicRunFinished' else 'submissionId'
        identifier = observation['evidence'].get(field)
        matches = []
        for row in db.execute('SELECT observation_json FROM observations WHERE goal_id=?', (observation['goalContext']['goalId'],)):
            candidate = json.loads(row[0])
            if (candidate.get('kind') in ('runRequested', 'submissionRequested')
                    and candidate['evidence'].get(field) == identifier
                    and candidate['evidence'].get('codeVersion') == observation['evidence'].get('codeVersion')
                    and candidate['organization']['scope'] == observation['organization']['scope']):
                matches.append(candidate['observationId'])
        if matches:
            observation['evidence']['requestObservationIds'] = matches
        else:
            observation['uncertainty'].append('REQUEST_OBSERVATION_UNAVAILABLE')

    def _persist(self, db, goal, pendings, observations, comparison=None):
        db.execute('DELETE FROM observation_pending WHERE goal_id=?', (goal,))
        for key, pending in pendings.items():
            db.execute('INSERT INTO observation_pending VALUES(?,?,?)', (key, goal, canonical(pending)))
        created = 0
        for observation in observations:
            self._link_request(db, observation)
            comparable = comparison if observation['kind'] == 'publicRunFinished' else None
            if comparable:
                observation['uncertainty'].remove('RESULT_COMPARISON_UNAVAILABLE')
                observation['evidence']['comparison'] = comparable
            read_observation(observation)
            inserted = db.execute('INSERT OR IGNORE INTO observations(observation_id,goal_id,observation_json) VALUES(?,?,?)',
                (observation['observationId'], goal, canonical(observation))).rowcount
            created += inserted
            if comparable and inserted:
                signature = digest([observation['organization']['scope'], observation['evidence']['codeVersion'], comparable])
                previous = db.execute('SELECT summary_json FROM observation_summaries WHERE signature=?', (signature,)).fetchone()
                summary = json.loads(previous[0]) if previous else {'goalContext': deepcopy(observation['goalContext']),
                    'scope': deepcopy(observation['organization']['scope']), 'comparison': comparable,
                    'codeVersion': observation['evidence']['codeVersion'], 'count': 0, 'observationIds': [], 'runIds': []}
                if observation['evidence']['runId'] not in summary['runIds']:
                    summary['count'] += 1
                    summary['observationIds'].append(observation['observationId'])
                    summary['runIds'].append(observation['evidence']['runId'])
                db.execute('INSERT INTO observation_summaries VALUES(?,?,?) ON CONFLICT(signature) DO UPDATE SET summary_json=excluded.summary_json', (signature, goal, canonical(summary)))
        return created

    def process(self, goal_id, *, now, policy):
        report = {'processed': 0, 'created': 0, 'failures': 0, 'cursor': 0}
        while True:
            with closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM goal_records WHERE goal_id=?', (goal_id,)).fetchone() is None:
                    raise ValueError('goal does not exist')
                cursor = db.execute('SELECT receive_sequence,received_time FROM observation_cursors WHERE goal_id=?', (goal_id,)).fetchone()
                sequence, last_time = tuple(cursor) if cursor else (0, 0)
                row = db.execute('SELECT receive_sequence,event_id,received_at,event_json FROM raw_events WHERE goal_id=? AND receive_sequence>? ORDER BY receive_sequence LIMIT 1', (goal_id, sequence)).fetchone()
                pendings = {r['scope_key']: json.loads(r['pending_json']) for r in db.execute('SELECT * FROM observation_pending WHERE goal_id=?', (goal_id,))}
                snapshot = lambda goal, version: self._snapshot(db, goal, version)
                if row is None:
                    pendings, observations = expire(pendings, max(now, last_time), snapshot)
                    report['created'] += self._persist(db, goal_id, pendings, observations)
                    report['cursor'] = sequence
                    return report
                comparison = None
                event = None
                received = last_time
                try:
                    received = epoch(row['received_at'])
                    if received < last_time:
                        raise OrganizationInputError('RECEIPT_TIME_REGRESSED')
                    event = json.loads(row['event_json'])
                    if not isinstance(event, dict):
                        raise OrganizationInputError('EVENT_SCHEMA_INVALID')
                    if event.get('eventName') not in KINDS:
                        raise OrganizationInputError('EVENT_KIND_UNSUPPORTED')
                    try:
                        event = read_event(event)
                    except Exception:
                        raise OrganizationInputError('EVENT_SCHEMA_INVALID') from None
                    if event['eventId'] != row['event_id']:
                        raise OrganizationInputError('EVENT_ID_MISMATCH')
                    scope = self._context(db, event, goal_id)
                    comparison = self._public_comparison(event)
                except (ValueError, KeyError, TypeError) as error:
                    reason = str(error) if isinstance(error, OrganizationInputError) else 'EVENT_PARSE_INVALID'
                    db.execute('INSERT OR IGNORE INTO observation_failures VALUES(?,?,?,?)',
                        (goal_id, row['receive_sequence'], row['event_id'], reason))
                    received = max(last_time, received)
                    report['failures'] += 1
                    event = None
                pendings, observations = expire(pendings, received, snapshot)
                if event is not None:
                    pendings, additions = organize(pendings, event, scope, received, policy, snapshot)
                    observations.extend(additions)
                report['created'] += self._persist(db, goal_id, pendings, observations, comparison)
                db.execute('INSERT INTO observation_cursors VALUES(?,?,?) ON CONFLICT(goal_id) DO UPDATE SET receive_sequence=excluded.receive_sequence,received_time=excluded.received_time',
                    (goal_id, row['receive_sequence'], received))
                report['processed'] += 1

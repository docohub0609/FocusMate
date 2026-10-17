"""SQLite receipts plus strict joins to saved business records.

The supplied extension resolver is a trusted application dependency, not an event
field. Future dialogue sources must install one; an arbitrary reference fails shut.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..contracts.raw_events import EventRejected, business_content, read_event, validate_event


def event_from_record(name, record):
    fields = {
        'codeSnapshotCreated': ('api', 'codeSnapshotId', ('codeVersion',)),
        'submissionRequested': ('api', 'submissionId', ('submissionId', 'codeVersion', 'problemRef')),
        'ojResultProduced': ('ojWorker', 'ojResultId', ('submissionId', 'ojResultId', 'verdict', 'codeVersion', 'problemRef')),
        'publicRunFinished': ('toolExecutor', 'runId', ('runId', 'loopId', 'codeVersion', 'problemRef', 'status', 'sampleStatus')),
        'runRequested': ('api', 'runId', ('runId', 'codeVersion', 'problemRef', 'runMode', 'inputRef')),
        'runFinished': ('ojWorker', 'runId', ('runId', 'codeVersion', 'problemRef', 'runMode', 'status')),
    }
    source, key, keys = fields[name]
    payload = {k: deepcopy(record[k]) for k in keys if k in record}
    if name == 'codeSnapshotCreated':
        payload.update(artifactRef=record[key], problemRef=deepcopy(record['problemRef']))
    return {'eventId': 'event-' + uuid4().hex, 'schemaVersion': '2.0.0',
        'eventVersion': 2, 'createdAt': record['createdAt'], 'goalContext': deepcopy(record['goalContext']),
        'eventName': name, 'source': source, 'actor': 'system' if source != 'api' else 'user',
        'idempotencyKey': record[key], 'privacyLevel': 'taskContent', 'payload': payload}


class SQLiteEventStore:
    def __init__(self, path, *, reference_resolver=None, clock=None, initialize=True):
        self.path = Path(path)
        self.reference_resolver = reference_resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        if initialize:
            with closing(sqlite3.connect(self.path)) as db, db:
                self.initialize(db)

    @staticmethod
    def initialize(db):
        db.execute('''CREATE TABLE IF NOT EXISTS raw_events (
            receive_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE, goal_id TEXT NOT NULL,
            source TEXT NOT NULL, event_name TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            received_at TEXT NOT NULL, business_json TEXT NOT NULL, event_json TEXT NOT NULL,
            UNIQUE(goal_id,source,event_name,idempotency_key))''')

    def receive(self, event, *, trusted_source, trusted_actor=None):
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                return self.append_on_connection(db, event, trusted_source=trusted_source, trusted_actor=trusted_actor)
        except EventRejected as error:
            return {'status': 'rejected', 'reason': str(error)}
        except sqlite3.Error:
            return {'status': 'rejected', 'reason': 'EVENT_STORAGE_UNAVAILABLE'}

    def append_on_connection(self, db, event, *, trusted_source, trusted_actor=None):
        """Participate in the caller's transaction; do not commit or swallow failure."""
        validate_event(event, trusted_source, trusted_actor)
        self._references(db, event)
        key = (event['goalContext']['goalId'], event['source'], event['eventName'], event['idempotencyKey'])
        canonical = business_content(event)
        row = db.execute('SELECT receive_sequence,received_at,event_json,business_json FROM raw_events '
                         'WHERE goal_id=? AND source=? AND event_name=? AND idempotency_key=?', key).fetchone()
        if row:
            if row[3] != canonical:
                raise EventRejected('EVENT_IDEMPOTENCY_CONFLICT')
            return self._receipt(row, 'duplicate')
        received = self.clock()
        try:
            cursor = db.execute('INSERT INTO raw_events(event_id,goal_id,source,event_name,idempotency_key,received_at,business_json,event_json) VALUES(?,?,?,?,?,?,?,?)',
                (event['eventId'], *key, received, canonical, json.dumps(event, ensure_ascii=False)))
        except sqlite3.IntegrityError:
            raise EventRejected('EVENT_ID_CONFLICT') from None
        return {'status': 'recorded', 'receiveSequence': cursor.lastrowid, 'receivedAt': received, 'event': deepcopy(event)}

    @staticmethod
    def _receipt(row, status='recorded'):
        return {'status': status, 'receiveSequence': row[0], 'receivedAt': row[1], 'event': read_event(json.loads(row[2]))}

    def list_goal(self, goal_id, *, after_sequence=0, limit=100):
        if not isinstance(goal_id, str) or not goal_id or type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid event page')
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT receive_sequence,received_at,event_json FROM raw_events '
                'WHERE goal_id=? AND receive_sequence>? ORDER BY receive_sequence LIMIT ?', (goal_id, after_sequence, limit)).fetchall()
        return [self._receipt(row) for row in rows]

    def _references(self, db, event):
        goal = event['goalContext']; payload = event['payload']; name = event['eventName']
        row = db.execute('SELECT problem_id,problem_version FROM goal_records WHERE goal_id=?', (goal['goalId'],)).fetchone()
        if row is None:
            raise EventRejected('EVENT_CONTEXT_INVALID')
        problem = {'problemId': row[0], 'problemVersion': row[1]}
        if 'problemRef' in payload and payload['problemRef'] != problem:
            raise EventRejected('EVENT_CONTEXT_INVALID')
        if goal['scope'] == 'subgoal':
            try:
                contract = db.execute("SELECT compilation_json FROM goal_contract_versions WHERE goal_id=? AND version=? AND status IN ('frozen','superseded')",
                    (goal['goalId'], event['contractVersion'])).fetchone()
            except sqlite3.OperationalError:
                contract = None
            if contract is None or goal['subgoalId'] not in {s['subgoalId'] for s in json.loads(contract[0])['contract']['subgoals']}:
                raise EventRejected('EVENT_CONTEXT_INVALID')
        if 'codeVersion' in payload:
            snapshot = db.execute('SELECT code_snapshot_id FROM code_snapshots WHERE goal_id=? AND code_version=?',
                (goal['goalId'], payload['codeVersion'])).fetchone()
            if snapshot is None or (name == 'codeSnapshotCreated' and snapshot[0] != payload['artifactRef']):
                raise EventRejected('EVENT_REFERENCE_INVALID')
        if name in ('submissionRequested', 'ojResultProduced'):
            submission = db.execute('SELECT goal_id,code_version,problem_id,problem_version FROM submissions WHERE submission_id=?', (payload['submissionId'],)).fetchone()
            if submission is None or tuple(submission) != (goal['goalId'], payload['codeVersion'], problem['problemId'], problem['problemVersion']):
                raise EventRejected('EVENT_REFERENCE_INVALID')
        if name == 'ojResultProduced':
            result = db.execute('SELECT submission_id,goal_id,code_version,verdict FROM oj_results WHERE oj_result_id=?', (payload['ojResultId'],)).fetchone()
            if result is None or tuple(result) != (payload['submissionId'], goal['goalId'], payload['codeVersion'], payload['verdict']):
                raise EventRejected('EVENT_REFERENCE_INVALID')
        if name in ('helpRequested', 'actionFeedbackProvided', 'sessionPaused', 'sessionResumed', 'runRequested', 'runFinished', 'publicRunFinished'):
            if self.reference_resolver is None or self.reference_resolver(event) is not True:
                raise EventRejected('EVENT_REFERENCE_INVALID')
        if name == 'sessionResumed':
            previous = db.execute('SELECT event_json FROM raw_events WHERE event_id=? AND goal_id=? AND event_name=?',
                (payload['previousPauseEventId'], goal['goalId'], 'sessionPaused')).fetchone()
            if previous is None or json.loads(previous[0]).get('sessionRef') != event['sessionRef']:
                raise EventRejected('EVENT_REFERENCE_INVALID')

"""A recoverable editor draft is not the immutable code used by a running task."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from .problem_storage import ProblemRepository
from .submission_storage import SubmissionRepository, SubmissionFailure, timestamp, utc_now
from .schema_validation import validate
from src.focusmate.adapters.raw_events import SQLiteEventStore, event_from_record


class EditorRepository:
    def __init__(self, path):
        self.path = path
        self.submissions = SubmissionRepository(path)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS editor_workspaces (
                    problem_id TEXT NOT NULL, problem_version INTEGER NOT NULL,
                    goal_id TEXT NOT NULL UNIQUE REFERENCES goal_records(goal_id),
                    PRIMARY KEY(problem_id,problem_version));
                CREATE TABLE IF NOT EXISTS code_drafts (
                    goal_id TEXT PRIMARY KEY REFERENCES goal_records(goal_id),
                    revision INTEGER NOT NULL, source_code TEXT NOT NULL,
                    code_version INTEGER, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS code_draft_operations (
                    goal_id TEXT NOT NULL REFERENCES goal_records(goal_id),
                    operation_id TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY(goal_id,operation_id));
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def workspace(self, problem_id, version, *, create=False):
        problem = ProblemRepository(self.path).get(problem_id, version)
        if problem['lifecycle']['status'] != 'frozen':
            raise SubmissionFailure('PROBLEM_VERSION_NOT_FROZEN', '请先确认题目条件。', 409)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE' if create else 'BEGIN')
            row = db.execute('SELECT goal_id FROM editor_workspaces WHERE problem_id=? AND problem_version=?', (problem_id, version)).fetchone()
            if row is None:
                if not create:
                    raise SubmissionFailure('EDITOR_WORKSPACE_NOT_FOUND', '尚未创建编程工作区。', 404)
                goal_id, now = 'goal-' + uuid4().hex, timestamp(utc_now())
                db.execute('INSERT INTO goal_records VALUES(?,?,?,?,?,?)', (goal_id, '1.0.0', now, problem_id, version, 'active'))
                db.execute('INSERT INTO editor_workspaces VALUES(?,?,?)', (problem_id, version, goal_id))
                db.execute('INSERT INTO code_drafts VALUES(?,0,\'\',NULL,?)', (goal_id, now))
            else:
                goal_id = row['goal_id']
        return {'goal': self.submissions.get_goal(goal_id), 'draft': self.draft(goal_id), 'problem': problem}

    @staticmethod
    def _draft(row):
        return {'goalContext': {'goalId': row['goal_id'], 'scope': 'goal'}, 'language': 'python3',
                'revision': row['revision'], 'sourceCode': row['source_code'], 'codeVersion': row['code_version'],
                'updatedAt': row['updated_at'], 'contentHash': sha256(row['source_code'].encode()).hexdigest()}

    def draft(self, goal_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM code_drafts WHERE goal_id=?', (goal_id,)).fetchone()
        if row is None:
            raise SubmissionFailure('EDITOR_WORKSPACE_NOT_FOUND', '编程工作区不存在。', 404)
        return self._draft(row)

    def save_draft(self, goal_id, request):
        if (not isinstance(request, dict) or set(request) != {'sourceCode', 'expectedRevision', 'operationId'}
                or not isinstance(request['sourceCode'], str) or type(request['expectedRevision']) is not int
                or request['expectedRevision'] < 0 or not isinstance(request['operationId'], str)
                or not 1 <= len(request['operationId']) <= 256):
            raise SubmissionFailure('INVALID_CODE_DRAFT', '代码、修改序号或保存编号无效。', 422)
        goal = self.submissions.get_goal(goal_id)
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=False)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT request_json,result_json FROM code_draft_operations WHERE goal_id=? AND operation_id=?', (goal_id, request['operationId'])).fetchone()
            if old:
                if old['request_json'] != canonical:
                    raise SubmissionFailure('IDEMPOTENCY_CONFLICT', '同一保存编号的内容不能改变。', 409)
                return json.loads(old['result_json'])
            row = db.execute('SELECT * FROM code_drafts WHERE goal_id=?', (goal_id,)).fetchone()
            if row is None:
                raise SubmissionFailure('EDITOR_WORKSPACE_NOT_FOUND', '编程工作区不存在。', 404)
            if row['revision'] != request['expectedRevision']:
                raise SubmissionFailure('CODE_DRAFT_CONFLICT', '另一个页面已保存修改，请核对后台版本。', 409)
            source = request['sourceCode']
            if source != row['source_code']:
                now, code_version = timestamp(utc_now()), None
                if source.strip():
                    code_version = db.execute('SELECT COALESCE(MAX(code_version),0)+1 FROM code_snapshots WHERE goal_id=?', (goal_id,)).fetchone()[0]
                    snapshot = {'codeSnapshotId': 'snapshot-' + uuid4().hex, 'schemaVersion': '1.0.0', 'createdAt': now,
                                'goalContext': {'goalId': goal_id, 'scope': 'goal'}, 'codeVersion': code_version,
                                'language': 'python3', 'sourceCode': source, 'contentHash': sha256(source.encode()).hexdigest()}
                    validate('CodeSnapshot', snapshot)
                    db.execute('INSERT INTO code_snapshots VALUES(?,?,?,?,?,?,?,?)',
                               (snapshot['codeSnapshotId'], '1.0.0', now, goal_id, code_version, 'python3', source, snapshot['contentHash']))
                    event = event_from_record('codeSnapshotCreated', dict(snapshot, problemRef=goal['problemRef']))
                    SQLiteEventStore(self.path, initialize=False).append_on_connection(db, event, trusted_source='api')
                db.execute('UPDATE code_drafts SET revision=revision+1,source_code=?,code_version=?,updated_at=? WHERE goal_id=?', (source, code_version, now, goal_id))
            result = self._draft(db.execute('SELECT * FROM code_drafts WHERE goal_id=?', (goal_id,)).fetchone())
            db.execute('INSERT INTO code_draft_operations VALUES(?,?,?,?)', (goal_id, request['operationId'], canonical, json.dumps(result, ensure_ascii=False)))
            return result

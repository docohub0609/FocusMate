"""Durable, single-process extraction jobs; no automatic replay after restart."""
from __future__ import annotations

from contextlib import closing
import json
import re
import threading
from uuid import uuid4

from .problem_intake import timestamp, utc_now
from .problem_storage import StorageFailure
from .schema_validation import validate, validation_errors


class FeatureJobs:
    def __init__(self, problems):
        self.problems = problems
        with closing(problems._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS feature_extraction_jobs (
                    job_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                    problem_id TEXT NOT NULL, version INTEGER NOT NULL,
                    status TEXT NOT NULL, owner TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    feature_set_id TEXT, failure_code TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS extraction_active ON feature_extraction_jobs(problem_id,version)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS feature_extraction_keys (
                    operation_key TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES feature_extraction_jobs(job_id));
            ''')

            if 'failure_details' not in {r[1] for r in db.execute('PRAGMA table_info(feature_extraction_jobs)')}:
                db.execute('ALTER TABLE feature_extraction_jobs ADD COLUMN failure_details TEXT')

    def enqueue(self, request, key, owner):
        if validation_errors('FeatureExtractionJobRequest', request):
            raise StorageFailure('INVALID_EXTRACTION_REQUEST', '解析请求不完整，请重新打开题目。', 422, [])
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
            raise StorageFailure('INVALID_OPERATION_KEY', '缺少有效的操作编号。', 422, [])
        fingerprint = json.dumps(request, sort_keys=True, separators=(',', ':'))
        with closing(self.problems._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM feature_extraction_keys WHERE operation_key=?', (key,)).fetchone()
            if prior:
                if prior['request_json'] != fingerprint:
                    raise StorageFailure('IDEMPOTENCY_CONFLICT', '这次操作的内容已经改变。', 409, [])
                return self._public(db.execute('SELECT * FROM feature_extraction_jobs WHERE job_id=?', (prior['job_id'],)).fetchone())
            self._check(db, request)
            active = db.execute("SELECT * FROM feature_extraction_jobs WHERE problem_id=? AND version=? AND status IN ('queued','running')",
                                (request['problemId'], request['problemVersion'])).fetchone()
            if active:
                if active['request_json'] != fingerprint:
                    raise StorageFailure('EXTRACTION_BUSY', '上一版题目的解析尚未结束，请稍后再试。', 409, [])
                job_id = active['job_id']
            else:
                if db.execute("SELECT COUNT(*) FROM feature_extraction_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                    raise StorageFailure('EXTRACTION_BUSY', '当前解析任务较多，请稍后再试。', 429, [])
                job_id, now = 'extraction-' + str(uuid4()), timestamp(utc_now())
                db.execute('INSERT INTO feature_extraction_jobs (job_id,request_json,problem_id,version,status,owner,created_at,updated_at,feature_set_id,failure_code) VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
                           (job_id, fingerprint, request['problemId'], request['problemVersion'], 'queued', owner, now, now))
            db.execute('INSERT INTO feature_extraction_keys VALUES(?,?,?)', (key, fingerprint, job_id))
            return self._public(db.execute('SELECT * FROM feature_extraction_jobs WHERE job_id=?', (job_id,)).fetchone())

    def _check(self, db, request):
        row = db.execute('SELECT * FROM problem_versions WHERE problem_id=? AND version=?',
                         (request['problemId'], request['problemVersion'])).fetchone()
        self.problems._require_draft(row, request['expectedDraftRevision'])
        if row['content_hash'] != request['contentHash']:
            raise StorageFailure('FEATURE_STALE', '题目内容已经变化，请重新解析。', 409, [])
        return row

    def _public(self, row):
        if row is None:
            raise StorageFailure('EXTRACTION_NOT_FOUND', '尚未开始解析。', 404, [])
        result = {'jobId': row['job_id'], 'request': json.loads(row['request_json']),
                  'status': row['status'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}
        if row['feature_set_id']:
            result['featureSetId'] = row['feature_set_id']
        if row['failure_code']:
            result['failureCode'] = row['failure_code']
            if row['failure_details']:
                result['failureDetails'] = json.loads(row['failure_details'])
        validate('FeatureExtractionJob', result)
        return result

    def get(self, job_id):
        with closing(self.problems._connect()) as db:
            return self._public(db.execute('SELECT * FROM feature_extraction_jobs WHERE job_id=?', (job_id,)).fetchone())

    def current(self, problem_id, version):
        self.problems.get(problem_id, version)
        with closing(self.problems._connect()) as db:
            row = db.execute('SELECT * FROM feature_extraction_jobs WHERE problem_id=? AND version=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                             (problem_id, version)).fetchone()
            return self._public(row) if row else None

    def recover(self, owner):
        with closing(self.problems._connect()) as db, db:
            db.execute("UPDATE feature_extraction_jobs SET status='interrupted',failure_code='EXTRACTION_INTERRUPTED',updated_at=? WHERE status IN ('queued','running') AND owner<>?",
                       (timestamp(utc_now()), owner))

    def claim(self, owner):
        with closing(self.problems._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM feature_extraction_jobs WHERE status='queued' AND owner=? ORDER BY created_at,rowid LIMIT 1", (owner,)).fetchone()
            if row is None:
                return None
            request = json.loads(row['request_json'])
            try:
                version = self._check(db, request)
            except StorageFailure:
                db.execute("UPDATE feature_extraction_jobs SET status='stale',failure_code='FEATURE_STALE',updated_at=? WHERE job_id=?", (timestamp(utc_now()), row['job_id']))
                return None
            db.execute("UPDATE feature_extraction_jobs SET status='running',updated_at=? WHERE job_id=?", (timestamp(utc_now()), row['job_id']))
            return {'jobId': row['job_id'], 'owner': owner, 'request': request,
                    'problemPackage': self.problems._record(db, version)['problemPackage']}

    def complete(self, claim, result):
        validate('ProblemFeatures', result)
        with closing(self.problems._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM feature_extraction_jobs WHERE job_id=? AND owner=? AND status='running'", (claim['jobId'], claim['owner'])).fetchone()
            if row is None:
                return
            request = json.loads(row['request_json'])
            if result['problemRef'] != {'problemId': request['problemId'], 'problemVersion': request['problemVersion']} or result['problemContentHash'] != request['contentHash']:
                raise StorageFailure('FEATURE_CONTEXT_MISMATCH', '解析结果与题目不对应。', 409, [])
            try:
                self._check(db, request)
            except StorageFailure:
                db.execute("UPDATE feature_extraction_jobs SET status='stale',failure_code='FEATURE_STALE',updated_at=? WHERE job_id=?", (timestamp(utc_now()), row['job_id']))
                return
            self.problems._save_feature_set(db, result, request['expectedDraftRevision'])
            db.execute("UPDATE feature_extraction_jobs SET status='succeeded',feature_set_id=?,updated_at=? WHERE job_id=?",
                       (result['featureSetId'], timestamp(utc_now()), row['job_id']))

    def fail(self, claim, code, details=None):
        with closing(self.problems._connect()) as db, db:
            db.execute("UPDATE feature_extraction_jobs SET status='failed',failure_code=?,failure_details=?,updated_at=? WHERE job_id=? AND owner=? AND status='running'",
                       (code, json.dumps(details or [], ensure_ascii=False), timestamp(utc_now()), claim['jobId'], claim['owner']))


class ExtractionRuntime:
    """One API owner per database; OS lock is released even after process death."""
    def __init__(self, jobs, provider_factory):
        self.jobs, self.provider_factory = jobs, provider_factory
        self.owner = str(uuid4())
        self.stop = threading.Event()
        self.lock = open(str(jobs.problems.path) + '.extraction.lock', 'a+b')
        self.lock.seek(0, 2)
        if self.lock.tell() == 0:
            self.lock.write(b'0')
            self.lock.flush()
        self.lock.seek(0)
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('This database already has an API extraction owner.') from None
        jobs.recover(self.owner)
        self.thread = threading.Thread(target=self._run, daemon=True, name='problem-extraction')
        self.thread.start()

    def _run(self):
        from .problem_features import extract_problem_features
        try:
            while not self.stop.is_set():
                try:
                    claim = self.jobs.claim(self.owner)
                    if claim:
                        try:
                            result = extract_problem_features({'problemPackage': claim['problemPackage']}, self.provider_factory())
                            self.jobs.complete(claim, result)
                        except Exception as error:
                            code = getattr(error, 'code', 'FEATURE_EXTRACTION_INTERNAL_ERROR')
                            details = []
                            for item in getattr(error, 'field_errors', [])[:10]:
                                # Never send raw provider responses, schema values or logs to the browser.
                                field = str(item.get('field', ''))
                                if not re.fullmatch(r'[A-Za-z0-9_.]{1,160}', field):
                                    field = 'body'
                                issue = item.get('code')
                                message = {'incompleteLabels': '这项条件的中文显示名称不完整。',
                                           'invalidValue': '这项条件的取值或分类不符合要求。',
                                           'invalidStructure': '这项条件的字段格式不符合要求。'}.get(issue, '这项条件的内容或原文引用未通过检查。')
                                details.append({'field': field, 'message': message})
                            self.jobs.fail(claim, code, details)
                    else:
                        self.stop.wait(0.1)
                except Exception:
                    # A database outage must not advance or fabricate a task result.
                    self.stop.wait(0.5)
        finally:
            self.lock.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)

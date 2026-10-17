"""Authoritative final-evidence reads and atomic storage, independent of Core."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from focusmate.contracts.final_oj import FinalOJError, FINAL_STATUSES, encode, digest
from focusmate.contracts.schema_validation import validate
from services.oj_worker.judge import OJResultRepository, JudgeFailure
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider, COMPARISON_POLICY


class SQLiteFinalOJStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file(): raise FinalOJError('BUSINESS_DATABASE_REQUIRED')
        with closing(self._connect()) as db, db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'goal_records','code_snapshots','submissions','goal_contract_versions'} <= tables:
                raise FinalOJError('BUSINESS_TABLES_REQUIRED')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS goal_states(goal_id TEXT PRIMARY KEY, state_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS goal_state_history(goal_id TEXT NOT NULL, version INTEGER NOT NULL,
                    request_id TEXT NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY(goal_id,version));
                CREATE TABLE IF NOT EXISTS final_verifier_results(result_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, result_json TEXT NOT NULL, UNIQUE(goal_id,request_id));
                CREATE TABLE IF NOT EXISTS final_oj_requests(goal_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_json TEXT NOT NULL, owner_json TEXT NOT NULL, pinned_json TEXT NOT NULL,
                    report_json TEXT NOT NULL, PRIMARY KEY(goal_id,request_id));
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def goal_state(self, goal_id):
        with closing(self._connect()) as db:
            row = db.execute('SELECT state_json FROM goal_states WHERE goal_id=?',(goal_id,)).fetchone()
            if row:
                state = json.loads(row[0]); validate('GoalState', state)
                return state
        return None

    def report(self, goal_id, request_id):
        with closing(self._connect()) as db:
            row = db.execute('SELECT report_json FROM final_oj_requests WHERE goal_id=? AND request_id=?',
                             (goal_id, request_id)).fetchone()
            return json.loads(row[0]) if row else None

    def bound_report(self, request, owner):
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM final_oj_requests WHERE goal_id=? AND request_id=?',
                             (request['goalId'],request['requestId'])).fetchone()
            if row:
                if row['request_json'] != encode(request) or row['owner_json'] != encode(owner):
                    raise FinalOJError('REQUEST_CONFLICT')
                return json.loads(row['report_json'])
        return None

    def transact(self, request, owner, decide, *, deadline=None):
        key = request['goalId'], request['requestId']
        try:
            with closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT * FROM final_oj_requests WHERE goal_id=? AND request_id=?',key).fetchone()
                if old:
                    if old['request_json'] != encode(request) or old['owner_json'] != encode(owner):
                        raise FinalOJError('REQUEST_CONFLICT')
                    previous = json.loads(old['report_json'])
                    if previous['status'] in FINAL_STATUSES: return previous
                row = db.execute('SELECT state_json FROM goal_states WHERE goal_id=?',(key[0],)).fetchone()
                state = json.loads(row[0]) if row else None
                if state: validate('GoalState', state)
                pinned = json.loads(old['pinned_json']) if old else {}
                try:
                    bundle = self._bundle(db, request, pinned, state, owner)
                except FinalOJError as exc:
                    bundle = {'error': str(exc), 'refs': pinned}
                except (ValueError, KeyError, TypeError):
                    bundle = {'error': 'SOURCE_RECORD_INVALID', 'refs': pinned}
                report, new_state = decide(request, bundle, state)
                validate('FinalOJReport', report)
                if deadline is not None and time.time() >= deadline:
                    raise FinalOJError('DEADLINE_EXCEEDED')
                if report.get('verifierResult'):
                    result = report['verifierResult']
                    db.execute('INSERT INTO final_verifier_results VALUES(?,?,?,?)',
                        (result['verifierResultId'],key[0],key[1],encode(result)))
                if new_state:
                    validate('GoalState', new_state)
                    db.execute('INSERT INTO goal_states VALUES(?,?) ON CONFLICT(goal_id) DO UPDATE SET state_json=excluded.state_json',
                               (key[0],encode(new_state)))
                    db.execute('INSERT INTO goal_state_history VALUES(?,?,?,?)',
                               (key[0],new_state['version'],key[1],encode(new_state)))
                db.execute('INSERT INTO final_oj_requests VALUES(?,?,?,?,?,?) ON CONFLICT(goal_id,request_id) '
                           'DO UPDATE SET pinned_json=excluded.pinned_json, report_json=excluded.report_json',
                    (*key,encode(request),encode(owner),encode(bundle.get('refs', pinned)),encode(report)))
                return report
        except sqlite3.Error:
            raise FinalOJError('FINAL_STORAGE_UNAVAILABLE') from None

    def _bundle(self, db, req, pinned, state, owner):
        goal = db.execute('SELECT * FROM goal_records WHERE goal_id=?',(req['goalId'],)).fetchone()
        sub = db.execute('SELECT * FROM submissions WHERE submission_id=? AND goal_id=?',
                         (req['submissionId'],req['goalId'])).fetchone()
        if goal is None or sub is None: raise FinalOJError('SOURCE_UNAVAILABLE')
        if ('codeVersion' in owner and owner['codeVersion'] != sub['code_version'] or
            'problemRef' in owner and owner['problemRef'] != {'problemId':goal['problem_id'],'problemVersion':goal['problem_version']}):
            raise FinalOJError('CALLER_CONTEXT_MISMATCH')
        if (sub['problem_id'],sub['problem_version']) != (goal['problem_id'],goal['problem_version']):
            raise FinalOJError('SUBMISSION_REFERENCE_MISMATCH')
        if state and state['status'] == 'completed':
            return {'refs': pinned}
        row = db.execute("SELECT * FROM goal_contract_versions WHERE goal_id=? AND status='frozen'",(req['goalId'],)).fetchone()
        if row is None or row['version'] != req['goalContractVersion']:
            raise FinalOJError('CONTRACT_VERSION_CHANGED')
        try:
            contract = json.loads(row['compilation_json'])['contract']
            validate('GoalContract', contract)
            if (contract['goalId'] != req['goalId'] or contract['version'] != row['version'] or
                contract['problemRef'] != {'problemId':goal['problem_id'],'problemVersion':goal['problem_version']} or
                contract['contractStatus'] != 'frozen' or contract['finalCriterion'] != {'verifierType':'finalOj','requiredVerdict':'AC'}):
                raise ValueError()
        except (ValueError, KeyError, TypeError): raise FinalOJError('CONTRACT_INVALID') from None
        if contract.get('schemaVersion') == '2.0.0':
            problem = db.execute('SELECT content_hash,lifecycle_status FROM problem_versions WHERE problem_id=? AND version=?',
                (goal['problem_id'],goal['problem_version'])).fetchone()
            if problem is None or problem['lifecycle_status'] != 'frozen' or problem['content_hash'] != contract['problemContentHash']:
                raise FinalOJError('PROBLEM_VERSION_CHANGED')
        latest = db.execute('SELECT MAX(code_version) FROM code_snapshots WHERE goal_id=?',(req['goalId'],)).fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_drafts'").fetchone():
            draft=db.execute('SELECT code_version FROM code_drafts WHERE goal_id=?',(req['goalId'],)).fetchone()
            if draft is not None:latest=draft[0]
        if latest != sub['code_version']: raise FinalOJError('CODE_VERSION_CHANGED')
        code = db.execute('SELECT * FROM code_snapshots WHERE goal_id=? AND code_version=?',(req['goalId'],sub['code_version'])).fetchone()
        if code is None: raise FinalOJError('SOURCE_UNAVAILABLE')
        if hashlib.sha256(code['source_code'].encode()).hexdigest() != code['content_hash']:
            raise FinalOJError('CODE_HASH_MISMATCH')
        if code['language'] != sub['language']: raise FinalOJError('SUBMISSION_REFERENCE_MISMATCH')
        refs = {'goalContractVersion':row['version'],'contractHash':row['content_hash'],
                'contractContentHash':digest(contract),'problemId':goal['problem_id'],'problemVersion':goal['problem_version'],
                'submissionId':sub['submission_id'],'codeVersion':code['code_version'],
                'codeSnapshotId':code['code_snapshot_id'],'codeHash':code['content_hash']}
        if any(refs.get(k) != v for k,v in pinned.items() if k in refs):
            raise FinalOJError('PINNED_MATERIAL_CHANGED')
        result_row = db.execute('SELECT * FROM oj_results WHERE submission_id=?',(sub['submission_id'],)).fetchone()
        bundle = {'contract':contract,'submissionStatus':sub['status'],'refs':refs}
        if sub['status'] != 'finished':
            if result_row: raise FinalOJError('RESULT_INCONSISTENT')
            return bundle
        if result_row is None: raise FinalOJError('RESULT_MISSING')
        try:
            result = OJResultRepository._result(result_row)
            validate('OJResult', result)
        except Exception: raise FinalOJError('RESULT_INCONSISTENT') from None
        if (result['goalContext'] != {'goalId':req['goalId'],'scope':'goal'} or result['codeVersion'] != sub['code_version'] or
            result['problemRef'] != {'problemId':goal['problem_id'],'problemVersion':goal['problem_version']}):
            raise FinalOJError('RESULT_REFERENCE_MISMATCH')
        receipt = db.execute("SELECT event_json FROM raw_events WHERE goal_id=? AND source='ojWorker' AND event_name='ojResultProduced' AND idempotency_key=?",
                             (req['goalId'],result['ojResultId'])).fetchone()
        if not receipt: raise FinalOJError('RESULT_RECEIPT_MISSING')
        event = json.loads(receipt[0])
        expected = {k:result[k] for k in ('submissionId','ojResultId','verdict','codeVersion','problemRef')}
        if event['payload'] != expected or event['goalContext'] != result['goalContext']:
            raise FinalOJError('RESULT_REFERENCE_MISMATCH')
        try:
            trusted = SQLiteTrustedAssetProvider(self.path).get_on_connection(db,goal['problem_id'],goal['problem_version'])
        except JudgeFailure: raise FinalOJError('UNTRUSTED_ASSETS') from None
        if trusted.assurance != 'full' or result.get('assurance', 'full') != 'full': raise FinalOJError('UNTRUSTED_ASSETS')
        if result['verifierAssetRef'] != {'assetId':trusted.asset_id,'assetVersion':trusted.asset_version,'status':'ready'}:
            raise FinalOJError('RESULT_REFERENCE_MISMATCH')
        summary = result['testSummary']
        if result['comparisonPolicy'] != COMPARISON_POLICY or summary['total'] != len(trusted.cases) or summary['passed'] > summary['total']:
            raise FinalOJError('RESULT_INCONSISTENT')
        if result['verdict'] == 'AC' and (summary['passed'] != summary['total'] or summary['firstFailedCase'] is not None or result['stderrCategory'] is not None):
            raise FinalOJError('RESULT_INCONSISTENT')
        if result['verdict'] != 'AC' and summary['passed'] == summary['total']:
            raise FinalOJError('RESULT_INCONSISTENT')
        asset = db.execute('SELECT material_hash FROM trusted_test_materials WHERE asset_id=? AND asset_version=?',(trusted.asset_id,trusted.asset_version)).fetchone()
        refs.update(ojResultId=result['ojResultId'],assetId=trusted.asset_id,assetVersion=trusted.asset_version,materialHash=asset[0])
        bundle['result'] = result
        return bundle

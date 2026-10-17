"""Read authorized business snapshots without initializing business tables."""
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3

class SQLiteToolBackend:
    def __init__(self, database_path, public_service=None, local_service=None):
        self.path=Path(database_path).resolve();self.public_service=public_service;self.local_service=local_service
    def _connect(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        return db
    @staticmethod
    def _goal(db,ctx):
        row=db.execute('SELECT problem_id,problem_version FROM goal_records WHERE goal_id=?',(ctx['goalContext']['goalId'],)).fetchone()
        if not row or (row['problem_id'],row['problem_version'])!=(ctx['problemRef']['problemId'],ctx['problemRef']['problemVersion']):
            raise ValueError('NOT_FOUND_OR_FORBIDDEN')
    def load_code(self,ctx,version):
        with closing(self._connect()) as db:
            self._goal(db,ctx)
            row=db.execute('SELECT code_snapshot_id,code_version,source_code,content_hash FROM code_snapshots WHERE goal_id=? AND code_version=?',
                (ctx['goalContext']['goalId'],version)).fetchone()
            if not row: raise ValueError('NOT_FOUND_OR_FORBIDDEN')
            if hashlib.sha256(row['source_code'].encode()).hexdigest()!=row['content_hash']:raise ValueError('SOURCE_HASH_MISMATCH')
            return {'codeSnapshotId':row['code_snapshot_id'],'codeVersion':row['code_version'],'sourceCode':row['source_code'],'contentHash':row['content_hash']}
    def load_submission(self,ctx,identity):
        with closing(self._connect()) as db:
            self._goal(db,ctx)
            row=db.execute('SELECT submission_id,goal_id,problem_id,problem_version,code_version,status FROM submissions WHERE submission_id=? AND goal_id=?',
                (identity,ctx['goalContext']['goalId'])).fetchone()
            if not row or (row['problem_id'],row['problem_version'])!=(ctx['problemRef']['problemId'],ctx['problemRef']['problemVersion']):raise ValueError('NOT_FOUND_OR_FORBIDDEN')
            result={'submissionId':identity,'codeVersion':row['code_version'],'problemRef':ctx['problemRef'],'status':row['status']}
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oj_results'").fetchone()
            if exists:
                outcome=db.execute('SELECT oj_result_id,goal_id,problem_id,problem_version,code_version,verdict FROM oj_results WHERE submission_id=?',(identity,)).fetchone()
                if outcome:
                    if row['status']!='finished' or (outcome['goal_id'],outcome['problem_id'],outcome['problem_version'],outcome['code_version'])!=(row['goal_id'],row['problem_id'],row['problem_version'],row['code_version']):raise ValueError('RESULT_ASSOCIATION_INVALID')
                    if outcome['verdict'] not in ('AC','WA','TLE','RE','CE'):raise ValueError('INVALID_VERDICT')
                    result.update(resultRef=outcome['oj_result_id'],verdict=outcome['verdict'])
            return result
    def run_public(self,ctx,code):
        if self.public_service is None:raise ValueError('PUBLIC_RUN_UNAVAILABLE')
        return self.public_service.run(ctx,code)
    def run_local(self,ctx,code,args):
        if self.local_service is None:raise ValueError('LOCAL_RUN_UNAVAILABLE')
        return self.local_service.run(ctx,code,args)

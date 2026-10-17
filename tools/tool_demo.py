"""Local developer entrypoints. Fixture authorization is never production login."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import sqlite3
from uuid import uuid4
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.tool_gateway import ToolGateway
from focusmate.core.tool_dialogue import run_tool_dialogue
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
from focusmate.adapters.tool_storage import SQLiteToolBackend
from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.local_runs import LocalRunService


def business_snapshot(path):
    """Public runs may append event receipts, but not change learning/OJ records."""
    with sqlite3.connect(path) as db:
        return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                for table in ('goal_records', 'code_snapshots', 'submissions', 'oj_results', 'public_test_cases')}


def provision_fixture(path, value):
    """Simulated business records, actual saved sources; no production store writes."""
    ctx=value['context']; fixtures=value['fixtures'];goal=ctx['goalContext']['goalId'];problem=ctx['problemRef']
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE goal_records(goal_id TEXT PRIMARY KEY,problem_id TEXT,problem_version INTEGER);
        CREATE TABLE code_snapshots(code_snapshot_id TEXT PRIMARY KEY,goal_id TEXT,code_version INTEGER,source_code TEXT,content_hash TEXT);
        CREATE TABLE submissions(submission_id TEXT PRIMARY KEY,goal_id TEXT,problem_id TEXT,problem_version INTEGER,code_version INTEGER,status TEXT);
        CREATE TABLE oj_results(oj_result_id TEXT PRIMARY KEY,submission_id TEXT,goal_id TEXT,problem_id TEXT,problem_version INTEGER,code_version INTEGER,verdict TEXT,hidden_payload TEXT);
        CREATE TABLE public_test_cases(test_case_id TEXT PRIMARY KEY,asset_id TEXT,asset_version INTEGER,problem_id TEXT,problem_version INTEGER,example_index INTEGER,stdin_text TEXT,expected_output_text TEXT);""")
        db.execute('INSERT INTO goal_records VALUES(?,?,?)',(goal,problem['problemId'],problem['problemVersion']))
        for c in fixtures['codes']:
            db.execute('INSERT INTO code_snapshots VALUES(?,?,?,?,?)',('snapshot-'+str(c['version']),goal,c['version'],c['source'],hashlib.sha256(c['source'].encode()).hexdigest()))
        for s in fixtures.get('submissions',[]):
            db.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?)',(s['submissionId'],goal,problem['problemId'],problem['problemVersion'],s['codeVersion'],s['status']))
            if 'verdict' in s:
                db.execute('INSERT INTO oj_results VALUES(?,?,?,?,?,?,?,?)',('result-'+s['submissionId'],s['submissionId'],goal,problem['problemId'],problem['problemVersion'],s['codeVersion'],s['verdict'],'PRIVATE_SENTINEL_DO_NOT_EXPOSE'))
        for i,s in enumerate(fixtures.get('publicSamples',[])):
            db.execute('INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?)',('example-'+str(i+1),'asset-1',1,problem['problemId'],problem['problemVersion'],i+1,s['input'],s['expectedOutput']))


def create_fixture_runner(provider, rootdir=None):
    def run(value, directory):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        ctx=deepcopy(value['context']);ctx['loopId']='loop-'+uuid4().hex
        ctx['deadlineAt']=(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()
        db=directory/'fixture.sqlite3';provision_fixture(db,value)
        service=PublicRunService(db,directory/'work')
        local_service=LocalRunService(directory/'local-work')
        backend=SQLiteToolBackend(db,service,local_service)
        simulation=value.get('simulation')
        if simulation:
            original=backend.run_public
            def simulate(context,code):
                if simulation['kind']=='unavailable':
                    return {'status':'failed','failure':'SIMULATED_INFRASTRUCTURE_UNAVAILABLE',
                        'goalContext':context['goalContext'],'problemRef':context['problemRef'],'codeVersion':code['codeVersion'], 'simulation':True}
                raise ValueError('unsupported simulation')
            backend.run_public=simulate
        gateway=ToolGateway(backend,SQLiteToolLedger(directory/'calls.sqlite3'))
        before=business_snapshot(db)
        report=run_tool_dialogue(ctx,deepcopy(value['materials']),provider,gateway,tools=TOOL_DEFINITIONS)
        if business_snapshot(db)!=before:raise AssertionError('business fixture modified')
        report['executionSource']='simulated-fault' if simulation else 'saved-fixture-with-real-tools'
        report['authorizationSource']='developer-fixture-not-production-login'
        report['businessRecordsUnchanged']=True
        report['context']=ctx
        return report
    return run

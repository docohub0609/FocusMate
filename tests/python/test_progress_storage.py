from copy import deepcopy
import json
import sqlite3
from hashlib import sha256
import pytest
from test_progress_estimation import fixture, Provider
from focusmate.core.progress_estimation import estimate_progress
from focusmate.adapters.progress_storage import SQLiteProgressResolver


def database(tmp_path):
    path=tmp_path/'business.db';request,records=fixture()
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE goal_records(goal_id,problem_id,problem_version);
        CREATE TABLE goal_contract_versions(goal_id,version,status,compilation_json);
        CREATE TABLE code_snapshots(code_snapshot_id,goal_id,code_version,language,source_code,content_hash);''')
        db.execute('INSERT INTO goal_records VALUES(?,?,?)',('g1','p1',1))
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?)',('g1',1,'frozen',json.dumps({'contract':records[0]['content']})))
        for r in records[1:3]:
            text=r['content']['text']
            db.execute('INSERT INTO code_snapshots VALUES(?,?,?,?,?,?)',(r['ref'],'g1',r['version'],'python3',text,sha256(text.encode()).hexdigest()))
    for s in request['sources']:
        s['sourceType']={'goal':'contract','code':'code','dialogue':'external'}[s['kind']]
        if s['kind']=='goal':s['sourceId']='g1'
    return path,request,records


def test_read_only_snapshot_and_external_user_material(tmp_path):
    path,request,records=database(tmp_path);before=path.read_bytes()
    with SQLiteProgressResolver(path,request,supplemental_records=[records[-1]]) as resolver:
        report=estimate_progress(request,resolver,Provider())
    assert report['status']=='estimated',report
    assert path.read_bytes()==before
    assert report['materialSnapshot']['records'][1]['origin']=='business-database'


def test_model_wait_does_not_keep_business_read_transaction_open(tmp_path):
    path,request,records=database(tmp_path)
    class WritingProbe(Provider):
        def complete(self,**kwargs):
            with sqlite3.connect(path,timeout=0.01) as db:
                db.execute("UPDATE goal_records SET problem_id=problem_id")
            return super().complete(**kwargs)
    with SQLiteProgressResolver(path,request,supplemental_records=[records[-1]]) as resolver:
        report=estimate_progress(request,resolver,WritingProbe())
    assert report['status']=='estimated',report.get('failure')


@pytest.mark.parametrize('fault',['missing_table','bad_hash','foreign_code','wrong_problem','external_code','missing_file'])
def test_invalid_business_source_never_calls_model(tmp_path,fault):
    path,req,records=database(tmp_path)
    if fault=='missing_file':path=tmp_path/'absent.db'
    else:
        with sqlite3.connect(path) as db:
            if fault=='missing_table':db.execute('DROP TABLE code_snapshots')
            if fault=='bad_hash':db.execute("UPDATE code_snapshots SET source_code='tampered'")
            if fault=='foreign_code':db.execute("UPDATE code_snapshots SET goal_id='g2'")
            if fault=='wrong_problem':db.execute("UPDATE goal_records SET problem_version=2")
        if fault=='external_code':req['sources'][1]['sourceType']='external'
    provider=Provider()
    with SQLiteProgressResolver(path,req,supplemental_records=records) as resolver:
        report=estimate_progress(req,resolver,provider)
    assert report['status']=='failed' and report['failure']['stage']=='input'
    assert not provider.calls
    if fault=='missing_file':assert not path.exists()


def test_superseded_contract_retains_immutable_frozen_content(tmp_path):
    path,req,records=database(tmp_path)
    with sqlite3.connect(path) as db:db.execute("UPDATE goal_contract_versions SET status='superseded'")
    with SQLiteProgressResolver(path,req,supplemental_records=[records[-1]]) as resolver:
        report=estimate_progress(req,resolver,Provider())
    assert report['status']=='estimated'


def public_fixture(tmp_path):
    path,req,records=database(tmp_path);public=tmp_path/'tasks.db'
    context={'goalContext':req['goalContext'],'problemRef':req['problemRef'],'codeVersion':1,'loopId':'loop-01'}
    result={**context,'runId':'run-01','status':'succeeded','sampleStatus':'failed','hiddenTests':'DO_NOT_SEND',
        'caseResults':[{'testCaseId':'public-1','input':'1','expectedOutput':'2','actualOutput':'1','outcome':'wrongAnswer','backendLogs':'DO_NOT_SEND'}]}
    with sqlite3.connect(public) as db:
        db.executescript('CREATE TABLE tool_public_tasks(run_id,context_json,result_json,status); CREATE TABLE tool_public_comparison(run_id,conditions_hash);')
        db.execute('INSERT INTO tool_public_tasks VALUES(?,?,?,?)',('run-01',json.dumps(context),json.dumps(result),'succeeded'))
        db.execute('INSERT INTO tool_public_comparison VALUES(?,?)',('run-01','conditions-01'))
    req['sources'].append({'ref':'r01','kind':'runEvidence','sourceType':'publicRun','sourceId':'run-01','version':1,'contentHash':'sha256:'+'0'*64})
    return path,public,req,records


def test_public_safe_projection_keeps_old_code_and_not_hidden_fields(tmp_path):
    path,public,req,records=public_fixture(tmp_path);before=public.read_bytes()
    with SQLiteProgressResolver(path,req,public_task_db=public,supplemental_records=[records[-1]]) as resolver:
        run=resolver.resolve('r01');req['sources'][-1]['contentHash']=run['contentHash']
        report=estimate_progress(req,resolver,Provider())
    assert report['status']=='estimated'
    assert run['content']['codeSnapshotRef']=='c01' and run['content']['completed']
    assert 'DO_NOT_SEND' not in json.dumps(report)
    assert public.read_bytes()==before


@pytest.mark.parametrize('fault',['missing_db','scope','result_version','status'])
def test_public_bad_association_stops_before_model(tmp_path,fault):
    path,public,req,records=public_fixture(tmp_path)
    with sqlite3.connect(public) as db:
        raw=json.loads(db.execute('SELECT result_json FROM tool_public_tasks').fetchone()[0])
        if fault=='scope':raw['goalContext']={'goalId':'foreign','scope':'goal'}
        if fault=='result_version':raw['codeVersion']=2
        if fault=='status':raw['status']='running'
        db.execute('UPDATE tool_public_tasks SET result_json=?',(json.dumps(raw),))
    p=Provider()
    with SQLiteProgressResolver(path,req,public_task_db=None if fault=='missing_db' else public,supplemental_records=[records[-1]]) as resolver:
        report=estimate_progress(req,resolver,p)
    assert report['status']=='failed' and not p.calls


def test_observation_contract_mismatch_before_model(tmp_path):
    from tools.progress_evaluation import load_cases
    path,req,records=database(tmp_path)
    o=deepcopy(load_cases()[0]['records'][-1]['content']['observation'])
    o['goalContext']=req['goalContext'];o['organization']['scope'].update(goalContext=req['goalContext'],problemRef=req['problemRef'],contractVersion=2)
    o['sourceEventIds']=['event-1'];o['evidence']['codeRefs']=['c01','c02']
    event={'schemaVersion':'2.0.0','eventVersion':2,'eventId':'event-1','createdAt':'2026-09-19T00:00:00Z',
        'goalContext':req['goalContext'],'eventName':'codeSnapshotCreated','source':'api','actor':'user','idempotencyKey':'save-1','privacyLevel':'taskContent',
        'payload':{'artifactRef':'c02','codeVersion':2,'problemRef':req['problemRef']}}
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE observations(observation_id,goal_id,observation_json); CREATE TABLE raw_events(event_id,goal_id,event_json);')
        db.execute('INSERT INTO observations VALUES(?,?,?)',('obs-01','g1',json.dumps(o)))
        db.execute('INSERT INTO raw_events VALUES(?,?,?)',('event-1','g1',json.dumps(event)))
    req['sources'].append({'ref':'o01','kind':'observation','sourceType':'observation','sourceId':'obs-01','version':1,'contentHash':'sha256:'+'0'*64})
    p=Provider()
    with SQLiteProgressResolver(path,req,supplemental_records=[records[-1]]) as resolver:
        req['sources'][-1]['contentHash']=resolver.resolve('o01')['contentHash']
        report=estimate_progress(req,resolver,p)
    assert report['failure']['reason']=='SOURCE_ASSOCIATION_MISMATCH' and not p.calls

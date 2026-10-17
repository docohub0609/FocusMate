import json
import sqlite3
from datetime import datetime, timezone

from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.core.manager_execution import reconcile_runs
from test_session_manager import setup, ask


class Service:
    def __init__(self,path): self.path=path;self.cancelled=[]
    def cancel(self,run_id):self.cancelled.append(run_id);return {'status':'cancelled','runId':run_id}


def running(m):
    r=ask(m);j=m.claim('s1','w')
    m.complete('s1',j['stepId'],j['token'],{'kind':'tool','call':{'callId':'c','toolName':'run_public_samples','arguments':{'codeVersion':3}}})
    return r,m.claim('s1','w')


def service(tmp_path,r,status='succeeded'):
    srv=Service(tmp_path/'tasks.db')
    context={'loopId':r,'goalContext':{'goalId':'g1','scope':'goal'},'problemRef':{'problemId':'p1','problemVersion':1},'codeVersion':3}
    result={**context,'runId':'J1','status':status,'sampleStatus':'passed','caseResults':[{'outcome':'passed','stdin':'','expectedOutput':'1','actualOutput':'1'}]}
    with sqlite3.connect(srv.path) as db:
        db.execute('CREATE TABLE tool_public_tasks(run_id TEXT,context_json TEXT,result_json TEXT,status TEXT)')
        db.execute('INSERT INTO tool_public_tasks VALUES(?,?,?,?)',('J1',json.dumps(context),json.dumps(result),status))
    return srv


def test_read_completed_task_after_lost_callback_without_rerun(tmp_path):
    m,c=setup(tmp_path);r,t=running(m);srv=service(tmp_path,r)
    lookup=ManagerRunLookup(srv,None)
    reconcile_runs(m,'s1',lookup)
    assert m.read('s1')['steps'][t['stepId']]['status']=='done'
    next_step=m.claim('s1','new');assert next_step['kind']=='model'
    assert srv.cancelled==[]
    reconcile_runs(m,'s1',lookup)
    assert len(m.read('s1')['steps'])==3


def test_cancel_uses_registered_original_task(tmp_path):
    m,c=setup(tmp_path);r,t=running(m);srv=service(tmp_path,r,'running')
    m.command('s1','stop','stop_run',{})
    reconcile_runs(m,'s1',ManagerRunLookup(srv,None))
    assert srv.cancelled==['J1']
    assert m.read('s1')['cancellations'][0]['status']=='done'


def test_result_wrong_goal_cannot_be_attached(tmp_path):
    m,c=setup(tmp_path);r,t=running(m);srv=service(tmp_path,r)
    with sqlite3.connect(srv.path) as db:
        db.execute("UPDATE tool_public_tasks SET result_json=json_set(result_json,'$.goalContext.goalId','other')")
    report=reconcile_runs(m,'s1',ManagerRunLookup(srv,None))
    assert report['failures']
    assert m.read('s1')['steps'][t['stepId']]['result'] is None


def test_impossible_passed_result_rejected_on_recovery(tmp_path):
    m,c=setup(tmp_path);r,t=running(m);srv=service(tmp_path,r)
    with sqlite3.connect(srv.path) as db:
        db.execute("UPDATE tool_public_tasks SET result_json=json_set(result_json,'$.caseResults[0].outcome','failed')")
    assert reconcile_runs(m,'s1',ManagerRunLookup(srv,None))['failures']


def test_local_result_uses_saved_context_loop_not_missing_result_field(tmp_path):
    m,c=setup(tmp_path);r,t=running(m)
    # Adapter contract coverage: existing local result stores loop only in context_json.
    state=m.read('s1');step=state['steps'][t['stepId']]
    step['payload']={'callId':'c','toolName':'run_local_code','arguments':{'codeVersion':3,'observe':{'identifiers':['x'],'returnValue':False},'initialVariables':{'x':1}}}
    srv=Service(tmp_path/'local.db')
    ctx={'loopId':r,'goalContext':state['goalContext'],'problemRef':state['problemRef'],'codeVersion':3}
    value={k:v for k,v in ctx.items() if k!='loopId'}
    value.update(runId='L1',status='succeeded',observations={'x':2},setup={'x':1})
    with sqlite3.connect(srv.path) as db:
        db.execute('CREATE TABLE tool_local_tasks(run_id TEXT,context_json TEXT,result_json TEXT,status TEXT)')
        db.execute('INSERT INTO tool_local_tasks VALUES(?,?,?,?)',('L1',json.dumps(ctx),json.dumps(value),'succeeded'))
    assert ManagerRunLookup(None,srv).lookup(state,step)['loopId']==r

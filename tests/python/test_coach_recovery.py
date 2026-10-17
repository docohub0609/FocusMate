import json
import sqlite3
import pytest
from tests.python.test_coach_service import setup_service
from tests.python.test_coach_worker import Provider
from tests.python.test_dynamic_coach_loop import diagnosis,plan,review
from tests.python.test_runtime_subgoals import mutation
from services.coach_worker.worker import CoachWorker
from services.oj_worker.editor_runs import RunRepository


def prepared(tmp_path):
    e,g,s=setup_service(tmp_path);session=s.workspace(g,create=True)['session']['sessionId']
    e.save_draft(g,{'sourceCode':'print(1)','expectedRevision':0,'operationId':'code'})
    s.command(session,{'operationId':'ask','kind':'ask','codeVersion':1,'payload':{'text':'帮我修复边界并继续检查'}})
    checked=review();checked.pop('kind')
    provider=Provider([{'value':diagnosis(),'candidateGoals':[mutation()]},
        plan(changes=[mutation()],targets=['new:boundary']),checked])
    worker=CoachWorker(s,provider)
    worker.process_once();worker.process_once()
    # Explicit fake review: provide the condition IDs from the actual prepared input.
    from focusmate.core.coach_goal_review_wire import goal_requirement_catalog
    state=s.manager.read(session)
    catalog=goal_requirement_catalog(state,state['rounds'][state['currentRoundId']])
    provider.responses[0]['goalReview']['requirementReviews']=[
        {'requirementId':x['requirementId'],'verdict':'pass','reason':'模拟审核认为条件符合代码问题'} for x in catalog]
    return e,g,s,session,worker


def test_manager_commit_survives_business_projection_failure(tmp_path,monkeypatch):
    e,g,s,session,worker=prepared(tmp_path)
    # API DB transaction rolls back after the separate Manager commit.
    def broken(db,state):raise sqlite3.OperationalError('simulated projection failure')
    original=s._project
    st=s.manager.claim(session,'worker',materials=s.materials(session))
    monkeypatch.setattr(s,'_project',broken)
    with pytest.raises(sqlite3.OperationalError):s.complete(session,st['stepId'],st['token'],review())
    assert len(s.manager.read(session)['outbox'])==1
    monkeypatch.setattr(s,'_project',original)
    state=s.synchronize(session)
    with s.connect() as db:targets=json.loads(db.execute('SELECT targets_json FROM coach_workspaces').fetchone()[0])
    assert targets and len(state['outbox'])==1


def test_business_result_receipt_replayed_after_projection_failure_only_registers_one_followup(tmp_path,monkeypatch):
    e,g,s,session,worker=prepared(tmp_path);worker.process_once()
    runs=RunRepository(e.path)
    payload={'goalContext':{'goalId':g,'scope':'goal'},'problemRef':{'problemId':'problem-submit','problemVersion':1},
        'codeVersion':1,'runMode':'publicExample','operationId':'run'}
    task=runs.enqueue(payload);claim=runs.claim()
    runs.finish(claim,'failed',{'cases':[],'failureCode':'SIMULATED_DOCKER_UNAVAILABLE'})
    original=s._project
    monkeypatch.setattr(s,'_project',lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('simulated crash')))
    with pytest.raises(sqlite3.OperationalError):s.process_events()
    monkeypatch.setattr(s,'_project',original)
    s.process_events();s.process_events()
    state=s.manager.read(session)
    automatic=[r for r in state['rounds'].values() if r['trigger']=='run']
    assert len(automatic)==1
    assert automatic[0]['triggerEvidence']['failureCategory']=='system'
    assert automatic[0]['triggerEvidence']['completed'] is False
    with s.connect() as db:assert db.execute('SELECT processed FROM coach_execution_links').fetchone()[0]==1


def test_history_cursor_is_stable_while_new_messages_arrive(tmp_path):
    e,g,s,session,worker=prepared(tmp_path);worker.process_once()
    for i in range(22):s.command(session,{'operationId':f'm{i}','kind':'message','codeVersion':1,'payload':{'text':f'问题{i}'}})
    first=s.read(session)
    assert len(first['items'])==20 and first['items'][0]['text']=='问题21'
    s.command(session,{'operationId':'later','kind':'message','codeVersion':1,'payload':{'text':'后来新消息'}})
    second=s.read(session,cursor=first['nextCursor'])
    assert len(second['items'])==4
    ids=[x['historySequence'] for x in first['items']+second['items']]
    assert len(set(ids))==24

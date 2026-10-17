import json
from tests.python.test_coach_service import setup_service
from tests.python.test_dynamic_coach_loop import diagnosis, plan, review
from tests.python.test_final_oj import seeded, finish


class Provider:
    public_config={'model':'explicit-test-fixture','maxTokens':8192}
    def __init__(self,responses):self.responses=list(responses);self.calls=[]
    def complete(self,**kwargs):
        self.calls.append(kwargs)
        return {'content':json.dumps(self.responses.pop(0),ensure_ascii=False),'toolCalls':[]}


def test_worker_runs_owned_steps_and_recovery_does_not_regenerate(tmp_path):
    from services.coach_worker.worker import CoachWorker
    e,g,s=setup_service(tmp_path);session=s.workspace(g,create=True)['session']['sessionId']
    s.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'什么是循环？'}})
    checked=review();checked.pop('kind')
    provider=Provider([{'value':diagnosis(),'candidateGoals':[]},plan(),checked])
    worker=CoachWorker(s,provider)
    for _ in range(3):assert worker.process_once()
    state=s.manager.read(session)
    assert state['status']=='waiting_user', state['rounds']
    assert len(state['outbox'])==1 and len(provider.calls)==3
    assert CoachWorker(s,provider).process_once() is None
    assert len(provider.calls)==3


def test_trusted_ac_finishes_without_a_coach_session_or_model(tmp_path):
    from apps.api.coach_service import CoachService
    path,_,_,_=seeded(tmp_path)
    finish(path,tmp_path)
    service=CoachService(path)
    service.process_events()
    assert service.final.goal_state('g1')['status']=='completed'
    assert service.process_events()['finalized']==0


def test_ac_for_cleared_draft_cannot_complete_goal(tmp_path):
    from apps.api.coach_service import CoachService
    path,_,_,_=seeded(tmp_path);finish(path,tmp_path)
    s=CoachService(path)
    # Existing legacy snapshot remains, but the current editor draft is explicitly empty.
    with s.connect() as db:db.execute("INSERT INTO code_drafts VALUES('g1',0,'print(1)',1,'2026-09-28T00:00:00Z')")
    s.editor.save_draft('g1',{'sourceCode':'','expectedRevision':0,'operationId':'clear'})
    s.process_events()
    assert (s.final.goal_state('g1') or {}).get('status')!='completed'
    with s.connect() as db:
        report=json.loads(db.execute('SELECT report_json FROM coach_final_receipts').fetchone()[0])
    assert 'CODE_VERSION_CHANGED' in json.dumps(report)


def test_one_bad_session_does_not_block_other_work(tmp_path,monkeypatch):
    from services.coach_worker.worker import CoachWorker
    from focusmate.contracts.manager import ManagerError
    e,g,s=setup_service(tmp_path);session=s.workspace(g,create=True)['session']['sessionId']
    s.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'什么是循环？'}})
    original=s.synchronize
    def sync(key):
        if key=='broken':raise ManagerError('COACH_BINDING_CHANGED')
        return original(key)
    monkeypatch.setattr(s,'sessions',lambda:['broken',session]);monkeypatch.setattr(s,'synchronize',sync)
    worker=CoachWorker(s,Provider([{'value':diagnosis(),'candidateGoals':[]}]))
    assert worker.process_once()
    assert worker.failures==[{'sessionId':'broken','code':'COACH_BINDING_CHANGED'}]
    assert s.read(session)['round']['phase']=='plan'

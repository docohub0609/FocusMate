from tests.python.test_submission_api import running_api,request


def test_coach_registration_is_fast_persistent_and_idempotent(tmp_path):
    with running_api(tmp_path) as base:
        _,editor=request(base,'POST','/api/v1/problems/problem-submit/versions/1/editor-workspace',{})
        goal=editor['goal']['goalId'];path=f'/api/v1/goals/{goal}/coach-workspace'
        assert request(base,'GET',path)[0]==404
        status,workspace=request(base,'POST',path,{})
        assert status==200
        assert request(base,'POST',path,{})[1]==workspace
        session='/api/v1/coach-sessions/'+workspace['session']['sessionId']
        cmd={'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释循环'}}
        status,receipt=request(base,'POST',session+'/commands',cmd)
        assert status==202
        assert request(base,'POST',session+'/commands',cmd)[1]==receipt
        assert request(base,'POST',session+'/commands',{**cmd,'payload':{'text':'不同内容'}})[0]==409
        status,state=request(base,'GET',session)
        assert status==200 and state['round']['status']=='queued'
        assert state['round']['counts']=={'model':0,'tool':0,'execution':0}
        assert state['workGoals']==[]
        assert request(base,'GET',session+'/checks/missing')[0]==404
        assert request(base,'GET',session+'?limit=no')[0]==422
        assert request(base,'POST',session+'/commands',{**cmd,'kind':'final_completed'})[0]==422


def test_finished_round_with_skipped_checks_is_explicit_in_api_and_pinned_history(tmp_path,monkeypatch):
    # Exercise persisted v1 responses through the unchanged HTTP projection.
    from focusmate.core.dynamic_coach import DynamicCoachManager
    original_init=DynamicCoachManager.__init__
    def legacy_init(self,*args,**kwargs):
        original_init(self,*args,**{**kwargs,'workflow_version':1,'repair_policy_version':0})
    monkeypatch.setattr(DynamicCoachManager,'__init__',legacy_init)
    from apps.api.coach_service import CoachService
    from tests.python.test_dynamic_coach_loop import diagnosis,plan,review
    from tests.python.test_runtime_subgoals import mutation
    with running_api(tmp_path) as base:
        _,editor=request(base,'POST','/api/v1/problems/problem-submit/versions/1/editor-workspace',{})
        goal=editor['goal']['goalId']
        _,workspace=request(base,'POST',f'/api/v1/goals/{goal}/coach-workspace',{})
        sid=workspace['session']['sessionId'];url='/api/v1/coach-sessions/'+sid
        service=CoachService(tmp_path/'submission-api.db')
        service.editor.save_draft(goal,{'sourceCode':'print(1)','expectedRevision':0,'operationId':'save1'})
        request(base,'POST',url+'/commands',{'operationId':'ask1','kind':'ask','codeVersion':1,
            'payload':{'text':'检查修改后的边界问题'}})

        def accept(result):
            step=service.manager.claim(sid,'test-worker',materials=service.materials(sid))
            assert step is not None
            service.complete(sid,step['stepId'],step['token'],result)

        change=mutation();change['objective']='修正列循环范围'
        accept({'kind':'diagnosis','value':diagnosis(),'candidateGoals':[change]})
        candidate=plan(changes=[change],targets=[change['key']],next_step='check')
        accept({'kind':'plan','value':candidate})
        rejected=review();rejected['goalReview'].update(status='failed',reason='先澄清目标条件')
        accept(rejected)
        accept({'kind':'plan','value':candidate});accept(review())
        state=service.manager.read(sid);target=next(iter(state['workGoals']))
        following=plan(targets=[target]);following['action']['content']='从代码结构看，问题已经解决。'
        accept({'kind':'plan','value':following});accept(review())

        _,shown=request(base,'GET',url)
        assert shown['round']['status']=='finished' and shown['round']['failureInfo'] is None
        summary=shown['round']['checkSummary']
        assert summary['status']=='incomplete' and summary['savedReportCount']==0
        assert summary['targets'][0]['subgoalId']==target
        assert summary['targets'][0]['reasonCode']=='MODEL_BUDGET_RESERVED'
        action=next(x for x in shown['items'] if x['kind']=='action')
        assert action['checkSummary']==summary and action['roundId']==shown['round']['roundId']
        assert action['content'].startswith('本次正式检查未完成')
        assert '额度不足' in action['content'] and '修正列循环范围' in action['content']
        _,batch=request(base,'GET',url+'/checks/'+shown['round']['batchId'])
        assert batch['reports']==[] and batch['checkSummary']==summary
        # Projection annotates the saved model answer; it never rewrites its provenance.
        assert service.manager.read(sid)['outbox'][0]['learningAction']['content']=='从代码结构看，问题已经解决。'

        service.editor.save_draft(goal,{'sourceCode':'print(2)','expectedRevision':1,'operationId':'save2'})
        request(base,'POST',url+'/commands',{'operationId':'ask2','kind':'ask','codeVersion':2,
            'payload':{'text':'现在看另一份代码'}})
        _,later=request(base,'GET',url)
        assert later['round']['status']=='queued' and later['round']['checkSummary'] is None
        old_action=next(x for x in later['items'] if x['kind']=='action')
        assert old_action['codeVersion']==1 and old_action['currentCode'] is False
        assert old_action['checkSummary']==summary and old_action['content']==action['content']
        # Reading the old batch after a new round still describes only its original code.
        _,old_batch=request(base,'GET',url+'/checks/'+summary['batchId'])
        assert old_batch['checkSummary']==summary and old_batch['checkSummary']['codeVersion']==1

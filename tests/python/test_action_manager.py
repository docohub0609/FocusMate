import json
from test_action_policy import data,answer
from test_action_review import review
from test_manager_execution import Provider
from test_diagnosis_manager import drain
from focusmate.core.session_manager import SessionManager
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.core.action_execution import ActionExecutor
from focusmate.core.manager_execution import advance_once


def setup(tmp_path,responses):
    b,d=data();m=SessionManager(SQLiteManagerStore(tmp_path/'manager.db'))
    m.create('s',b['request']['goalContext'],b['request']['problemRef'],contract_version=1,
             code_version=2,allowed_tools=[],mode='coaching')
    m.command('s','ask','ask',{'text':b['records'][-1]['content']['text']})
    p=Provider([{'content':json.dumps(v,ensure_ascii=False),'toolCalls':[]} for v in responses])
    e=ActionExecutor(p,None,None,bundle=b,skills=[],diagnosis=d)
    return m,p,e


def test_checked_action_publishes_once_and_restart_does_not_regenerate(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review()]);s,r=drain(m,e)
    assert r['status']=='ready_to_publish',r.get('failure')
    assert r['counts']['model']==2
    a=m.publish('s')
    assert a['content']==answer()['content'] and a['learningAction']['schemaVersion']=='2.0.0'
    assert m.publish('s') is None
    drain(m,e);assert len(p.calls)==2


def test_one_repair_then_reject_does_not_publish(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review('fail'),answer(),review('fail')]);s,r=drain(m,e)
    assert r['status']=='failed' and len(p.calls)==4
    assert m.publish('s') is None and not s['outbox']


def test_uncertain_review_does_not_trigger_repair(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review('uncertain')]);s,r=drain(m,e)
    assert r['status']=='failed' and len(p.calls)==2
    assert r['failure']=='ACTION_REVIEW_UNCERTAIN'


def pending_review(m,e):
    for _ in range(3):advance_once(m,'s',e)
    st=m.claim('s','reviewer')
    assert st['kind']=='action_review'
    return st,e.execute(m.read('s'),st)


def test_paused_review_is_retained_then_published_after_resume(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review()]);st,result=pending_review(m,e)
    m.command('s','pause','pause',{});m.complete('s',st['stepId'],st['token'],result)
    assert m.publish('s') is None
    m.command('s','resume','resume',{});drain(m,e)
    assert m.publish('s') is not None and len(p.calls)==2


def test_changed_code_prevents_late_answer_publication(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review()]);st,result=pending_review(m,e)
    m.command('s','new-code','code_changed',{'codeVersion':3})
    m.complete('s',st['stepId'],st['token'],result)
    assert m.publish('s') is None
    s=m.read('s');assert s['rounds'][st['roundId']]['status']=='superseded'


def test_new_message_invalidates_old_action_without_resetting_budget(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review()]);st,result=pending_review(m,e)
    m.command('s','new-message','message',{'text':'不要给代码，只解释变量。'})
    m.complete('s',st['stepId'],st['token'],result)
    s=m.read('s');r=s['rounds'][st['roundId']]
    assert m.publish('s') is None and r['counts']['model']==2
    assert any(x['kind']=='diagnosis_prepare' and x['status']=='pending' for x in s['steps'].values())


def test_corrected_answer_requires_second_successful_review(tmp_path):
    from focusmate.core.action_planning import review_issues
    issues=review_issues(review('fail'),answer())
    second=review();second['issueChecks']=[{'issueId':i['issueId'],'status':'resolved','reason':'正文已核对。','materialRefs':['c02']} for i in issues]
    m,p,e=setup(tmp_path,[answer(),review('fail'),answer(),second]);s,r=drain(m,e)
    assert r['status']=='ready_to_publish' and len(p.calls)==4
    assert json.loads(p.calls[2]['messages'][1]['content'])['previousIssues']==issues
    assert json.loads(p.calls[3]['messages'][1]['content'])['previousIssues']==issues
    assert m.publish('s')['content']==answer()['content']


def test_second_review_without_old_issue_answers_cannot_publish(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review('fail'),answer(),review()]);s,r=drain(m,e)
    assert r['status']=='failed' and r['failure']=='INCOMPLETE_ISSUE_REVIEW'
    assert len(p.calls)==4 and m.publish('s') is None


def test_review_cannot_overwrite_pending_permission(tmp_path):
    m,p,e=setup(tmp_path,[answer(),review()]);st,result=pending_review(m,e)
    m.command('s','need-permission','request_permission',{'tools':['read_code_excerpt']})
    m.complete('s',st['stepId'],st['token'],result)
    r=m.read('s')['rounds'][st['roundId']]
    assert r['status']=='waiting_permission'
    assert m.publish('s') is None
    m.command('s','grant','grant_permission',{'tools':['read_code_excerpt']})
    s=m.read('s');assert s['rounds'][s['currentRoundId']]['counts']['model']==2


def test_reorientation_reason_reaches_next_diagnosis_model(tmp_path):
    from test_diagnosis import candidate
    m,p,e=setup(tmp_path,[])
    call={'callId':'reorient','toolName':'reconsider_diagnosis','arguments':{
        'reason':'需重新核对该问题是否属于输入解释，而非更新方向。','evidenceRefs':['u01']}}
    p.responses=iter([{'content':'','toolCalls':[call]},
        {'content':json.dumps(candidate()),'toolCalls':[]},
        {'content':json.dumps(answer()),'toolCalls':[]},{'content':json.dumps(review()),'toolCalls':[]}])
    s,r=drain(m,e)
    payload=json.loads(p.calls[1]['messages'][1]['content'])
    assert payload['plannerReorientation']['reason']==call['arguments']['reason']
    assert payload['plannerReorientation']['semanticStatus']=='notVerified'
    assert payload['plannerReorientation']['previousDiagnosis']['diagnosisId']==e.seed['diagnosisId']
    assert r['diagnosisReturns']==1
    m.command('s','fresh-question','message',{'text':'新问题：只解释输入。'})
    r=m.read('s')['rounds'][r['roundId']]
    assert 'plannerReorientation' not in r
    assert r['diagnosisReturns']==1

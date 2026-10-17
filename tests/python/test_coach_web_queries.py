"""Web projections use persisted evidence, never create model decisions."""
import json
import sqlite3
import pytest
from apps.api.coach_service import CoachService
from tests.python.test_coach_service import setup_service
from tests.python.test_dynamic_coach_loop import diagnosis, plan, review
from tests.python.test_runtime_subgoals import mutation
from tests.python.test_submission_api import running_api, request


def accept(service, sid, result):
    step = service.manager.claim(sid, 'fixture', materials=service.materials(sid))
    service.complete(sid, step['stepId'], step['token'], result)


def help_with_goal(service, sid, *, change=None):
    change = change or mutation()
    if service.manager.workflow_version == 1:
        accept(service, sid, {'kind': 'diagnosis', 'value': diagnosis(), 'candidateGoals': [change]})
    candidate = plan(changes=[change], targets=[change['key']])
    candidate['action']['content'] = '看看这行：\n```python\nprint(1)\n```'
    if service.manager.workflow_version == 2:
        from tests.python.test_coach_compact_flow import compact
        candidate=compact(targets=[change['key']],content='看看这行：\n```python\nprint(1)\n```')
        candidate['goalChanges']=[{key:value for key,value in change.items() if key != 'expectedVersion'}]
    accept(service, sid, {'kind': 'plan', 'value': candidate})
    accept(service, sid, review())


def test_goal_detail_pins_version_and_only_publishes_existing_records(tmp_path):
    editor, goal, service = setup_service(tmp_path)
    sid = service.workspace(goal, create=True)['session']['sessionId']
    code = editor.save_draft(goal, dict(sourceCode='print(1)', expectedRevision=0, operationId='save'))
    service.command(sid, dict(operationId='ask', kind='ask', codeVersion=1, payload={'text': '解释这里'}))
    help_with_goal(service, sid)
    state = service.read(sid)
    key = state['workGoals'][0]['subgoalId']
    assert state['currentWorkGoalIds'] == [key]
    action = next(i for i in state['items'] if i['kind'] == 'action')
    assert action['codeHash'] == code['contentHash']
    assert action['codeRanges'] == [{'startLine': 1, 'endLine': 1, 'text': 'print(1)'}]
    detail = service.work_goal(sid, key, version=1)
    assert detail['items'][0]['codeHash'] == action['codeHash']
    assert 'inputSnapshot' not in json.dumps(detail)
    with pytest.raises(ValueError, match='NOT_FOUND'): service.work_goal(sid, 'other-goal')
    with pytest.raises(ValueError, match='NOT_FOUND'): service.work_goal(sid, key, version=2)
    editor.save_draft(goal, dict(sourceCode='print(2)', expectedRevision=1, operationId='save2'))
    historical = service.work_goal(sid, key, version=1)
    assert historical['items'] == detail['items']
    service.command(sid, dict(operationId='next', kind='ask', codeVersion=2, payload={'text': '普通问答'}))
    if service.manager.workflow_version == 1:
        accept(service, sid, {'kind': 'diagnosis', 'value': diagnosis(), 'candidateGoals': []})
    accept(service, sid, {'kind': 'plan', 'value': plan()})
    accept(service, sid, review())
    assert service.read(sid)['currentWorkGoalIds'] == []


def test_completion_reads_without_creating_coach_contract_or_session(tmp_path):
    editor, goal, service = setup_service(tmp_path)
    assert service.completion(goal)['status'] == 'notVerified'
    assert service.sessions() == []
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT COUNT(*) FROM goal_contract_versions').fetchone()[0] == 0


def test_reports_remain_with_original_goal_version_after_revision(tmp_path):
    from focusmate.core.dynamic_coach_roles import verification_bundle
    editor, goal, service = setup_service(tmp_path)
    sid = service.workspace(goal, create=True)['session']['sessionId']
    editor.save_draft(goal, dict(sourceCode='print(1)', expectedRevision=0, operationId='save'))
    service.command(sid, dict(operationId='ask', kind='ask', codeVersion=1, payload={'text': '检查输出'}))
    help_with_goal(service, sid)
    key = service.read(sid)['workGoals'][0]['subgoalId']
    service.command(sid, dict(operationId='check', kind='check', codeVersion=1, payload={'subgoalIds': [key]}))
    if service.manager.workflow_version == 1:
        accept(service, sid, {'kind': 'diagnosis', 'value': diagnosis(), 'candidateGoals': []})
        accept(service, sid, {'kind': 'plan', 'value': plan(targets=[key], next_step='check')})
        accept(service, sid, review())
    step = service.manager.claim(sid, 'fixture', materials=service.materials(sid))
    state = service.manager.read(sid)
    bundle = verification_bundle(state, state['rounds'][step['roundId']], step)
    service.complete(sid, step['stepId'], step['token'], {'kind': 'verification', 'value': {'checks': [
        {'checkId': item['checkId'], 'status': 'notReady', 'reason': '模拟缺少依据',
         'quotes': [{'ref': 'code:1', 'text': 'print(1)'}], 'evidenceType': 'codeStructureCheck',
         'limitations': ['未经审核的模型自由文字，不允许显示']} for item in bundle['checks']]}})
    # Formal reports are visible before subsequent help exists, without unreviewed prose.
    immediate = service.read(sid)
    public_report = next(item for item in immediate['items'] if item['kind'] == 'report')
    assert public_report['codeVersion'] == 1 and public_report['checks'][0]['quotes'][0]['text'] == 'print(1)'
    for projection in (immediate, service.work_goal(sid, key), service.check_batch(sid, public_report['batchId'])):
        text = json.dumps(projection, ensure_ascii=False)
        assert '模拟缺少依据' not in text and '未经审核的模型自由文字' not in text
    accept(service, sid, {'kind': 'plan', 'value': plan(targets=[key])}); accept(service, sid, review())
    original = service.work_goal(sid, key, version=1)
    report = next(i for i in original['items'] if i['kind'] == 'report')
    assert report['result']['inputVersionRefs']['codeVersion'] == 1
    assert report['result']['status'] == 'notReady'
    assert report['batchId'] == service.read(sid)['round']['batchId']
    service.command(sid, dict(operationId='revise', kind='ask', codeVersion=1, payload={'text': '调整检查条件'}))
    change = mutation(key); change.update(kind='revise', expectedVersion=1, objective='新的问题范围')
    help_with_goal(service, sid, change=change)
    old = service.work_goal(sid, key, version=1)
    assert old['currentVersion'] == 2 and report in old['items']
    assert not any(i['kind'] == 'report' for i in service.work_goal(sid, key, version=2)['items'])
    first = service.work_goal(sid, key, version=1, limit=1)
    next_page = service.work_goal(sid, key, version=1, cursor=first['nextCursor'])
    assert first['items'][0] not in next_page['items']


def test_final_completion_is_bound_to_verifier_code_even_after_edit(tmp_path):
    from tests.python.test_final_oj import seeded, finish
    path, repo, submission, _ = seeded(tmp_path)
    service = CoachService(path)
    finish(path, tmp_path)
    assert service.completion('g1')['status'] == 'pending'
    service.process_events()
    result = service.completion('g1')
    assert result['status'] == 'completed' and result['codeVersion'] == 1 and result['resultId']
    assert service.sessions() == []
    repo.create_code_snapshot('g1', 'print(2)', 'python3')
    changed = service.completion('g1')
    assert changed['currentCodeVersion'] == 2 and changed['codeVersion'] == 1
    assert not any(word in json.dumps(changed) for word in ('expectedOutput', 'stdout', 'hiddenTests'))


def test_web_query_http_validation_and_no_workspace_side_effect(tmp_path):
    with running_api(tmp_path) as base:
        _, editor = request(base, 'POST', '/api/v1/problems/problem-submit/versions/1/editor-workspace', {})
        goal = editor['goal']['goalId']
        assert request(base, 'GET', f'/api/v1/goals/{goal}/completion')[0] == 200
        assert request(base, 'GET', f'/api/v1/goals/{goal}/coach-workspace')[0] == 404
        assert request(base, 'GET', '/api/v1/goals/missing/completion')[0] == 404
        _, workspace = request(base, 'POST', f'/api/v1/goals/{goal}/coach-workspace', {})
        path = '/api/v1/coach-sessions/' + workspace['session']['sessionId'] + '/work-goals/other'
        assert request(base, 'GET', path)[0] == 404
        for query in ('?version=0', '?version=no', '?version=1&version=2', '?unknown=x', '?limit=101'):
            assert request(base, 'GET', path + query)[0] == 422


def test_terminal_run_waits_for_persisted_follow_up_receipt(tmp_path):
    from tests.python.test_coach_recovery import prepared
    from services.oj_worker.editor_runs import RunRepository
    editor, goal, service, sid, worker = prepared(tmp_path)
    worker.process_once()
    assert not service.read(sid)['pendingFollowUp']
    runs = RunRepository(editor.path)
    runs.enqueue({'goalContext': {'goalId': goal, 'scope': 'goal'},
        'problemRef': {'problemId': 'problem-submit', 'problemVersion': 1},
        'codeVersion': 1, 'runMode': 'publicExample', 'operationId': 'run'})
    claim = runs.claim()
    runs.finish(claim, 'failed', {'cases': [], 'failureCode': 'SIMULATED_DOCKER_UNAVAILABLE'})
    before = service.read(sid)
    assert before['round']['status'] == 'finished' and before['pendingFollowUp']
    service.process_events()
    after = service.read(sid)
    assert after['round']['status'] == 'queued' and not after['pendingFollowUp']
    assert after['round']['roundId'] != before['round']['roundId']


def test_v2_report_remains_visible_when_following_help_fails(tmp_path):
    from focusmate.core.dynamic_coach_roles import verification_bundle
    editor, goal, service = setup_service(tmp_path, workflow_version=2)
    sid=service.workspace(goal,create=True)['session']['sessionId']
    editor.save_draft(goal,dict(sourceCode='print(1)',expectedRevision=0,operationId='save'))
    service.command(sid,dict(operationId='ask',kind='ask',codeVersion=1,payload={'text':'看一下这行'}))
    help_with_goal(service,sid)
    key=service.read(sid)['workGoals'][0]['subgoalId']
    service.command(sid,dict(operationId='check',kind='check',codeVersion=1,payload={'subgoalIds':[key]}))
    step=service.manager.claim(sid,'fixture',materials=service.materials(sid))
    assert step['kind']=='verification'
    internal=service.manager.read(sid)
    bundle=verification_bundle(internal,internal['rounds'][step['roundId']],step)
    service.complete(sid,step['stepId'],step['token'],{'kind':'verification','value':{'checks':[
        {'checkId':item['checkId'],'status':'failed','reason':'UNREVIEWED ANSWER',
         'quotes':[{'ref':'code:1','text':'print(1)'}],'evidenceType':'codeStructureCheck',
         'limitations':['PRIVATE LIMITATION']} for item in bundle['checks']]}})
    before=service.read(sid)
    report=next(item for item in before['items'] if item['kind']=='report')
    assert before['round']['phase']=='plan' and report['result']['status']=='failed'
    accept(service,sid,{'kind':'failure','code':'PROVIDER_UNAVAILABLE'})
    failed=service.read(sid)
    assert failed['round']['failureInfo']['savedReportCount']==1
    assert failed['round']['retryInfo']['canRetry']
    assert report in failed['items']
    assert 'UNREVIEWED ANSWER' not in json.dumps(failed)
    page=service.read(sid,limit=1)
    older=service.read(sid,limit=1,cursor=page['nextCursor'])
    assert page['items'][0] not in older['items']


def test_v2_switching_topic_keeps_old_goal_but_history_read_does_not_focus_it(tmp_path):
    from tests.python.test_coach_compact_flow import compact
    editor,goal,service=setup_service(tmp_path,workflow_version=2)
    sid=service.workspace(goal,create=True)['session']['sessionId']
    service.command(sid,dict(operationId='ask',kind='ask',codeVersion=None,payload={'text':'边界怎么处理'}))
    help_with_goal(service,sid)
    key=service.read(sid)['workGoals'][0]['subgoalId']
    service.command(sid,dict(operationId='newtopic',kind='ask',codeVersion=None,payload={'text':'输入什么意思'}))
    accept(service,sid,{'kind':'plan','value':compact(content='输入按行读取。')})
    accept(service,sid,review())
    state=service.read(sid)
    assert state['currentWorkGoalIds']==[] and state['workGoals'][0]['status']=='active'
    service.work_goal(sid,key,version=1)
    assert service.read(sid)['currentWorkGoalIds']==[]
    with service.connect() as db:
        assert json.loads(db.execute('SELECT targets_json FROM coach_workspaces WHERE session_id=?',(sid,)).fetchone()[0])==[]

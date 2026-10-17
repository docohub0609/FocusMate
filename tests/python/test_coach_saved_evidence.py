"""Saved execution facts reach Coach without another execution or model request."""
from copy import deepcopy
import json
import sqlite3

import pytest

from apps.api.coach_service import CoachService
from services.oj_worker.editor_runs import RunRepository
from services.oj_worker.judge import JudgeOutcome, OJResultRepository, TrustedTestBundle
from services.oj_worker.queue import WorkItem
from tests.python.test_editor_workspace import editor


def workspace(tmp_path):
    repo = editor(tmp_path)
    goal = repo.workspace('problem-submit', 1, create=True)['goal']
    repo.save_draft(goal['goalId'], {'sourceCode':'print(input())', 'expectedRevision':0, 'operationId':'save'})
    service = CoachService(repo.path)
    sid = service.workspace(goal['goalId'], create=True)['session']['sessionId']
    return service, sid, goal


def save_run(service, goal, key, *, mode='publicExample', status='finished', cases=None):
    runs = RunRepository(service.path)
    request = {'goalContext':{'goalId':goal['goalId'], 'scope':'goal'}, 'problemRef':goal['problemRef'],
               'codeVersion':1, 'runMode':mode, 'operationId':key}
    if mode == 'customInput': request['stdin'] = '2 0 2 1 1 0\n'
    task = runs.enqueue(request)
    claim = runs.claim()
    assert claim['run_id'] == task['runId']
    cases = cases if cases is not None else [{'index':1, 'stdin':'2 0 2 1 1 0\n',
        'expectedOutput':'0 0 1 1 2 2\n' if mode == 'publicExample' else None,
        'stdout':'0 0 1 1 2 2\n', 'stderr':'', 'outcome':'passed' if mode == 'publicExample' else 'executed'}]
    runs.finish(claim, status, {'cases':cases, 'failureCode':'RUN_SYSTEM_ERROR' if status == 'failed' else None})
    return task['runId']


def save_oj(service, goal, key, *, verdict='AC'):
    repo = service.editor.submissions
    sub, _ = repo.create_submission({'goalContext':{'goalId':goal['goalId'],'scope':'goal'},
        'problemRef':goal['problemRef'],'codeVersion':1,'language':'python3','idempotencyKey':key})
    code = repo.get_code_snapshot(goal['goalId'], 1)
    item = WorkItem(sub['submissionId'], goal['goalId'], 'problem-submit', 1, 1, 'python3',
                    sub['resourcePolicyRef'], code['sourceCode'], code['contentHash'])
    bundle = TrustedTestBundle('asset-fixture', 1, 'problem-submit', 1, 'ready', (None,) * 30)
    result = OJResultRepository(service.path).save(item, bundle,
        JudgeOutcome(verdict, 30 if verdict == 'AC' else 0, None if verdict == 'AC' else 1, 60, 1024, None))
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE submissions SET status='finished' WHERE submission_id=?", (sub['submissionId'],))
    return sub, result


def execution_records(service, sid):
    return [r for r in service.materials(sid)['records'] if r['kind'] == 'runEvidence']


def test_manual_help_reads_saved_runs_and_ac_without_trigger(tmp_path):
    service, sid, goal = workspace(tmp_path)
    public = save_run(service, goal, 'public')
    custom = save_run(service, goal, 'custom', mode='customInput')
    sub, _ = save_oj(service, goal, 'accepted')
    service.command(sid, {'operationId':'ask', 'kind':'ask', 'codeVersion':1,
                         'payload':{'text':'已经通过，为什么还说 one 要改？'}})
    records = execution_records(service, sid)
    by = {r['ref']:r['content'] for r in records}
    assert set(by) == {'execution:'+public, 'execution:'+custom, 'execution:'+sub['submissionId']}
    assert by['execution:'+public]['sampleStatus'] == 'passed'
    assert by['execution:'+public]['caseResults'][0]['stdout'] == '0 0 1 1 2 2\n'
    assert by['execution:'+custom]['evidenceType'] == 'runResult'
    assert 'sampleStatus' not in by['execution:'+custom] and 'verdict' not in by['execution:'+custom]
    assert by['execution:'+sub['submissionId']]['testSummary'] == {'passed':30,'total':30,'firstFailedCase':None}
    assert all(r['content']['codeSnapshotRef'] == 'code:1' for r in records)
    assert all(r['content']['codeHash'] == service.editor.submissions.get_code_snapshot(goal['goalId'],1)['contentHash'] for r in records)
    assert not service.manager.read(sid)['rounds'][service.manager.read(sid)['currentRoundId']].get('triggerEvidence')


def test_final_result_uses_saved_matching_verifier_and_survives_without_trigger(tmp_path):
    from tests.python.test_final_oj import seeded, finish, service as final_service, request
    path, repo, sub, code = seeded(tmp_path)
    finish(path, tmp_path)
    final = final_service(path).verify(request(sub))['verifierResult']
    service = CoachService(path)
    sid = service.workspace('g1', create=True)['session']['sessionId']
    records = service.materials(sid)['records']
    match = [r for r in records if r['content'].get('verifierType') == 'finalOj']
    assert len(match) == 1
    assert match[0]['kind'] == 'verifiedFact'
    assert match[0]['content']['status'] == 'passed'
    assert match[0]['content']['verifierResultId'] == final['verifierResultId']
    assert match[0]['content']['inputVersionRefs']['codeHash'] == code['contentHash']
    for field, wrong in (('codeHash','wrong'),('goalContractVersion',2),('problemVersion',2)):
        changed=deepcopy(final);changed['inputVersionRefs'][field]=wrong
        with sqlite3.connect(path) as db:
            db.execute('UPDATE final_verifier_results SET result_json=?',(json.dumps(changed),))
        assert not [r for r in service.materials(sid)['records'] if r['content'].get('verifierType')=='finalOj']
    with sqlite3.connect(path) as db:
        db.execute('UPDATE final_verifier_results SET result_json=?',(json.dumps(final),))
    # Completion is historical after another code version; never rebind it to that code.
    repo.create_code_snapshot('g1', 'print("different")', 'python3')
    assert not [r for r in service.materials(sid)['records'] if r['kind'] in ('runEvidence','verifiedFact')]


@pytest.mark.parametrize('damage', [
    "UPDATE editor_runs SET code_hash='wrong'",
    "UPDATE editor_runs SET code_version=2",
    "UPDATE editor_runs SET problem_version=2",
    "UPDATE editor_runs SET goal_id='foreign-goal'",
])
def test_mismatching_run_never_claims_current_code(tmp_path, damage):
    service, sid, goal = workspace(tmp_path)
    save_run(service, goal, 'public')
    with sqlite3.connect(service.path) as db: db.execute(damage)
    assert execution_records(service, sid) == []


@pytest.mark.parametrize('damage', [
    "UPDATE oj_results SET code_version=2",
    "UPDATE oj_results SET problem_version=2",
    "UPDATE oj_results SET goal_id='foreign-goal'",
    "UPDATE submissions SET code_version=2",
    "UPDATE submissions SET status='running'",
])
def test_mismatching_judge_result_never_claims_current_code(tmp_path, damage):
    service, sid, goal = workspace(tmp_path)
    save_oj(service, goal, 'accepted')
    with sqlite3.connect(service.path) as db: db.execute(damage)
    assert execution_records(service, sid) == []


def test_system_failure_is_not_a_code_verdict(tmp_path):
    service, sid, goal = workspace(tmp_path)
    save_run(service, goal, 'down', status='failed', cases=[])
    sub, _ = service.editor.submissions.create_submission({'goalContext':{'goalId':goal['goalId'],'scope':'goal'},
        'problemRef':goal['problemRef'],'codeVersion':1,'language':'python3','idempotencyKey':'down'})
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE submissions SET status='failed', status_reason='PRIVATE TRACE' WHERE submission_id=?",(sub['submissionId'],))
    records = execution_records(service,sid)
    assert len(records) == 2
    assert all(r['content']['completed'] is False and r['content']['failureCategory'] == 'system' for r in records)
    assert all(r['content'].get('verdict') is None for r in records)
    assert 'PRIVATE TRACE' not in json.dumps(records)


def test_oj_summary_is_allowlisted_and_recent_ac_is_retained(tmp_path):
    service, sid, goal = workspace(tmp_path)
    accepted, _ = save_oj(service, goal, 'accepted')
    for index in range(5): save_oj(service, goal, 'wa'+str(index), verdict='WA')
    with sqlite3.connect(service.path) as db:
        db.execute('UPDATE oj_results SET test_summary_json=? WHERE submission_id=?',
            (json.dumps({'passed':30,'total':30,'firstFailedCase':None,'stdout':'PRIVATE OUTPUT','input':'HIDDEN INPUT'}),accepted['submissionId']))
    records = execution_records(service,sid)
    assert len(records) <= 4
    assert any(r['content'].get('verdict') == 'AC' for r in records)
    assert 'PRIVATE OUTPUT' not in json.dumps(records) and 'HIDDEN INPUT' not in json.dumps(records)


def test_runs_are_bounded_keep_failure_case_and_mark_text_truncation(tmp_path):
    service, sid, goal = workspace(tmp_path)
    cases = [{'index':i+1,'stdin':'x'*10000,'expectedOutput':'ok','stdout':'ok','stderr':'','outcome':'passed'} for i in range(12)]
    cases[-1].update(outcome='outputMismatch',stdout='wrong')
    for i in range(5): save_run(service,goal,'run'+str(i),cases=cases)
    records = execution_records(service,sid)
    assert len(records) == 2
    for record in records:
        content = record['content']
        assert content['sampleStatus'] == 'failed' and content['caseCount'] == 12
        assert content['omittedCaseCount'] == 8 and len(content['caseResults']) == 4
        assert any(case['outcome'] == 'outputMismatch' for case in content['caseResults'])
        assert content['caseResults'][0]['inputTruncated'] is True
    assert len(json.dumps(records)) < 30000


def test_claim_keeps_original_evidence_snapshot_and_empty_draft_has_no_current_results(tmp_path):
    service, sid, goal = workspace(tmp_path)
    service.command(sid, {'operationId':'ask','kind':'ask','codeVersion':1,'payload':{'text':'看看代码'}})
    step = service.manager.claim(sid,'worker',materials=service.materials(sid))
    original = deepcopy(step['input'])
    save_run(service,goal,'later')
    assert execution_records(service,sid)
    assert service.manager.read(sid)['steps'][step['stepId']]['input'] == original
    service.editor.save_draft(goal['goalId'],{'sourceCode':'','expectedRevision':1,'operationId':'clear'})
    assert execution_records(service,sid) == []


def test_follow_up_trigger_is_deduplicated_and_only_saved_content_is_used(tmp_path):
    service, sid, goal = workspace(tmp_path)
    run = save_run(service,goal,'real')
    service.command(sid, {'operationId':'ask','kind':'ask','codeVersion':1,'payload':{'text':'看看结果'}})
    def trigger(state):
        state['rounds'][state['currentRoundId']]['triggerEvidence'] = {
            'id':run,'evidenceType':'publicSampleRunResult','completed':True,'invented':'NOT SAVED'}
        return {}
    service.manager.store.change(sid,'test-trigger',{'kind':'fixture'},trigger)
    records = execution_records(service,sid)
    assert len(records) == 1
    assert records[0]['ref'] == 'execution:'+run
    assert records[0]['content']['sampleStatus'] == 'passed'
    assert 'NOT SAVED' not in json.dumps(records)
    # Even a stale trigger cannot attach version 1 output to version 2 code.
    service.editor.save_draft(goal['goalId'],{'sourceCode':'print(2)','expectedRevision':1,'operationId':'v2'})
    assert execution_records(service,sid) == []


def test_repeated_old_reports_do_not_overflow_direct_check_context(tmp_path):
    from focusmate.core.runtime_subgoals import apply_changes
    from tests.python.test_runtime_subgoals import mutation
    from focusmate.core.dynamic_coach_execution import request_payload,prompt_for,BUDGET,GOAL_EVIDENCE_POLICY
    from focusmate.core.coach_condition_verification import CONDITION_PROMPT
    from focusmate.core.context_builder import build_context,MapContextResolver,hash_content
    service,sid,goal=workspace(tmp_path)
    service.command(sid,{'operationId':'ask','kind':'ask','codeVersion':1,'payload':{'text':'之前的检查条件是不是错了？'}})
    goals,aliases=apply_changes({},[mutation()],goal_id=goal['goalId'],available_refs={'message:1'})
    key=aliases['new:boundary']
    reports={}
    # Simulated repetitions reproduce history growth without any model/network call.
    for i in range(20):
        ref='process-'+str(20-i).zfill(2)
        reports[ref]={'reportId':ref,'workGoalVersion':1,
            'verifierResult':{'goalContext':{'goalId':goal['goalId'],'scope':'subgoal','subgoalId':key},
                'createdAt':f'2026-10-17T05:00:{i:02d}+00:00','status':'failed','inputVersionRefs':{'codeVersion':1}},
            'checks':[{'status':'failed','reason':'旧模型反复要求按自己的错误判断修改代码。'*160,
                       'quotes':[{'ref':'code:1','text':'print(input())'}]}],
            'inputSnapshot':{'records':['private historical input; keep saved, do not repeat in prompt']}}
    def seed(state):
        state['workGoals']=deepcopy(goals);state['checks']=deepcopy(reports)
        return {}
    service.manager.store.change(sid,'history-fixture',{'kind':'fixture'},seed)
    save_run(service,goal,'sample')
    save_oj(service,goal,'accepted')
    service.command(sid,{'operationId':'check','kind':'check','codeVersion':1,'payload':{'subgoalIds':[key]}})
    material=service.materials(sid)
    kept=[r for r in material['records'] if r['ref'].startswith('process-')]
    # Newest time is process-01; dictionary/ID order must not select process-20.
    assert [r['ref'] for r in kept]==['process-01']
    assert len(service.manager.read(sid)['checks'])==20
    assert all('inputSnapshot' not in r['content'] for r in kept)
    assert len([r for r in material['records'] if r['kind']=='runEvidence'])==2
    step=service.manager.claim(sid,'worker',materials=material)
    state=service.manager.read(sid)
    assert step['kind']=='verification'
    def context(candidate):
        return build_context({'goalContext':state['goalContext'],'purpose':'processVerification',
            'policyVersion':'dynamic-coach-v3','systemPrompt':prompt_for('verification',3)+GOAL_EVIDENCE_POLICY+CONDITION_PROMPT,
            'mandatoryPayload':request_payload(state,candidate),'dialogueRefs':[],'sourceRefs':[],
            'budget':BUDGET},MapContextResolver([]))
    assert context(step)['status']=='ready'
    old=deepcopy(step)
    old['input']['records']=[r for r in material['records'] if not r['ref'].startswith('process-')]
    for ref,report in reports.items():
        content={k:v for k,v in report.items() if k!='inputSnapshot'}
        old['input']['records'].append({'ref':ref,'kind':'verifiedFact','version':1,
            'content':content,'contentHash':hash_content(content),'goalContext':report['verifierResult']['goalContext']})
    assert context(old)['failure']['code']=='REQUIRED_MATERIAL_TOO_LONG'


def test_report_selection_prioritizes_current_goal_version_code_and_batch():
    from apps.api.coach_evidence import relevant_check_reports
    def report(key,version,code,when):
        return {'workGoalVersion':version,'verifierResult':{'goalContext':{'goalId':'g','subgoalId':key},
            'inputVersionRefs':{'codeVersion':code},'createdAt':when}}
    state={'goalContext':{'goalId':'g'},'codeVersion':2,'currentRoundId':'r','currentWorkGoalIds':['a'],
        'rounds':{'r':{'targets':[{'subgoalId':'a','version':2}],'reportRefs':['this-batch']}},
        'workGoals':{'a':{'status':'active','versions':[{'version':1},{'version':2}]},
                     'b':{'status':'withdrawn','versions':[{'version':1}]}},
        'checks':{'old-condition':report('a',1,2,'2026-10-17'),
                  'old-code':report('a',2,1,'2026-10-18'),
                  'current':report('a',2,2,'2026-10-16'),
                  'withdrawn':report('b',1,2,'2026-10-18'),
                  'this-batch':report('a',2,2,'2026-10-15')}}
    assert {ref for ref,_ in relevant_check_reports(state)}=={'current','this-batch'}

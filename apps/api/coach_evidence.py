"""Read bounded, code-bound execution facts; never execute or decide correctness."""
import json


RUNS_PER_MODE = 2
CASES_PER_RUN = 4
TEXT_LIMIT = 1024
CASE_FAILURES = {'outputMismatch','compileError','runtimeError','timeLimitExceeded',
                 'outputLimitExceeded','memoryLimitExceeded'}


def relevant_check_reports(state):
    """One latest report per current goal version; original history stays saved.

    Reports for this batch and explicitly selected goals are never evicted by
    unrelated history. Prefer this code's report over a newer report about old
    code, and compare saved timestamps rather than JSON dictionary key order.
    """
    current_round=state['rounds'].get(state.get('currentRoundId')) or {}
    goals={key:goal['versions'][-1]['version'] for key,goal in state['workGoals'].items()
           if goal['status'] not in ('withdrawn','replaced')}
    latest={}
    for ref,report in state['checks'].items():
        result=report['verifierResult'];scope=result['goalContext'];key=scope.get('subgoalId')
        if (scope.get('goalId')!=state['goalContext']['goalId'] or key not in goals
                or report['workGoalVersion']!=goals[key]):continue
        rank=(result['inputVersionRefs'].get('codeVersion')==state['codeVersion'],result['createdAt'],ref)
        if key not in latest or rank>latest[key][0]:latest[key]=(rank,ref,report)
    focused=list(dict.fromkeys([target['subgoalId'] for target in current_round.get('targets',[])]+
                               state.get('currentWorkGoalIds',[])))
    chosen={ref:state['checks'][ref] for ref in current_round.get('reportRefs',[]) if ref in state['checks']}
    for key in focused:
        if key in latest:
            _,ref,report=latest[key];chosen[ref]=report
    for _,ref,report in sorted(latest.values(),key=lambda item:item[0],reverse=True):
        if len(chosen)>=6:break
        chosen.setdefault(ref,report)
    return list(chosen.items())


def _project_case(case):
    result = {key:case[key] for key in ('index','outcome','wallTimeMs','executionTimeMs',
        'memoryLimitBytes','peakMemoryBytes') if key in case}
    for key, flag in (('stdin','inputTruncated'),('expectedOutput','expectedTruncated'),
                      ('stdout','stdoutTruncated'),('stderr','stderrTruncated')):
        value = case.get(key)
        result[key] = value[:TEXT_LIMIT] if isinstance(value,str) else None
        result[flag] = bool(case.get(flag)) or isinstance(value,str) and len(value)>TEXT_LIMIT
    return result


def _run(row, code):
    result = json.loads(row['result_json']) if row['result_json'] else {}
    cases = result.get('cases',[])
    # Retain actual mismatches ahead of successful samples when space is limited.
    selected = sorted(cases,key=lambda case: (case.get('outcome') not in CASE_FAILURES,
                                             case.get('index',0)))[:CASES_PER_RUN]
    completed = row['status']=='finished'
    content = {'id':row['run_id'], 'evidenceType':'publicSampleRunResult' if row['run_mode']=='publicExample' else 'runResult',
        'source':'savedUserRun', 'codeSnapshotRef':'code:'+str(row['code_version']),
        'codeVersion':row['code_version'], 'codeHash':code['contentHash'], 'runMode':row['run_mode'],
        'status':row['status'], 'completed':completed, 'createdAt':row['created_at'],
        'finishedAt':row['finished_at'], 'caseCount':len(cases), 'omittedCaseCount':len(cases)-len(selected),
        'caseResults':[_project_case(case) for case in selected],
        'failureCategory':'system' if row['status'] in ('failed','interrupted') else None}
    if row['run_mode']=='publicExample':
        content['sampleStatus'] = ('passed' if completed and cases and all(c.get('outcome')=='passed' for c in cases)
            else 'failed' if completed and any(c.get('outcome') in CASE_FAILURES for c in cases) else 'inconclusive')
    if content['failureCategory']=='system': content['failureCode']='RUN_SYSTEM_ERROR'
    return 'execution:'+row['run_id'], 'runEvidence', content


def _judge(row, code):
    content = {'id':row['submission_id'], 'evidenceType':'ojResult', 'source':'trustedOjWorker',
        'codeSnapshotRef':'code:'+str(row['code_version']), 'codeVersion':row['code_version'],
        'codeHash':code['contentHash'], 'status':row['status'], 'createdAt':row['created_at'],
        'completed':row['status']=='finished', 'failureCategory':None, 'verdict':None}
    if row['status'] in ('failed','expired'):
        content.update(completed=False, failureCategory='system')
    else:
        summary = json.loads(row['test_summary_json'])
        resource = json.loads(row['resource_summary_json'])
        assurance = json.loads(row['assurance_json']) if row['assurance_json'] else {}
        content.update(ojResultId=row['oj_result_id'], verdict=row['verdict'],
            testSummary={key:summary.get(key) for key in ('passed','total','firstFailedCase')},
            resourceSummary={key:resource.get(key) for key in ('totalWallTimeMs','totalExecutionTimeMs','peakMemoryBytes')},
            assurance='limited' if assurance.get('assurance')=='limited' else 'full',
            verifierAssetRef={'assetId':row['asset_id'],'assetVersion':row['asset_version']})
    return 'execution:'+row['submission_id'], 'runEvidence', content


def saved_execution_evidence(db, state, code):
    """Rows are read within the caller's snapshot and frozen by Manager.claim.

    OJ rows have no separate hash column: their immutable (goal, code_version)
    reference is joined to the hash-checked CodeSnapshot. Final results also
    retain their own pinned hash. Only summary columns are selected from OJ.
    """
    if code is None: return []
    goal = state['goalContext']['goalId']; problem = state['problemRef']
    params = (goal,problem['problemId'],problem['problemVersion'],code['codeVersion'])
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    current_round = state['rounds'].get(state.get('currentRoundId')) or {}
    trigger = current_round.get('triggerEvidence',{}).get('id')
    records=[]
    if 'editor_runs' in tables:
        where = "goal_id=? AND problem_id=? AND problem_version=? AND code_version=? AND code_hash=? AND status IN ('finished','failed','interrupted')"
        rows=[]
        for mode in ('publicExample','customInput'):
            rows.extend(db.execute('SELECT * FROM editor_runs WHERE '+where+
                ' AND run_mode=? ORDER BY created_at DESC,run_id DESC LIMIT ?',
                (*params,code['contentHash'],mode,RUNS_PER_MODE)).fetchall())
        if trigger and not any(row['run_id']==trigger for row in rows):
            rows.extend(db.execute('SELECT * FROM editor_runs WHERE '+where+
                ' AND run_id=? AND run_mode IN (\'publicExample\',\'customInput\')',(*params,code['contentHash'],trigger)).fetchall())
        records.extend(_run(row,code) for row in rows)
    # Do not let recent unsuccessful submissions hide the same-code AC.
    if 'oj_results' in tables:
        select = '''SELECT s.submission_id,s.code_version,s.status,s.created_at,o.oj_result_id,o.verdict,
            o.test_summary_json,o.resource_summary_json,o.assurance_json,o.asset_id,o.asset_version
            FROM submissions s JOIN code_snapshots c ON c.goal_id=s.goal_id AND c.code_version=s.code_version
            JOIN oj_results o ON o.submission_id=s.submission_id AND o.goal_id=s.goal_id
                AND o.problem_id=s.problem_id AND o.problem_version=s.problem_version AND o.code_version=s.code_version
            WHERE s.goal_id=? AND s.problem_id=? AND s.problem_version=? AND s.code_version=? AND c.content_hash=?
                AND s.status='finished' AND o.source='trustedOjWorker' '''
        bound=(*params,code['contentHash'])
        rows=db.execute(select+' ORDER BY o.created_at DESC,o.oj_result_id DESC LIMIT 3',bound).fetchall()
        accepted=db.execute(select+" AND o.verdict='AC' ORDER BY o.created_at DESC,o.oj_result_id DESC LIMIT 1",bound).fetchone()
        if accepted and all(row['submission_id']!=accepted['submission_id'] for row in rows): rows.append(accepted)
        if trigger and all(row['submission_id']!=trigger for row in rows):
            rows.extend(db.execute(select+' AND s.submission_id=?',(*bound,trigger)).fetchall())
        records.extend(_judge(row,code) for row in rows)
    # No judge verdict exists for a stopped infrastructure task.
    failed=db.execute('''SELECT s.submission_id,s.code_version,s.status,s.created_at
        FROM submissions s JOIN code_snapshots c ON c.goal_id=s.goal_id AND c.code_version=s.code_version
        WHERE s.goal_id=? AND s.problem_id=? AND s.problem_version=? AND s.code_version=? AND c.content_hash=?
            AND s.status IN ('failed','expired') ORDER BY s.created_at DESC,s.submission_id DESC LIMIT 2''',
        (*params,code['contentHash'])).fetchall()
    records.extend(_judge(row,code) for row in failed)
    if 'final_verifier_results' in tables:
        rows=db.execute('''SELECT result_id,result_json FROM final_verifier_results WHERE goal_id=?
            AND json_extract(result_json,'$.inputVersionRefs.codeVersion')=?
            AND json_extract(result_json,'$.inputVersionRefs.codeHash')=?
            AND json_extract(result_json,'$.inputVersionRefs.problemId')=?
            AND json_extract(result_json,'$.inputVersionRefs.problemVersion')=?
            AND json_extract(result_json,'$.inputVersionRefs.goalContractVersion')=?
            ORDER BY rowid DESC LIMIT 2''',(goal,code['codeVersion'],code['contentHash'],
                problem['problemId'],problem['problemVersion'],state['contractVersion'])).fetchall()
        for row in rows:
            value=json.loads(row['result_json'])
            if (value.get('goalContext')!={'goalId':goal,'scope':'goal'} or value.get('verifierType')!='finalOj'
                    or value.get('verifierResultId')!=row['result_id'] or value.get('status') not in ('passed','failed')): continue
            refs=value['inputVersionRefs']
            # Only publish the saved conclusion and identifiers, never its input snapshot.
            summary={key:value[key] for key in ('verifierResultId','verifierType','status','createdAt','ojResultId')}
            summary.update(codeSnapshotRef='code:'+str(code['codeVersion']),
                inputVersionRefs={key:refs[key] for key in ('goalContractVersion','problemId','problemVersion',
                    'submissionId','codeVersion','codeHash','ojResultId','assetId','assetVersion') if key in refs})
            records.append(('final-verification:'+row['result_id'],'verifiedFact',summary))
    return records

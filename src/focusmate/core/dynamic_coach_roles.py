"""Runtime target support for T071/T072/T073; model choices remain untrusted."""
from copy import deepcopy
from datetime import datetime, timezone

from jsonschema import Draft202012Validator
from focusmate.contracts.schema_validation import _catalog, validate
from focusmate.core.action_planning import (CANDIDATE_SCHEMA, REVIEW_SCHEMA, validate_action_candidate,
    validate_action_review, review_issues, action_from_candidate, obj, array, TEXT, REF)
from focusmate.core.diagnosis import validate_diagnosis
from focusmate.core.context_builder import hash_content
from focusmate.core.process_verification import finish_verification
from focusmate.core.runtime_subgoals import apply_changes, current_goals, CHANGE_SCHEMA
from focusmate.core.session_manager import identity
from focusmate.contracts.model_response import ReferenceValidationError

PLAN_SCHEMA = obj({'goalChanges':array(CHANGE_SCHEMA, maximum=12),
    'next':{'enum':['help','check','wait']}, 'checkTargets':array(REF), 'action':CANDIDATE_SCHEMA})
GOAL_REVIEW = obj({'status':{'enum':['passed','failed','inconclusive']},'reason':TEXT,'evidenceRefs':array(REF,1)})
DYNAMIC_REVIEW_SCHEMA = obj({'actionReview':REVIEW_SCHEMA,'goalReview':GOAL_REVIEW})


def diagnosis_schema():
    return obj({'value':_catalog()[0]['DiagnosisCandidates'], 'candidateGoals':array(CHANGE_SCHEMA,maximum=12)})


def input_bundle(s, r, st):
    material=st['input']; records=deepcopy(material['records'])
    from focusmate.core.coach_condition_context import condition_concerns
    concern_refs=[x['reportRef'] for x in condition_concerns(s)] if r.get('workflowVersion',1)>=3 else []
    for ref in dict.fromkeys(r['reportRefs']+concern_refs):
        if any(x['ref']==ref for x in records):continue
        report=s['checks'][ref]
        content=deepcopy(report) if ref in r['reportRefs'] else {k:deepcopy(v) for k,v in report.items() if k!='inputSnapshot'}
        records.append({'ref':ref,'kind':'verifiedFact','version':report['workGoalVersion'],
            'contentHash':hash_content(content),'goalContext':report['verifierResult']['goalContext'],
            'content':content})
    bundle={'request':{'goalContext':deepcopy(s['goalContext']),'problemRef':deepcopy(s['problemRef']),
        'contractVersion':s['contractVersion'],'contractRef':material['contractRef'],
        'userRequestRef':material['userRequestRef'],'currentCodeRef':material.get('currentCodeRef'),
        'historyDiagnosisRefs':material.get('historyDiagnosisRefs',[]), 'dialogueRefs':[], 'baselineRefs':[]},'records':records,'missing':[]}
    from focusmate.core.process_evidence import collect_evidence
    extra,_=collect_evidence(bundle,s,st)
    bundle['records'].extend(extra)
    return bundle


def prepare_plan(value, s, r, st):
    compact_value = value if r.get('workflowVersion', 1) >= 2 else None
    if compact_value is not None:
        from focusmate.core.coach_compact_wire import normalize_compact_plan
        value = normalize_compact_plan(value, s['workGoals'], after_check=r['checkBatchDone'], workflow_version=r.get('workflowVersion',1))
    Draft202012Validator(PLAN_SCHEMA).validate(value)
    bundle=input_bundle(s,r,st)
    refs={x['ref'] for x in bundle['records']}
    goals, aliases=apply_changes(s['workGoals'],value['goalChanges'],goal_id=s['goalContext']['goalId'],available_refs=refs)
    proposed=deepcopy(value)
    targets=[aliases.get(t,t) for t in value['checkTargets']]
    ids={g['subgoalId'] for g in current_goals(goals)}
    if not set(targets)<=ids: raise ValueError('UNKNOWN_CHECK_TARGET')
    if (value['next']=='check') != bool(targets): raise ValueError('INVALID_CHECK_DIRECTION')
    if value['next']=='check' and r['checkBatchDone']: raise ValueError('CHECK_BATCH_ALREADY_DONE')
    action=proposed['action']
    action['subgoalIds']=[aliases.get(t,t) for t in action['subgoalIds']]
    action['followUp']['criterionIds']=[aliases.get(t,t) for t in action['followUp']['criterionIds']]
    if r.get('workflowVersion',1)>=3:
        from focusmate.core.coach_condition_context import suppress_invalid_followup
        suppress_invalid_followup(action,s,goals)
    if compact_value is not None:
        from focusmate.core.coach_compact_wire import compact_diagnosis
        r['diagnosis']=validate_diagnosis(compact_diagnosis(compact_value, targets=action['subgoalIds']),
            bundle, runtime_goals=current_goals(goals))
    if r['trigger']!='user' and action['answerScope']=='fullSolution':
        raise ValueError('FULL_SOLUTION_NOT_AUTHORIZED')
    validate_action_candidate(action,bundle,r['diagnosis'],runtime_goals=current_goals(goals))
    if not set(targets)<=set(action['subgoalIds']): raise ValueError('INVALID_CHECK_ASSOCIATION')
    proposed['checkTargets']=targets
    return {'candidate':proposed,'goals':goals,'aliases':aliases}


def verification_bundle(s,r,st):
    bundle=input_bundle(s,r,st)
    target=st['payload']['target'];key=target['subgoalId']
    goal=s['workGoals'][key];version=goal['versions'][-1]
    if version['version']!=target['version'] or goal['status'] in ('withdrawn','replaced'):
        raise ValueError('STALE_WORK_GOAL')
    scope={'goalId':s['goalContext']['goalId'],'scope':'subgoal','subgoalId':key}
    ref='work:'+key+':'+str(version['version'])
    record={'ref':ref,'kind':'goal','version':version['version'],'goalContext':scope,
            'content':deepcopy(version),'contentHash':version['contentHash']}
    records=bundle['records']+[record]
    req={**bundle['request'],'goalContext':scope,'contractRef':ref,
         'actionId':r['decisionId'],'materialRefs':[x['ref'] for x in records]}
    checks=[{'checkId':'check-'+hash_content([key,version['contentHash'],index]).split(':')[-1][:20],
             'requirement':text,'contractRef':ref,'ordinal':index} for index,text in enumerate(version['checks'])]
    return {'request':req,'records':records,'checks':checks,'subgoal':version,
            'action':deepcopy(r.get('acceptedPlan'))}


def next_check(manager,s,r):
    if not r['checkQueue']:
        r['checkBatchDone']=True
        if (r.get('workflowVersion',1)>=2 and r['reportRefs'] and not r.get('uncheckedTargets')
                and all(s['checks'][ref]['verifierResult']['status']=='passed' for ref in r['reportRefs'])):
            # Formal reports are projected directly; no generated congratulation
            # or unreviewed verifier prose is published as coaching advice.
            r['status']='finished';s['status']='waiting_user'
            return
        manager._step(s,r,'plan')
        return
    # Leave two calls for a final plan and its independent review.
    if r['counts']['model'] >= s['limits']['model']-(1 if r.get('workflowVersion',1)>=3 else 2):
        r.setdefault('uncheckedTargets',[]).extend({**deepcopy(x),'reason':'MODEL_BUDGET_RESERVED'} for x in r['checkQueue']);r['checkQueue']=[]
        r['checkBatchDone']=True
        manager._step(s,r,'plan')
        return
    target=r['checkQueue'].pop(0)
    dependencies=s['workGoals'][target['subgoalId']]['versions'][-1]['dependsOn']
    def latest_passed(key):
        for ref in reversed(s['workGoals'][key]['verificationRefs']):
            report=s['checks'][ref]
            if report['workGoalVersion']==s['workGoals'][key]['versions'][-1]['version'] and report['verifierResult']['inputVersionRefs'].get('codeVersion')==r['codeVersion']:
                return report['verifierResult']['status']=='passed'
        return False
    blocked=[key for key in dependencies if not latest_passed(key)]
    if blocked:
        r.setdefault('uncheckedTargets',[]).append({**target,'reason':'DEPENDENCY_NOT_VERIFIED','dependencies':blocked})
        next_check(manager,s,r)
        return
    manager._step(s,r,'verification',{'target':target})


def accept_result(manager,s,r,st,result):
    kind=st['kind']
    if result.get('kind')!=kind: raise ValueError('STEP_RESULT_MISMATCH')
    bundle=input_bundle(s,r,st)
    if kind=='diagnosis':
        Draft202012Validator(diagnosis_schema()).validate({k:result[k] for k in ('value','candidateGoals')})
        # Validate proposals, but T071 never applies them.
        apply_changes(s['workGoals'],result['candidateGoals'],goal_id=s['goalContext']['goalId'],
                      available_refs={x['ref'] for x in bundle['records']})
        r['diagnosis']=validate_diagnosis(result['value'],bundle,runtime_goals=current_goals(s['workGoals']))
        r['candidateGoals']=deepcopy(result['candidateGoals'])
        manager._step(s,r,'plan')
    elif kind=='plan':
        r['prepared']=prepare_plan(result['value'],s,r,st)
        r['preparedStepId']=st['stepId']
        accept_prepared_plan(manager,s,r,st,bundle)
    elif kind=='review':
        review={k:result[k] for k in ('actionReview','goalReview')}
        Draft202012Validator(DYNAMIC_REVIEW_SCHEMA).validate(review)
        refs={x['ref'] for x in bundle['records']}
        invalid=set(review['goalReview']['evidenceRefs'])-refs
        if invalid: raise ReferenceValidationError('INVALID_REVIEW_REFERENCE',invalid)
        candidate=r['prepared']['candidate']
        verdict=validate_action_review(review['actionReview'],candidate['action'],refs,
                                       previous_issues=r.get('reviewIssues',()))
        if verdict!='passed' or review['goalReview']['status']!='passed':
            r['lastReview']=deepcopy(review)
            if r.get('repairPolicyVersion'):
                from focusmate.core.dynamic_coach_repair import schedule_repair
                r['reviewIssues']=review_issues(review['actionReview'],candidate['action'])
                source=s['steps'].get(r.get('preparedStepId'))
                failure={'kind':'failure','code':'REVIEW_NOT_PASSED','correctable':True,
                    'rejectedResult':deepcopy(source['result']) if source else None,
                    'review':deepcopy(review)}
                if (source and verdict!='uncertain' and review['goalReview']['status']!='inconclusive'
                        and schedule_repair(manager,s,r,st,failure,source_step=source)):return
                manager._stop(s,r,'failed','REVIEW_NOT_PASSED');return
            if (not r['repaired'] and verdict!='uncertain' and review['goalReview']['status']!='inconclusive'
                    and r['counts']['model']<=s['limits']['model']-2):
                r['repaired']=True
                r['reviewIssues']=review_issues(review['actionReview'],candidate['action'])
                manager._step(s,r,'plan')
            else: manager._stop(s,r,'failed','REVIEW_NOT_PASSED')
            return
        commit_plan(manager,s,r,st,bundle,review)
    elif kind=='verification':
        prepared=verification_bundle(s,r,st)
        if r.get('workflowVersion',1)>=3:
            from focusmate.core.coach_condition_verification import finish_condition_verification
            report=finish_condition_verification(result['value'],prepared)
        else:report=finish_verification(result['value'],prepared)
        target=st['payload']['target'];key=target['subgoalId']
        report['workGoalVersion']=target['version'];report['batchId']=r['batchId']
        report['schemaVersion']='3.0.0' if r.get('workflowVersion',1)>=3 else '2.0.0'
        versions=report['verifierResult']['inputVersionRefs']
        versions['workGoalHash']=versions['contractHash'];versions['workGoalVersion']=target['version']
        versions['contractHash']=next(x['contentHash'] for x in bundle['records'] if x['ref']==bundle['request']['contractRef'])
        report['verifierResult']['reason']='逐项检查指定版本的工作目标；结果仅对应本次代码与材料。'
        validate('ProcessVerificationReportV3' if r.get('workflowVersion',1)>=3 else 'ProcessVerificationReportV2',report)
        # Envelope ordering metadata is not part of the immutable report schema.
        # Keep it separately so legacy consumers may still validate the report.
        r.setdefault('reportEvents',{})[report['reportId']]={'historySequence':s['revision']+1,
            'occurredAt':manager.clock(),'roundId':r['roundId']}
        ref=report['reportId'];s['checks'][ref]=report;r['reportRefs'].append(ref)
        s['workGoals'][key]['verificationRefs'].append(ref)
        s['workGoals'][key]['status']='verified' if report['verifierResult']['status']=='passed' else 'active'
        next_check(manager,s,r)
    else: raise ValueError('UNKNOWN_COACH_PHASE')


def accept_prepared_plan(manager,s,r,st,bundle):
    """Use the exact prepared goal identities that an independent review saw."""
    candidate=r['prepared']['candidate']
    if r.get('workflowVersion',1)>=2 and candidate['next']=='wait' and not candidate['goalChanges']:
        r['decisionId']=identity('decision');r['acceptedPlan']=deepcopy(candidate)
        s['decisions'].append({'decisionId':r['decisionId'],'roundId':r['roundId'],
            'goalContext':deepcopy(s['goalContext']),'plan':deepcopy(candidate),
            'review':None,'reviewNotRequired':'fixedWaitOnly',
            'codeVersion':r['codeVersion'],'inputHash':st['inputHash']})
        r['waitMessage']='好的，等你修改。'
        r['status']='finished';s['status']='waiting_user'
    elif r.get('workflowVersion',1)>=3 and candidate['action']['answerScope']!='fullSolution':
        commit_plan(manager,s,r,st,bundle,None)
    else:manager._step(s,r,'review')


def commit_plan(manager,s,r,st,bundle,review):
    candidate=r['prepared']['candidate']
    s['workGoals']=deepcopy(r['prepared']['goals'])
    r['decisionId']=identity('decision')
    r['acceptedPlan']=deepcopy(candidate)
    if r.get('workflowVersion',1)>=2 and candidate['next']!='wait':
        s['currentWorkGoalIds']=deepcopy(candidate['action']['subgoalIds'])
        if r.get('workflowVersion',1)>=3 and candidate['next']!='check':
            from focusmate.core.coach_condition_context import invalid_condition_goal_ids
            blocked=invalid_condition_goal_ids(s)
            s['currentWorkGoalIds']=[key for key in s['currentWorkGoalIds'] if key not in blocked]
    s['decisions'].append({'decisionId':r['decisionId'],'roundId':r['roundId'],
        'goalContext':deepcopy(s['goalContext']),'plan':deepcopy(candidate),'review':deepcopy(review),
        'codeVersion':r['codeVersion'],'inputHash':st['inputHash']})
    if candidate['next']=='check':
        pending=set(candidate['checkTargets']);ordered=[]
        while pending:
            ready=[k for k in candidate['checkTargets'] if k in pending and
                   not set(s['workGoals'][k]['versions'][-1]['dependsOn']) & pending]
            if not ready: raise ValueError('GOAL_DEPENDENCY_CYCLE')
            for k in ready:
                version=s['workGoals'][k]['versions'][-1]
                ordered.append({'subgoalId':k,'version':version['version']});pending.remove(k)
        r['checkQueue']=ordered
        r['batchId']=identity('batch')
        next_check(manager,s,r)
    else:
        publish_help(manager,s,r,st,bundle)


def publish_help(manager,s,r,st,bundle):
    candidate=r['acceptedPlan'];action_id=identity('action')
    action=action_from_candidate(candidate['action'],bundle,r['diagnosis'],action_id=action_id,
        created_at=datetime.now(timezone.utc).isoformat())
    if r.get('workflowVersion',1)>=3:
        action['schemaVersion']='3.0.0'
        action['semanticStatus']='modelReviewedNotVerified' if candidate['action']['answerScope']=='fullSolution' else 'modelOpinionNotVerified'
        validate('LearningActionV3',action)
    action['status']='published'
    s['outbox'].append({'actionId':action_id,'roundId':r['roundId'],'learningAction':action,
        'historySequence':s['revision']+1,'publishedAt':manager.clock(),
        'codeVersion':r['codeVersion'],'workGoalVersions':{k:s['workGoals'][k]['versions'][-1]['version']
            for k in action['subgoalIds']},'decisionId':r['decisionId']})
    r['status']='finished';s['status']='waiting_user'

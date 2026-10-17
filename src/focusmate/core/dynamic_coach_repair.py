"""One bounded content repair, owned by the original Coach round and lease."""
from copy import deepcopy
import json
import time

from jsonschema import ValidationError
from focusmate.contracts.manager import ManagerError
from focusmate.core.session_manager import TERMINAL, identity


# These are failures of a generated answer, not permissions or stored state.
CONTENT_FAILURES = {
    'MODEL_RESPONSE_INVALID', 'COACH_RESPONSE_REJECTED', 'INVALID_ACTION_FORMAT',
    'INVALID_ACTION_TARGET', 'INVALID_ACTION_SCOPE', 'INVALID_ACTION_REFERENCE',
    'INVALID_DIAGNOSIS_CORRECTION', 'MISSING_EXPECTED_USER_ACTION',
    'INVALID_FOLLOWUP_TARGET', 'MISSING_VERIFICATION_DIRECTION',
    'UNNECESSARY_VERIFICATION', 'INCOMPLETE_ACTION_REVIEW',
    'INVALID_REVIEW_REFERENCE', 'INVALID_REVIEW_QUOTE', 'MISSING_ERROR_LOCATION',
    'INCOMPLETE_ISSUE_REVIEW', 'UNKNOWN_CHECK_TARGET', 'INVALID_CHECK_DIRECTION',
    'INVALID_CHECK_ASSOCIATION', 'INVALID_VERIFICATION_OUTPUT',
    'INCOMPLETE_CHECK_COVERAGE', 'INVALID_EVIDENCE_QUOTE', 'INVALID_CODE_RANGE',
    'INVALID_USER_MESSAGE_REFERENCE', 'INVALID_CONDITION_CANNOT_DECIDE_CODE',
    'EVIDENCE_TYPE_NOT_ACCEPTED', 'REQUIREMENT_IS_NOT_USER_EVIDENCE',
    'USER_EXPLANATION_REQUIRED', 'CURRENT_CODE_EVIDENCE_REQUIRED',
    'CURRENT_COMPLEXITY_EVIDENCE_REQUIRED', 'PROBLEM_FACT_REQUIRED',
    'RUN_EVIDENCE_REQUIRED', 'RUN_FACT_CONTRADICTION', 'LOCAL_RUN_LIMITATION_REQUIRED',
    'PUBLIC_COMPARISON_REQUIRED', 'REVIEW_NOT_PASSED', 'INVALID_FACT_REFERENCE',
    'INVALID_EVIDENCE_REFERENCE', 'DUPLICATE_MODEL_KEY',
    'GOAL_REVIEW_COVERAGE_REQUIRED', 'GOAL_REVIEW_COVERAGE_MISMATCH',
}


def correctable_exception(exc):
    """Do not turn arbitrary KeyError/TypeError implementation bugs into AI work."""
    return (isinstance(exc, ValidationError) or str(exc) in CONTENT_FAILURES
        or str(exc) in {'duplicate JSON key','nonfinite JSON number','overflow JSON number'}
        or getattr(exc,'code',None)=='MODEL_RESPONSE_INVALID')


def schedule_repair(manager, s, r, failed_step, failure, *, source_step=None):
    if (not failure.get('correctable') or failure.get('code') not in CONTENT_FAILURES
            or failed_step['kind'] not in ('diagnosis','plan','verification','review')
            or r.get('repairRecord')):
        return False
    from focusmate.core.repair_agent import new_repair_record
    from focusmate.core.dynamic_coach_execution import request_payload, prompt_for, GOAL_EVIDENCE_POLICY
    source=deepcopy(source_step or failed_step)
    payload=request_payload(s,source)
    # A plan may request a complete answer. Reserve its mandatory review even
    # when a malformed original reply did not reveal its answer scope.
    review_calls=1 if source['kind']=='plan' else 0
    record=new_repair_record(repair_id=identity('repair'), owner={
        'sessionId':s['sessionId'],'roundId':r['roundId'],
        'goalContext':deepcopy(s['goalContext']),'problemRef':deepcopy(s['problemRef']),
        'codeVersion':r['codeVersion'],'contractVersion':s['contractVersion'],
        'sourceStepId':source['stepId'],'failedStepId':failed_step['stepId']},
        failure=deepcopy(failure), materials={'request':payload,
            'instructions':prompt_for(source['kind'],r.get('workflowVersion',1))+GOAL_EVIDENCE_POLICY},
        original=deepcopy(failure.get('responseText') or source.get('result') or failure.get('rejectedResult')),
        review_calls=review_calls)
    from focusmate.core.coach_model_projection import repair_packet
    # Freeze a small model-facing view as well as the complete private archive.
    # Old queued repairs have no presentation and retain their existing protocol.
    record['modelPacket']=repair_packet(record)
    from focusmate.core.context_builder import hash_content
    record['modelPacketHash']=hash_content(record['modelPacket'])
    r['repairRecord']=record
    r['repaired']=True
    if r['counts']['model']+1+review_calls>s['limits']['model']:
        record.update(status='exhausted',reason='REPAIR_BUDGET_EXHAUSTED')
        manager._stop(s,r,'failed','REPAIR_BUDGET_EXHAUSTED')
        return True
    st=manager._step(s,r,'repair',{'sourceStepId':source['stepId']})
    st['sourceStep']=source
    st['frozenInput']=deepcopy(source['input'])
    return True


def owned_repair(manager,s,step_id,token):
    st=s['steps'].get(step_id)
    if (not st or st['kind']!='repair' or st.get('token')!=token
            or st['status']!='running' or manager.clock()>=st['leaseUntil']):
        raise ManagerError('STALE_OWNER')
    r=s['rounds'][st['roundId']]
    if (r['status'] in TERMINAL or manager.clock()>=r['deadline']
            or s['currentRoundId']!=r['roundId'] or s['codeVersion']!=r['codeVersion']
            or s['contractVersion']!=r['repairRecord']['owner']['contractVersion']):
        raise ManagerError('STALE_OWNER')
    if s['status']=='paused':raise ManagerError('COACH_PAUSED')
    if st.get('workGoalVersions')!={k:g['versions'][-1]['contentHash'] for k,g in s['workGoals'].items()}:
        raise ManagerError('WORK_GOALS_CHANGED')
    if st.get('workGoalStates')!={k:g['status'] for k,g in s['workGoals'].items()}:
        raise ManagerError('WORK_GOALS_CHANGED')
    return r,st


def interrupt_repair(r):
    record=r.get('repairRecord')
    if record and record['status'] in ('running','submitted','validating'):
        record.update(status='interrupted',reason='REPAIR_INTERRUPTED')


class CandidateReply:
    """Exercise the unchanged response parser without another model request."""
    public_config={}

    def __init__(self,value):self.value=value

    def complete(self,**unused):
        return {'content':json.dumps(self.value,ensure_ascii=False),'toolCalls':[]}


def enforce_scope(s, source, candidate):
    if source['kind'] not in ('plan','diagnosis'):return
    # Rebinding a hint is allowed; rewriting an established problem/criterion
    # to make that hint pass is not. Existing goal revisions remain user work.
    for change in candidate.get('goalChanges',candidate.get('candidateGoals',[])):
        if change.get('key') in s['workGoals'] and change.get('kind')!='reuse':
            raise ValueError('REPAIR_SCOPE_VIOLATION')
        if set(change.get('replaces',[])) & set(s['workGoals']):
            raise ValueError('REPAIR_SCOPE_VIOLATION')


def execute_repair(manager,session,step,provider,*,review_provider=None):
    from focusmate.core.repair_agent import RepairExecutor, RepairValidation
    from focusmate.core.dynamic_coach_execution import DynamicCoachExecutor
    s=manager.read(session);r=s['rounds'][step['roundId']]
    record=deepcopy(r['repairRecord']);source=deepcopy(step['sourceStep'])
    payload=record['materials']['request']
    schema=payload.get('outputSchema',payload.get('reviewSchema'))

    def guard():owned_repair(manager,manager.read(session),step['stepId'],step['token'])

    def validate_checked(candidate,budget):
        guard()
        shadow=deepcopy(s);rr=shadow['rounds'][step['roundId']]
        enforce_scope(shadow,source,candidate)
        result=DynamicCoachExecutor(CandidateReply(candidate)).execute(shadow,source)
        if result.get('kind')=='failure':return RepairValidation(False,reason=result['code'])
        manager._accept(shadow,rr,source,result)
        reviewed=None
        if rr.get('phase')=='review' and rr['status'] not in TERMINAL:
            review_step=shadow['steps'][rr['currentStepId']]
            review_step.update(input=deepcopy(source['input']),inputHash=source['inputHash'],deadline=r['deadline'])
            budget.model_call('review')
            started=time.monotonic()
            reviewed=DynamicCoachExecutor(provider,review_provider=review_provider).execute(shadow,review_step)
            budget.record['calls'][-1].update(status='failed' if reviewed.get('kind')=='failure' else 'returned',
                elapsedMs=round((time.monotonic()-started)*1000),usage=deepcopy(reviewed.get('usage',{})))
            budget.persist()
            if reviewed.get('kind')=='failure':return RepairValidation(False,reason=reviewed['code'])
            manager._accept(shadow,rr,review_step,reviewed)
        if rr['status']=='failed':return RepairValidation(False,reason=rr.get('failure','REVIEW_NOT_PASSED'))
        accepted={'result':result,'review':reviewed}
        if source['kind']=='plan':
            accepted.update(prepared=deepcopy(rr['prepared']),diagnosis=deepcopy(rr['diagnosis']))
        return RepairValidation(True,result=accepted)

    def validate(candidate,budget):
        try:return validate_checked(candidate,budget)
        except (ValueError,ValidationError) as exc:
            if isinstance(exc,ManagerError):raise
            return RepairValidation(False,reason=str(exc)[:200])

    final=RepairExecutor(provider).execute(record,candidate_schema=schema,validate=validate,
        checkpoint=lambda value:manager.repair_checkpoint(session,step['stepId'],step['token'],value),
        guard=guard,deadline=time.monotonic()+max(0,r['deadline']-manager.clock()),
        charge_model=lambda purpose:manager.charge_repair_model(session,step['stepId'],step['token'],purpose))
    return {'kind':'repair','repairId':final['repairId']}


def accept_repair(manager,s,r,st,result):
    record=r['repairRecord']
    if result.get('kind')!='repair' or result.get('repairId')!=record['repairId']:
        interrupt_repair(r)
        manager._stop(s,r,'failed',result.get('code','REPAIR_FAILED'));return
    if record['status']!='succeeded' or not record.get('validation',{}).get('accepted'):
        manager._stop(s,r,'failed', 'REPAIR_BUDGET_EXHAUSTED' if record['status']=='exhausted' else
            'REPAIR_INTERRUPTED' if record['status']=='interrupted' else 'REPAIR_FAILED')
        return
    accepted=record['validation']['result'];source=st['sourceStep']
    enforce_scope(s,source,record['candidate'])
    if source['kind']=='plan':
        # Reuse server-created aliases, versions and hashes from shadow validation.
        # Re-running prepare_plan would allocate different UUIDs after review.
        from focusmate.core.dynamic_coach_roles import accept_prepared_plan,input_bundle
        r.update(prepared=deepcopy(accepted['prepared']),diagnosis=deepcopy(accepted['diagnosis']),
            preparedStepId=source['stepId'])
        accept_prepared_plan(manager,s,r,source,input_bundle(s,r,source))
    else:manager._accept(s,r,source,accepted['result'])
    if accepted.get('review') is not None:
        review_step=s['steps'][r['currentStepId']]
        if review_step['kind']!='review':raise ManagerError('REPAIR_REVIEW_MISMATCH')
        review_step.update(input=deepcopy(source['input']),inputHash=source['inputHash'],
            result=deepcopy(accepted['review']),status='done',finishedAt=manager.clock())
        manager._accept(s,r,review_step,accepted['review'])
    record['applied']=True

"""Resume owned computation without renewing its input, budget or execution permission."""
from copy import deepcopy
from focusmate.contracts.manager import ManagerError
from focusmate.core.context_builder import hash_content

EXECUTION = {'run_public_samples', 'run_local_code'}
FAILURES = {'failed', 'expired', 'interrupted'}


def input_identity(s, r):
    return hash_content({'code': s['codeVersion'], 'contract': s['contractVersion'],
        'problem': s['problemRef'], 'messages': s['messages'],
        'goals': {k: {'status': g['status'], 'version': g['versions'][-1]} for k, g in s['workGoals'].items()},
        'reports': r.get('reportRefs', []), 'evidence': r.get('triggerEvidence')})


def remember_failure(s, r):
    if r.get('workflowVersion', 1) < 2 or r['status'] not in FAILURES:
        return
    # JSON storage sorts object keys: dictionary order is not execution order.
    step = s['steps'].get(r.get('currentStepId'))
    if step and step['roundId'] == r['roundId']:
        r['failureStepId'] = step['stepId']
        r['failureInputIdentity'] = input_identity(s, r)
    # A failed transport may leave execution running even though its step was
    # marked done before accepting the reply. Cleanup must not depend on that label.
    for st in s['steps'].values():
        if st['roundId'] != r['roundId'] or st['kind'] != 'tool' or st['payload']['toolName'] not in EXECUTION or 'owner' not in st:
            continue
        receipt = (st.get('result') or {}).get('result', {})
        if receipt.get('status') in ('succeeded', 'failed', 'rejected'):
            continue
        if not any(x['stepId'] == st['stepId'] for x in s['cancellations']):
            s['cancellations'].append({'stepId': st['stepId'], 'roundId': r['roundId'],
                'status': 'pending', 'reason': r['failure']})


def unresolved_execution(s, r):
    for st in s['steps'].values():
        if st['roundId'] != r['roundId'] or st['kind'] != 'tool' or st['payload']['toolName'] not in EXECUTION:
            continue
        result = (st.get('result') or {}).get('result', {})
        if st['status'] == 'done' and result.get('status') in ('succeeded', 'failed', 'rejected'):
            continue
        if st['status'] in ('pending', 'cancelled') and 'owner' not in st:
            continue
        cleared = any(x['stepId'] == st['stepId'] and x['status'] == 'cancelled' for x in s['cancellations'])
        if not cleared:
            return True
    return False


def retry_availability(s, r, now):
    result = {'canRetry': False, 'canRestart': False, 'reason': 'NOT_RETRYABLE'}
    if not r or r['status'] not in FAILURES:
        return result
    if unresolved_execution(s, r):
        return {**result, 'reason': 'EXECUTION_OUTCOME_UNKNOWN'}
    result['canRestart'] = True
    if r.get('workflowVersion', 1) < 2:
        return {**result, 'canRetry': True, 'reason': None}
    if r.get('failureInputIdentity') != input_identity(s, r):
        return {**result, 'reason': 'RETRY_INPUT_CHANGED'}
    if r.get('deadline') is None or now >= r['deadline']:
        return {**result, 'reason': 'RETRY_EXPIRED'}
    st = s['steps'].get(r.get('failureStepId'))
    if not st or st['kind'] in ('tool', 'skill') or 'input' not in st:
        return result
    if st['kind']=='repair':
        if r.get('repairRecord',{}).get('status')=='succeeded':
            return {**result,'canRetry':True,'reason':None}
        return {**result,'reason':'REPAIR_ALREADY_ATTEMPTED'}
    from focusmate.core.dynamic_coach import remaining_model_reserve
    if r['counts']['model'] >= s['limits']['model'] - remaining_model_reserve(r, st['kind']):
        return {**result, 'reason': 'RETRY_BUDGET_EXHAUSTED'}
    return {**result, 'canRetry': True, 'reason': None}


def resume_step(manager, s, r):
    info = retry_availability(s, r, manager.clock())
    if not info['canRetry']:
        raise ManagerError(info['reason'])
    old = s['steps'][r['failureStepId']]
    resumed = manager._step(s, r, old['kind'], old['payload'])
    if old['kind']=='repair':resumed['sourceStep']=deepcopy(old['sourceStep'])
    resumed.update(retryOf=old['stepId'], frozenInput=deepcopy(old['input']))
    r.update(status='queued', failure=None)
    if s['status'] != 'paused':
        s['status'] = 'active'
    return r


def start_explicit_check(manager, s, r):
    from focusmate.core.dynamic_coach_roles import next_check
    from focusmate.core.session_manager import identity
    pending = {t['subgoalId'] for t in r['targets']}
    ordered = []
    for t in r['targets']:
        goal = s['workGoals'].get(t['subgoalId'])
        if not goal or goal['status'] in ('withdrawn', 'replaced') or goal['versions'][-1]['version'] != t['version']:
            raise ManagerError('STALE_WORK_GOAL')
    while pending:
        ready = [t for t in r['targets'] if t['subgoalId'] in pending and
                 not set(s['workGoals'][t['subgoalId']]['versions'][-1]['dependsOn']) & pending]
        if not ready:
            raise ManagerError('GOAL_DEPENDENCY_CYCLE')
        for t in ready:
            ordered.append(deepcopy(t)); pending.remove(t['subgoalId'])
    r.update(batchId=identity('batch'), decisionId=identity('decision'), acceptedPlan=None, checkQueue=ordered)
    s['currentWorkGoalIds'] = [t['subgoalId'] for t in r['targets']]
    next_check(manager, s, r)

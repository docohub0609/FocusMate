"""Deterministic final acceptance and the single GoalState writer."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from focusmate.contracts.final_oj import FinalOJError
from focusmate.contracts.schema_validation import validate


def checked_request(request):
    try:
        validate('FinalOJRequest', request)
    except Exception:
        raise FinalOJError('INVALID_FINAL_REQUEST') from None
    return deepcopy(request)


class GoalStateManager:
    """Store supplies a transaction snapshot; this service alone decides state writes."""
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def goal_state(self, goal_id):
        return self.store.goal_state(goal_id)

    def report(self, goal_id, request_id):
        return self.store.report(goal_id, request_id)

    def bound_report(self, request, owner):
        return self.store.bound_report(checked_request(request), owner)

    def verify(self, request, *, owner=None, deadline=None):
        request = checked_request(request)
        owner = deepcopy(owner or {'kind': 'independent'})
        if owner != {'kind': 'independent'} and (not {'kind','sessionId','roundId'} <= set(owner) or
                set(owner) - {'kind','sessionId','roundId','codeVersion','problemRef'} or
                owner['kind'] != 'session' or any(not isinstance(owner[k], str) or not owner[k] for k in ('sessionId','roundId'))):
            raise FinalOJError('INVALID_EXECUTION_OWNER')
        return self.store.transact(request, owner, self._decide, deadline=deadline)

    def _decide(self, request, bundle, state):
        now = self.clock()
        report = {'schemaVersion': '1.0.0', 'requestId': request['requestId'],
            'goalContext': {'goalId': request['goalId'], 'scope': 'goal'},
            'submissionId': request['submissionId'], 'goalContractVersion': request['goalContractVersion'],
            'status': 'requestFailed', 'reason': 'SOURCE_UNAVAILABLE', 'updatedAt': now,
            'inputVersionRefs': deepcopy(bundle.get('refs', {}))}
        if bundle.get('error'):
            report['reason'] = bundle['error']
            if bundle['error'] in ('CODE_VERSION_CHANGED','CONTRACT_VERSION_CHANGED','PINNED_MATERIAL_CHANGED'):
                report['status'] = 'superseded'
            return report, None
        if state and state['status'] == 'completed':
            report.update(status='alreadyCompleted', reason='COMPLETION_RETAINED', completionResultId=state['lastVerifierResultId'])
            return report, None
        if state and state['status'] in ('paused','ended'):
            report['reason'] = 'GOAL_NOT_ACTIVE'
            return report, None
        new_state = None
        if not state or state['goalContractVersion'] != request['goalContractVersion']:
            state = {'goalId': request['goalId'], 'schemaVersion': bundle['contract']['schemaVersion'],
                'version': state['version']+1 if state else 1, 'createdAt': state['createdAt'] if state else now,
                'updatedAt': now, 'goalContractVersion': request['goalContractVersion'], 'status': 'active',
                'subgoalStates': [{'subgoalId': s['subgoalId'], 'status': 'notStarted'} for s in bundle['contract'].get('subgoals', [])]}
            new_state = state
        if bundle['submissionStatus'] in ('awaitingAssets','queued','preparing','running'):
            report.update(status='waitingResult', reason='WAITING_FOR_OJ')
            return report, new_state
        if bundle['submissionStatus'] != 'finished':
            report['reason'] = 'SUBMISSION_UNAVAILABLE'
            return report, new_state
        result = bundle['result']
        passed = result['verdict'] == 'AC'
        verifier = {'verifierResultId': 'final-'+uuid4().hex, 'schemaVersion': '1.0.0',
            'createdAt': now, 'goalContext': report['goalContext'], 'verifierType': 'finalOj',
            'status': 'passed' if passed else 'failed', 'evidenceRefs': [result['ojResultId']],
            'reason': '可信OJ通过当前整题要求。' if passed else '可信OJ判定本次提交未通过：'+result['verdict'],
            'inputVersionRefs': report['inputVersionRefs'], 'ojResultId': result['ojResultId']}
        validate('VerifierResult', verifier)
        report.update(status='verified', reason='TRUSTED_AC' if passed else 'TRUSTED_NON_AC', verifierResult=verifier)
        if passed:
            new_state = deepcopy(state)
            new_state.update(status='completed', version=state['version']+1, updatedAt=now,
                             lastVerifierResultId=verifier['verifierResultId'])
        return report, new_state

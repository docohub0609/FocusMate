"""Deterministic final verification steps under the existing session controls."""
from copy import deepcopy
from pathlib import Path

from focusmate.contracts.final_oj import FINAL_STATUSES
from focusmate.contracts.manager import ManagerError
from focusmate.core.final_oj import checked_request


class FinalVerificationSessionMixin:
    def start_final_verification(self, session, request, verifier):
        req = checked_request(request)
        database = str(verifier.store.path.resolve())
        if Path(self.store.path).resolve() == Path(database):
            raise ManagerError('FINAL_REQUIRES_SEPARATE_MANAGER_DATABASE')

        def apply(s):
            self._expire(s)
            if req['goalId'] != s['goalContext']['goalId'] or req['goalContractVersion'] != s['contractVersion']:
                raise ManagerError('FINAL_CONTEXT_MISMATCH')
            old = s['rounds'].get(s['currentRoundId'])
            if old and old['status'] not in ('finished','failed','cancelled','superseded','expired','interrupted'):
                raise ManagerError('ROUND_BUSY')
            r = self._new_round(s, s['currentRoundId'])
            # Replace the unclaimed initial placeholder, never call a model.
            step = next(st for st in s['steps'].values() if st['roundId'] == r['roundId'])
            step.update(kind='final_oj', payload=deepcopy(req), status='waiting')
            r.update(phase='finalVerification', finalRequest=req, finalDatabase=database,
                     finalStepId=step['stepId'], status='waiting_oj')
            return {'sessionId':session, 'roundId':r['roundId'], 'requestId':req['requestId']}
        return self._change(session, {'kind':'final_verification','request':req,'database':database}, apply, key=req['requestId'])

    def advance_final_verification(self, session, verifier, *, round_id=None):
        def apply(s):
            r = s['rounds'].get(round_id or s['currentRoundId'])
            if not r or r.get('phase') != 'finalVerification': return {'status':'idle'}
            if str(verifier.store.path.resolve()) != r['finalDatabase']:
                raise ManagerError('FINAL_DATABASE_CHANGED')
            req = r['finalRequest']
            # Recover an already committed business result even when the session
            # subsequently paused/expired. This reads, never performs new work.
            owner = {'kind':'session','sessionId':session,'roundId':r['roundId'],
                     'codeVersion':r['codeVersion'],'problemRef':s['problemRef']}
            report = verifier.bound_report(req, owner)
            terminal_rounds = ('finished','failed','cancelled','superseded','expired','interrupted')
            if not report or report['status'] not in FINAL_STATUSES:
                self._expire(s)
                if r['status'] in terminal_rounds: return {'status':r['status']}
                if s['status'] == 'paused': return {'status':'paused'}
                if r['codeVersion'] != s['codeVersion'] or req['goalContractVersion'] != s['contractVersion']:
                    self._stop(s,r,'superseded','FINAL_CONTEXT_CHANGED')
                    return {'status':'superseded'}
                if s['currentRoundId'] != r['roundId']:
                    return {'status':'superseded'}
                report = verifier.verify(req, owner=owner, deadline=r['deadline'])
            r['finalVerification'] = deepcopy(report)
            if report['status'] in FINAL_STATUSES:
                s['steps'][r['finalStepId']].update(status='done',result=deepcopy(report))
                if r['status'] not in terminal_rounds:
                    r['status'] = 'finished' if report['status'] in ('verified','alreadyCompleted') else 'failed'
                if s['currentRoundId'] == r['roundId'] and s['status'] != 'paused': s['status'] = 'waiting_user'
            return report
        # The session transaction serializes pause/version changes with this
        # short DB-only step. The business transaction has its own durable receipt.
        return self._change(session, {'kind':'advance_final_verification','roundId':round_id}, apply)

"""T070 dynamic mode: durable steps, with semantic decisions supplied by T071/72."""
from copy import deepcopy
import time
from datetime import datetime, timezone
from focusmate.contracts.tool_calls import validate_call

from focusmate.contracts.manager import ManagerError, safe_payload
from focusmate.core.session_manager import SessionManager, TERMINAL, identity
from focusmate.core.context_builder import hash_content
from focusmate.core.runtime_subgoals import current_goals
from focusmate.core.dynamic_coach_recovery import (remember_failure, retry_availability, resume_step,
    start_explicit_check)

LIMITS = {'seconds': 600, 'model': 7, 'tool': 3, 'execution': 1}


def remaining_model_reserve(round_state, kind):
    if round_state.get("workflowVersion",1)>=3:return 0
    """Calls needed after this step to publish a reviewed response, without raising the cap."""
    if kind == 'verification' and round_state.get('workflowVersion', 1) >= 2:return 0
    if kind in ('diagnosis','verification'):return 2
    if kind=='plan':return 1
    if kind=='review' and round_state.get('prepared',{}).get('candidate',{}).get('next')=='check':
        return 1 if round_state.get('workflowVersion',1)>=2 else 2
    return 0


class DynamicCoachManager(SessionManager):
    def __init__(self, store, *, clock=time.time, workflow_version=3, repair_policy_version=1):
        super().__init__(store, clock=clock)
        if type(workflow_version) is not int or workflow_version not in (1, 2, 3):raise ValueError('INVALID_WORKFLOW_VERSION')
        self.workflow_version = workflow_version
        if repair_policy_version not in (0,1):raise ValueError('INVALID_REPAIR_POLICY_VERSION')
        self.repair_policy_version=repair_policy_version

    def _stop(self,s,r,status,reason,**unused):
        super()._stop(s,r,status,reason,cancel=True)
        remember_failure(s,r)

    def create(self, session_id, goal_context, problem_ref, *, contract_version,
               code_version, allowed_tools=(), limits=None, **unused):
        lim = deepcopy(limits or LIMITS)
        if set(lim) != set(LIMITS) or any(type(v) is not int or v < 1 for v in lim.values()):
            raise ManagerError('INVALID_LIMITS')
        if any(lim[key] > maximum for key,maximum in LIMITS.items()): raise ManagerError('INVALID_LIMITS')
        return self.store.create(session_id, {'schemaVersion':'2.0.0', 'sessionId':session_id,
            'revision':0, 'goalContext':deepcopy(goal_context), 'problemRef':deepcopy(problem_ref),
            'contractVersion':contract_version, 'codeVersion':code_version, 'allowedTools':list(allowed_tools),
            'limits':lim, 'status':'waiting_user', 'messages':[], 'rounds':{}, 'steps':{},
            'currentRoundId':None, 'outbox':[], 'cancellations':[], 'mode':'dynamicCoaching',
            'workGoals':{}, 'checks':{}, 'decisions':[], 'completed':False})

    def _step(self, s, r, kind, payload=None):
        st = {'stepId':identity('step'), 'roundId':r['roundId'], 'kind':kind,
              'payload':deepcopy(payload or {}), 'status':'pending', 'result':None,
              'codeVersion':r['codeVersion'], 'messageSeq':len(s['messages'])}
        s['steps'][st['stepId']] = st
        r['currentStepId'] = st['stepId']
        r['phase'] = kind
        return st

    def _new_round(self, s, previous=None, *, trigger='user', targets=(), direct_check=False):
        r = {'roundId':identity('round'), 'previousRoundId':previous, 'status':'queued',
             'createdAt':self.clock(), 'startedAt':None, 'deadline':None,
             'counts':{'model':0,'tool':0,'execution':0}, 'codeVersion':s['codeVersion'],
             'messageSeq':len(s['messages']), 'failure':None, 'trigger':trigger,
             'targets':deepcopy(list(targets)), 'checkBatchDone':False, 'repaired':False,
             'allowedTools':[], 'checkQueue':[], 'reportRefs':[], 'workflowVersion':self.workflow_version}
        if self.repair_policy_version:r['repairPolicyVersion']=self.repair_policy_version
        s['rounds'][r['roundId']] = r
        s['currentRoundId'] = r['roundId']
        if self.workflow_version >= 2 and direct_check and targets:start_explicit_check(self,s,r)
        else:self._step(s, r, 'plan' if self.workflow_version >= 2 else 'diagnosis')
        if s['status'] != 'paused': s['status'] = 'active'
        return r

    def _expire(self, s):
        for r in s['rounds'].values():
            if r['status'] not in TERMINAL and r['deadline'] is not None and self.clock() >= r['deadline']:
                self._stop(s, r, 'expired', 'DEADLINE_EXCEEDED')

    def command(self, session, request_id, kind, payload, *, expected_code_version='unbound'):
        fields = {'ask':{'text','replyTo'}, 'message':{'text','replyTo'},
            'check':{'text','subgoalIds'}, 'pause':set(), 'resume':set(), 'retry':set(), 'restart':set(),
            'code_changed':{'codeVersion'}, 'follow_up':{'eventId','codeVersion','targets','evidence'},
            'final_completed':{'resultId'}, 'binding_changed':set(),
            'grant_permission':{'tools','stepId'}, 'stop_run':set(), 'revoke_execution':set()}
        if (kind not in fields or not isinstance(payload,dict) or set(payload)-fields[kind]
                or not isinstance(request_id,str) or not 1 <= len(request_id) <= 256):
            raise ManagerError('INVALID_COMMAND')
        if kind in ('ask','message') and (not isinstance(payload.get('text'),str) or not payload['text'].strip()):
            raise ManagerError('EMPTY_MESSAGE')
        if kind == 'code_changed' and payload.get('codeVersion') is not None and (
                type(payload['codeVersion']) is not int or payload['codeVersion'] < 1):
            raise ManagerError('INVALID_VERSION')
        def apply(s):
            if expected_code_version != 'unbound' and s['codeVersion'] != expected_code_version:
                raise ManagerError('CODE_VERSION_CHANGED')
            self._expire(s)
            r = s['rounds'].get(s['currentRoundId'])
            if kind == 'code_changed':
                if s['codeVersion'] != payload.get('codeVersion'):
                    s['codeVersion'] = payload.get('codeVersion')
                    for goal in s['workGoals'].values():
                        if self.workflow_version < 2 and goal['status']=='verified' and any('codeVersion' in s['checks'][ref]['verifierResult']['inputVersionRefs']
                                for ref in goal['verificationRefs'] if ref in s['checks']):
                            goal['status']='active'
                    if r and r['status'] not in TERMINAL: self._stop(s,r,'superseded','CODE_CHANGED')
            elif kind == 'binding_changed':
                s['status'] = 'paused'
                if r and r['status'] not in TERMINAL: self._stop(s,r,'superseded','CONTRACT_CHANGED')
            elif kind == 'final_completed':
                s['completed'] = True
                if r and r['trigger'] not in ('user','retry') and r['status'] not in TERMINAL:
                    self._stop(s,r,'superseded','GOAL_COMPLETED')
            elif kind == 'pause':
                s['status'] = 'paused'
                if r and r.get('phase')=='repair' and r['status'] not in TERMINAL:
                    from focusmate.core.dynamic_coach_repair import interrupt_repair
                    interrupt_repair(r)
                    self._stop(s,r,'interrupted','REPAIR_INTERRUPTED')
            elif kind == 'resume':
                s['status'] = 'active' if r and r['status'] not in TERMINAL else 'waiting_user'
                for st in list(s['steps'].values()):
                    if r and st['roundId'] == r['roundId'] and st['status'] == 'retained' and r['status'] not in TERMINAL:
                        st['status'] = 'done'
                        self._accept(s,r,st,st['result'])
            elif kind in ('stop_run','revoke_execution'):
                if r and r['status'] not in TERMINAL: self._stop(s,r,'cancelled','USER_CANCELLED')
                s['allowedTools'] = []
            elif kind == 'grant_permission':
                names = payload.get('tools')
                pending=next((x for x in s['steps'].values() if r and x['roundId']==r['roundId'] and x['kind']=='tool' and x['status']=='pending'),None)
                if not r or r['status'] != 'waiting_permission' or names != r.get('permissionTools') or not pending or payload.get('stepId')!=pending['stepId']:
                    raise ManagerError('PERMISSION_MISMATCH')
                r['allowedTools'] = list(names)
                r['status'] = 'processing'
                r.pop('permissionTools',None)
            elif kind == 'retry':
                if not r or r['status'] not in {'failed','expired','interrupted'}: raise ManagerError('NOT_RETRYABLE')
                if r.get('workflowVersion',1) >= 2:
                    r=resume_step(self,s,r)
                    # Explicitly resume an automatic round without inheriting
                    # old full-answer permission. A user's existing round keeps
                    # its current request authorization, input and budget.
                    if r['trigger']!='user':r['trigger']='retry'
                else:r = self._new_round(s,r['roundId'],trigger='retry',targets=r['targets'])
            elif kind == 'restart':
                info=retry_availability(s,r,self.clock())
                if not info['canRestart']:raise ManagerError(info['reason'])
                r=self._new_round(s,r['roundId'],trigger='retry')
            elif kind == 'follow_up':
                if s['status'] == 'paused' or s['completed'] or s['codeVersion'] != payload.get('codeVersion'):
                    return {'status':'ignored'}
                targets = [t for t in payload.get('targets',[]) if t['subgoalId'] in s['workGoals']
                    and s['workGoals'][t['subgoalId']]['status'] in ('active','verified')
                    and s['workGoals'][t['subgoalId']]['versions'][-1]['version'] == t['version']]
                if self.workflow_version>=3:
                    from focusmate.core.coach_condition_context import invalid_condition_goal_ids
                    blocked=invalid_condition_goal_ids(s)
                    targets=[t for t in targets if t['subgoalId'] not in blocked]
                if not targets: return {'status':'ignored'}
                if r and r['status'] not in TERMINAL: self._stop(s,r,'superseded','NEW_EVIDENCE')
                r = self._new_round(s,s['currentRoundId'],trigger='run',targets=targets)
                r['triggerEvidence'] = deepcopy(payload['evidence'])
                if r['workflowVersion'] >= 2 and payload['evidence'].get('completed'):
                    for st in s['steps'].values():
                        if st['roundId']==r['roundId'] and st['status']=='pending':st['status']='cancelled'
                    start_explicit_check(self,s,r)
            else:
                if payload.get('replyTo') and not any(a['actionId']==payload['replyTo'] for a in s['outbox']):
                    raise ManagerError('UNKNOWN_ACTION')
                targets = payload.get('subgoalIds',[])
                if (not isinstance(targets,list) or any(not isinstance(t,str) or t not in s['workGoals'] for t in targets)
                        or len(set(targets))!=len(targets)):
                    raise ManagerError('UNKNOWN_SUBGOAL')
                s['messages'].append({'sequence':len(s['messages'])+1, 'receivedAt':self.clock(),
                    'historySequence':s['revision']+1,'codeVersion':s['codeVersion'],
                    'text':payload.get('text','请检查所选问题。'), 'replyTo':payload.get('replyTo')})
                if r and r['status'] not in TERMINAL: self._stop(s,r,'superseded','NEW_MESSAGE')
                r = self._new_round(s,s['currentRoundId'],targets=[{'subgoalId':key,
                    'version':s['workGoals'][key]['versions'][-1]['version']} for key in targets],
                    direct_check=kind=='check' and not payload.get('text'))
            return {'sessionId':session,'roundId':r['roundId'] if r else None,'status':s['status']}
        return self._change(session,{'kind':kind,'payload':payload,'expectedCodeVersion':expected_code_version},apply,request_id)

    def recover(self, session):
        def apply(s):
            self._expire(s)
            for st in s['steps'].values():
                if st['status'] == 'running' and self.clock() >= st['leaseUntil']:
                    r = s['rounds'][st['roundId']]
                    if st['kind']=='repair':
                        from focusmate.core.dynamic_coach_repair import interrupt_repair
                        interrupt_repair(r)
                    if r['status'] not in TERMINAL: self._stop(s,r,'interrupted','STEP_OWNER_LOST')
                    st['status'] = 'interrupted'
            return {'status':s['status']}
        return self._change(session,{'kind':'recover'},apply)

    def claim(self, session, worker, *, materials, lease_seconds=120):
        safe_payload(materials)
        def apply(s):
            self._expire(s)
            r = s['rounds'].get(s['currentRoundId'])
            if s['status'] == 'paused' or not r or r['status'] in TERMINAL or r.get('permissionTools'): return None
            if s['completed'] and r['trigger'] not in ('user','retry'):
                self._stop(s,r,'superseded','GOAL_COMPLETED');return None
            if any(st['roundId']==r['roundId'] and st['status']=='running' for st in s['steps'].values()): return None
            st = next((st for st in s['steps'].values() if st['roundId']==r['roundId'] and st['status']=='pending'),None)
            if not st: return None
            if (materials.get('codeVersion',s['codeVersion'])!=s['codeVersion'] or
                    materials.get('contractVersion',s['contractVersion'])!=s['contractVersion'] or
                    materials.get('messageSeq',len(s['messages']))!=len(s['messages'])):
                return None
            if r['deadline'] is None:
                r['startedAt'] = self.clock()
                r['deadline'] = self.clock()+s['limits']['seconds']
            metric = 'tool' if st['kind'] in ('tool','skill') else 'model'
            reserve = remaining_model_reserve(r,st['kind'])
            if st['kind']!='repair' and r['counts'][metric] >= s['limits'][metric]-reserve:
                self._stop(s,r,'failed','BUDGET_EXHAUSTED')
                return None
            if 'frozenInput' in st and hash_content(materials)!=hash_content(st['frozenInput']):
                self._stop(s,r,'superseded','RETRY_INPUT_CHANGED');return None
            st['input'] = deepcopy(st.get('frozenInput',materials))
            st['inputHash'] = hash_content(st['input'])
            st['workGoalVersions']={k:g['versions'][-1]['contentHash'] for k,g in s['workGoals'].items()}
            if st['kind']=='repair':st['workGoalStates']={k:g['status'] for k,g in s['workGoals'].items()}
            st.update(status='running',owner=worker,token=identity('lease'),
                leaseUntil=min(r['deadline'],self.clock()+lease_seconds),deadline=r['deadline'])
            # Repair owns its individual requests, including any required review.
            if st['kind']!='repair':r['counts'][metric] += 1
            if st['kind']=='tool' and st['payload']['toolName'] in ('run_public_samples','run_local_code'):
                if r['counts']['execution']>=s['limits']['execution']:
                    self._stop(s,r,'failed','EXECUTION_BUDGET_EXHAUSTED')
                    st['status']='cancelled'
                    return None
                r['counts']['execution']+=1
            r['status'] = 'processing'
            return deepcopy(st)
        return self._change(session,{'kind':'claim','worker':worker,'materialsHash':hash_content(materials)},apply)

    def heartbeat(self, session, step_id, token):
        def apply(s):
            st = s['steps'][step_id]
            if st.get('token') != token or st['status'] != 'running' or self.clock() >= st['leaseUntil']:
                raise ManagerError('STALE_OWNER')
            r = s['rounds'][st['roundId']]
            if r['status'] in TERMINAL or self.clock() >= r['deadline']: raise ManagerError('STALE_OWNER')
            st['leaseUntil'] = min(r['deadline'],self.clock()+120)
            return st['leaseUntil']
        return self._change(session,{'kind':'heartbeat','stepId':step_id},apply)

    def complete(self, session, step_id, token, result):
        safe_payload(result)
        def apply(s):
            st = s['steps'].get(step_id)
            if not st or st.get('token')!=token or st['status']!='running' or self.clock()>=st['leaseUntil']:
                raise ManagerError('STALE_OWNER')
            r = s['rounds'][st['roundId']]
            st.update(result=deepcopy(result),status='done',finishedAt=self.clock())
            self._expire(s)
            if r['status'] in TERMINAL: return {'status':'retained'}
            if 'workGoalVersions' in st and st['workGoalVersions'] != {k:g['versions'][-1]['contentHash'] for k,g in s['workGoals'].items()}:
                self._stop(s,r,'superseded','WORK_GOALS_CHANGED');return {'status':'retained'}
            if st['kind']=='repair' and st.get('workGoalStates')!={k:g['status'] for k,g in s['workGoals'].items()}:
                self._stop(s,r,'superseded','WORK_GOALS_CHANGED');return {'status':'retained'}
            if s['status']=='paused':
                st['status']='retained'
                return {'status':'retained_while_paused'}
            self._accept(s,r,st,result)
            return {'status':r['status'],'roundId':r['roundId']}
        completion_key=('repair-complete:'+step_id+':'+token
            if self.read(session)['steps'].get(step_id,{}).get('kind')=='repair' else None)
        return self._change(session,{'kind':'complete','stepId':step_id,'token':token,'result':result},apply,completion_key)

    def _accept(self, s, r, st, result):
        if st['kind']=='repair':
            from focusmate.core.dynamic_coach_repair import accept_repair
            accept_repair(self,s,r,st,result)
            return
        if result.get('kind')=='failure':
            if r.get('repairPolicyVersion'):
                from focusmate.core.dynamic_coach_repair import schedule_repair
                if schedule_repair(self,s,r,st,result):return
                self._stop(s,r,'failed',result['code'])
                return
            if (r.get('workflowVersion',1)>=2 and result.get('retryable') and st['kind'] in ('plan','review','verification')
                    and not st.get('payload',{}).get('networkRetried') and
                    r['counts']['model']<s['limits']['model']-remaining_model_reserve(r,st['kind'])):
                following=self._step(s,r,st['kind'],{**st['payload'],'networkRetried':True})
                following['frozenInput']=deepcopy(st['input']);return
            if (result.get('correctable') and st['kind'] in ('diagnosis','plan','review','verification') and
                    st['kind'] not in r.get('formatRepairs',[]) and
                    r['counts']['model']<s['limits']['model']-remaining_model_reserve(r,st['kind'])):
                r.setdefault('formatRepairs',[]).append(st['kind'])
                following=self._step(s,r,st['kind'],st['payload'])
                following['payload']['repairReason']=result['code']
                following['payload']['formatError']=result.get('formatError')
                following['payload']['responseDiagnostic']=deepcopy(result.get('responseDiagnostic'))
                following['payload']['rejectedResponseInfo']=deepcopy(result.get('rejectedResponseInfo'))
                following['payload']['previousRejectedResponse']=result.get('responseText') or {
                    k:v for k,v in result.get('rejectedResult',{}).items() if k in ('kind','value','candidateGoals','actionReview','goalReview')}
                return
            self._stop(s,r,'failed',result['code'])
            return
        if result.get('kind')=='toolRequest':
            if st['kind'] not in ('diagnosis','plan','verification'): raise ManagerError('TOOL_PHASE_FORBIDDEN')
            call=deepcopy(result['call'])
            if call.get('toolName')=='read_skill':
                from jsonschema import Draft202012Validator
                from focusmate.core.dynamic_coach_skills import PARAMETERS
                Draft202012Validator(PARAMETERS).validate(call['arguments'])
                if any(x['kind']=='skill' and x['roundId']==r['roundId'] for x in s['steps'].values()):
                    raise ManagerError('SKILL_ALREADY_READ')
                if r['counts']['tool']>=s['limits']['tool']:raise ManagerError('TOOL_BUDGET_EXHAUSTED')
                skill=self._step(s,r,'skill',call)
                skill['resumeKind']=st['kind'];skill['resumePayload']=deepcopy(st['payload'])
                return
            validate_call(call)
            if any(x['kind']=='tool' and x['roundId']==r['roundId'] and x['payload']['callId']==call['callId'] for x in s['steps'].values()):
                raise ManagerError('DUPLICATE_TOOL_CALL')
            if call['arguments'].get('codeVersion',r['codeVersion'])!=r['codeVersion']:
                raise ManagerError('CODE_VERSION_CHANGED')
            if r['counts']['tool']>=s['limits']['tool']: raise ManagerError('TOOL_BUDGET_EXHAUSTED')
            execution=call['toolName'] in ('run_public_samples','run_local_code')
            if execution and r['counts']['execution']>=s['limits']['execution']:
                raise ManagerError('EXECUTION_BUDGET_EXHAUSTED')
            tool=self._step(s,r,'tool',call)
            tool['resumeKind']=st['kind'];tool['resumePayload']=deepcopy(st['payload'])
            # Each tool has an immutable gateway context. The round owns aggregate quotas.
            tool['toolContext']={'loopId':tool['stepId'],'goalContext':deepcopy(s['goalContext']),
                'problemRef':deepcopy(s['problemRef']),'allowedCodeVersions':[r['codeVersion']] if r['codeVersion'] else [],
                'allowedTools':[call['toolName']],
                'deadlineAt':datetime.fromtimestamp(r['deadline'],timezone.utc).isoformat()}
            if execution:
                r['permissionTools']=[call['toolName']];r['status']='waiting_permission'
            return
        if st['kind']=='skill':
            if result.get('kind')!='skill' or result.get('callId')!=st['payload']['callId']:
                raise ManagerError('SKILL_RESULT_MISMATCH')
            self._step(s,r,st['resumeKind'],st['resumePayload'])
            return
        if st['kind']=='tool':
            receipt=result.get('result',{})
            if any(receipt.get(k)!=v for k,v in {'callId':st['payload']['callId'],
                'toolName':st['payload']['toolName'],'loopId':st['toolContext']['loopId'],
                'goalContext':s['goalContext'],'problemRef':s['problemRef']}.items()):
                raise ManagerError('TOOL_RESULT_MISMATCH')
            if receipt.get('status')!='succeeded':
                if receipt.get('status')=='running': st['status']='waiting'
                self._stop(s,r,'interrupted','TOOL_NOT_FINISHED')
            else:self._step(s,r,st['resumeKind'],st['resumePayload'])
            return
        # Validation and model-role transitions live together and are shared by the worker and tests.
        from focusmate.core.dynamic_coach_roles import accept_result
        accept_result(self,s,r,st,result)

    def repair_checkpoint(self,session,step_id,token,record):
        from focusmate.core.dynamic_coach_repair import owned_repair
        safe_payload(record)
        def apply(s):
            r,_=owned_repair(self,s,step_id,token)
            old=r['repairRecord']
            from focusmate.core.repair_agent import TERMINAL as REPAIR_TERMINAL
            if old['status'] in REPAIR_TERMINAL and old!=record:
                raise ManagerError('REPAIR_RECORD_TERMINAL')
            if not old['modelCalls']<=record['modelCalls']<=2:
                raise ManagerError('REPAIR_BUDGET_CONFLICT')
            if record['repairId']!=old['repairId'] or record['owner']!=old['owner']:
                raise ManagerError('REPAIR_SCOPE_VIOLATION')
            if old.get('candidate') is not None and old['candidate']!=record.get('candidate'):
                raise ManagerError('REPAIR_CANDIDATE_CONFLICT')
            for key in ('materials','original','failure'):
                if old[key]!=record[key]:raise ManagerError('REPAIR_SCOPE_VIOLATION')
            for key in ('modelPacket','modelPacketHash'):
                if old.get(key)!=record.get(key):raise ManagerError('REPAIR_SCOPE_VIOLATION')
            r['repairRecord']=deepcopy(record)
            return deepcopy(record)
        return self._change(session,{'kind':'repair_checkpoint','stepId':step_id,'record':record},apply)

    def charge_repair_model(self,session,step_id,token,purpose):
        from focusmate.core.dynamic_coach_repair import owned_repair
        def apply(s):
            r,_=owned_repair(self,s,step_id,token)
            if r['counts']['model']>=s['limits']['model']:raise ManagerError('REPAIR_BUDGET_EXHAUSTED')
            r['counts']['model']+=1
        return self._change(session,{'kind':'repair_model','stepId':step_id,'purpose':purpose},apply)

    def targets(self, session):
        s=self.read(session)
        focus=s.get('currentWorkGoalIds') if self.workflow_version>=2 else None
        goals=current_goals(s['workGoals'])
        if focus is None:goals=[g for g in goals if g['status']=='active']
        else:goals=[g for g in goals if g['status'] in ('active','verified') and g['subgoalId'] in focus]
        if self.workflow_version>=3:
            from focusmate.core.coach_condition_context import invalid_condition_goal_ids
            blocked=invalid_condition_goal_ids(s)
            goals=[g for g in goals if g['subgoalId'] not in blocked]
        return [{'subgoalId':g['subgoalId'],'version':g['version']} for g in goals]

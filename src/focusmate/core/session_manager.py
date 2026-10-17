"""Durable, deterministic session control. No semantic routing or GoalState writes."""
from copy import deepcopy
import time
from uuid import uuid4
from focusmate.contracts.manager import ManagerError, safe_payload
from focusmate.contracts.schema_validation import validate
from focusmate.contracts.tool_calls import validate_context, validate_call, TOOL_PARAMETERS
from focusmate.core.final_session import FinalVerificationSessionMixin

TERMINAL={'finished','failed','cancelled','superseded','expired','interrupted'}
EXECUTION={'run_public_samples','run_local_code'}
DEFAULT_LIMITS={'seconds':120,'model':7,'tool':3,'execution':1}


def identity(prefix): return prefix+'-'+uuid4().hex


class SessionManager(FinalVerificationSessionMixin):
    def __init__(self, store, *, clock=time.time):
        self.store=store; self.clock=clock

    def create(self, session_id, goal_context, problem_ref, *, contract_version, code_version, allowed_tools, limits=None, mode='legacy'):
        from datetime import datetime, timezone
        ctx={'loopId':session_id,'goalContext':goal_context,'problemRef':problem_ref,
             'allowedCodeVersions':[code_version] if code_version is not None else [],'allowedTools':allowed_tools,'deadlineAt':datetime.now(timezone.utc).isoformat()}
        if mode not in ('legacy','diagnosis','coaching') or code_version is None and (mode not in ('diagnosis','coaching') or any(t!='read_submission_result' for t in allowed_tools)):raise ManagerError('INVALID_CONTEXT')
        try: validate_context(ctx)
        except Exception: raise ManagerError('INVALID_CONTEXT') from None
        if type(contract_version)!=int or contract_version<1: raise ManagerError('INVALID_CONTRACT_VERSION')
        lim=deepcopy(DEFAULT_LIMITS if limits is None else limits)
        if set(lim)!=set(DEFAULT_LIMITS) or any(type(v)!=int or v<1 for v in lim.values()): raise ManagerError('INVALID_LIMITS')
        if lim['tool']>3 or lim['execution']>1: raise ManagerError('TOOL_LIMIT_UNSUPPORTED')
        return self.store.create(session_id,{'schemaVersion':'1.0.0','sessionId':session_id,'revision':0,
            'goalContext':deepcopy(goal_context),'problemRef':deepcopy(problem_ref),'contractVersion':contract_version,
            'codeVersion':code_version,'allowedTools':list(allowed_tools),'limits':lim,'status':'active',
            'messages':[],'rounds':{},'steps':{},'currentRoundId':None,'outbox':[],'cancellations':[], 'mode':mode})

    def read(self, session): return self.store.read(session)

    def pin_materials(self,session,materials,budget):
        safe_payload(materials)
        def apply(s):
            r=s['rounds'].get(s['currentRoundId'])
            if not r:return None
            bundle={'materials':deepcopy(materials),'budget':deepcopy(budget)}
            if 'inputBundle' in r and r['inputBundle']!=bundle:raise ManagerError('MATERIALS_CHANGED')
            r['inputBundle']=bundle
            return None
        return self._change(session,{'kind':'pin_materials','materials':materials,'budget':budget},apply)

    def _change(self, session, cmd, fn, key=None):
        safe_payload(cmd)
        return self.store.change(session,key or identity('internal'),cmd,fn)

    def _step(self,s,r,kind,payload=None):
        if kind=='model' and r.get('phase')=='verification':kind='verification_prepare'
        if kind=='model' and s.get('mode') in ('diagnosis','coaching'):
            kind='action_prepare' if r.get('phase')=='action' else 'diagnosis_prepare'
        step={'stepId':identity('step'),'roundId':r['roundId'],'kind':kind,'payload':deepcopy(payload or {}),
              'status':'pending','messageSeq':len(s['messages']),'codeVersion':r['codeVersion'],
              'deadline':r['deadline'],'result':None}
        s['steps'][step['stepId']]=step
        # Permission is a separate pending obligation: work finishing or a new
        # message must not implicitly grant it by replacing the visible status.
        r['status']='waiting_permission' if r.get('permissionTools') else ('waiting_tool' if kind=='tool' else 'processing')
        return step

    def _new_round(self,s,previous=None):
        r={'roundId':identity('round'),'previousRoundId':previous,'status':'processing',
           'codeVersion':s['codeVersion'],'createdAt':self.clock(),'deadline':self.clock()+s['limits']['seconds'],
           'counts':{'model':0,'tool':0,'execution':0},'candidate':None,'checked':False,'messageSeq':len(s['messages']),
           'allowedTools':list(s['allowedTools']),'failure':None}
        s['rounds'][r['roundId']]=r; s['currentRoundId']=r['roundId']; self._step(s,r,'model')
        return r

    def _stop(self,s,r,status,reason,*,cancel=False):
        r.update(status=status,failure=reason)
        for step in s['steps'].values():
            if step['roundId']!=r['roundId']: continue
            if step['status']=='pending': step['status']='cancelled'
            if step['kind']=='final_oj' and step['status']=='waiting': step['status']='cancelled'
            if cancel and step['kind']=='tool' and step['payload'].get('toolName') in EXECUTION and step['status'] in ('running','waiting'):
                if not any(x['stepId']==step['stepId'] for x in s['cancellations']):
                    s['cancellations'].append({'stepId':step['stepId'],'roundId':r['roundId'],'status':'pending','reason':reason})

    def _expire(self,s):
        for r in s['rounds'].values():
            if r['status'] not in TERMINAL and self.clock()>=r['deadline']:
                self._stop(s,r,'expired','DEADLINE_EXCEEDED',cancel=True)

    def command(self,session,request_id,kind,payload):
        try:validate('SessionManagerCommand',{'schemaVersion':'1.0.0','sessionId':session,'requestId':request_id,'kind':kind,'payload':payload})
        except Exception:raise ManagerError('INVALID_COMMAND') from None
        fields={'ask':{'text','replyTo'},'message':{'text','replyTo'},'retry':set(),'pause':set(),'resume':set(),
                'code_changed':{'codeVersion'},'stop_run':set(),'revoke_execution':set(),
                'request_permission':{'tools'},'grant_permission':{'tools'},'verification':{'actionId','materialRefs','codeVersion','subgoalId'}}
        if not isinstance(request_id,str) or not request_id or kind not in fields or not isinstance(payload,dict) or set(payload)-fields[kind]:
            raise ManagerError('INVALID_COMMAND')
        if kind in ('ask','message') and (not isinstance(payload.get('text'),str) or not payload['text'].strip()): raise ManagerError('EMPTY_MESSAGE')
        if kind=='code_changed' and (type(payload.get('codeVersion'))!=int or payload['codeVersion']<1): raise ManagerError('INVALID_VERSION')
        def apply(s):
            self._expire(s); r=s['rounds'].get(s['currentRoundId'])
            if kind in ('ask','message'):
                if payload.get('replyTo') and not any(a['actionId']==payload['replyTo'] for a in s['outbox']): raise ManagerError('UNKNOWN_ACTION')
                s['messages'].append({'sequence':len(s['messages'])+1,'receivedAt':self.clock(),**deepcopy(payload)})
                if r and r.get('phase') in ('verification','finalVerification') and r['status'] not in TERMINAL:
                    self._stop(s,r,'superseded','VERIFICATION_MESSAGE_CHANGED')
                if r is None or r['status'] in TERMINAL:
                    # A message is an explicit user request; code saves alone never start one.
                    r=self._new_round(s,s['currentRoundId'])
                    if s['status']!='paused': s['status']='active'
                else:
                    r['checked']=False; r['candidate']=None; r['messageSeq']=len(s['messages'])
                    r.pop('phase',None)
                    r.pop('plannerReorientation',None)
                    for st in s['steps'].values():
                        if st['roundId']==r['roundId'] and st['status']=='pending': st['status']='superseded'
                    active=any(st['roundId']==r['roundId'] and st['status'] in ('running','waiting','pending') for st in s['steps'].values())
                    if not active: self._step(s,r,'model')
            elif kind=='retry':
                if not r or r['status'] not in TERMINAL-{'finished'}: raise ManagerError('NOT_RETRYABLE')
                r=self._new_round(s,r['roundId'])
                if s['status']!='paused':s['status']='active'
            elif kind=='pause': s['status']='paused'
            elif kind=='resume': s['status']='active' if r and r['status'] not in TERMINAL else 'waiting_user'
            elif kind=='code_changed':
                s['codeVersion']=payload['codeVersion']
                if r and r['status'] not in TERMINAL: self._stop(s,r,'superseded','CODE_CHANGED')
            elif kind in ('stop_run','revoke_execution'):
                if kind=='revoke_execution': s['allowedTools']=[t for t in s['allowedTools'] if t not in EXECUTION]
                if r and r['status'] not in TERMINAL: self._stop(s,r,'cancelled',kind.upper(),cancel=True)
                # Also cancel historical executions retained after a version change.
                for old in s['rounds'].values():
                    if any(st['roundId']==old['roundId'] and st['kind']=='tool' and st['status'] in ('running','waiting') for st in s['steps'].values()):
                        self._stop(s,old,old['status'] if old['status'] in TERMINAL else 'cancelled',kind.upper(),cancel=True)
            elif kind in ('request_permission','grant_permission'):
                names=payload.get('tools')
                if not isinstance(names,list) or not names or any(t not in TOOL_PARAMETERS for t in names): raise ManagerError('INVALID_PERMISSION')
                if not r or r['status'] in TERMINAL: raise ManagerError('NO_ACTIVE_ROUND')
                if kind=='request_permission': r.update(status='waiting_permission',permissionTools=names)
                else:
                    if r['status']!='waiting_permission' or names!=r.get('permissionTools'): raise ManagerError('PERMISSION_MISMATCH')
                    # Immutable tool context cannot silently grow within an already-used loop.
                    old=r
                    self._stop(s,r,'superseded','PERMISSION_CHANGED')
                    s['allowedTools']=list(dict.fromkeys(s['allowedTools']+names)); r=self._new_round(s,r['roundId'])
                    r.update(deadline=old['deadline'],counts=deepcopy(old['counts']))
                    for st in s['steps'].values():
                        if st['roundId']==r['roundId']:st['deadline']=r['deadline']
                    if old.get('phase')=='verification':
                        for st in s['steps'].values():
                            if st['roundId']==r['roundId']:st['status']='superseded'
                        r.update(phase='verification',verificationRequest=deepcopy(old['verificationRequest']),
                            verificationEvidenceRoundIds=old.get('verificationEvidenceRoundIds',[])+[old['roundId']])
                        if 'inputBundle' in old:r['inputBundle']=deepcopy(old['inputBundle'])
                        self._step(s,r,'verification_prepare')
            elif kind=='verification':
                a=next((a for a in s['outbox'] if a['actionId']==payload.get('actionId')),None)
                if not a or not isinstance(payload.get('materialRefs'),list) or not payload['materialRefs'] or any(not isinstance(x,str) or not x for x in payload['materialRefs']): raise ManagerError('INVALID_VERIFICATION_ASSOCIATION')
                if payload.get('codeVersion')!=s['codeVersion']: raise ManagerError('OLD_VERIFICATION_EVIDENCE')
                if 'subgoalId' in payload:
                    action=a.get('learningAction',{})
                    if (action.get('contractVersion')!=s['contractVersion'] or action.get('goalContext',{}).get('goalId')!=s['goalContext']['goalId']
                            or action.get('followUp',{}).get('kind')!='awaitEvidence'
                            or payload['subgoalId'] not in action.get('followUp',{}).get('criterionIds',[])
                            or payload['subgoalId'] not in action.get('subgoalIds',[])
                            or s['goalContext'].get('scope')=='subgoal' and s['goalContext']['subgoalId']!=payload['subgoalId']):
                        raise ManagerError('INVALID_VERIFICATION_TARGET')
                if r and r['status'] not in TERMINAL: raise ManagerError('ROUND_BUSY')
                r=self._new_round(s,s['currentRoundId'])
                for st in s['steps'].values():
                    if st['roundId']==r['roundId']: st['status']='superseded'
                if 'subgoalId' in payload:
                    r.update(phase='verification',verificationRequest=deepcopy(payload))
                    self._step(s,r,'verification_prepare',payload)
                else:self._step(s,r,'verify',payload)
                if s['status']!='paused':s['status']='active'
            return {'sessionId':session,'roundId':r['roundId'] if r else None,'status':s['status']}
        return self._change(session,{'kind':kind,'payload':payload},apply,request_id)

    def claim(self,session,worker,*,lease_seconds=120):
        if not worker or lease_seconds<=0: raise ManagerError('INVALID_OWNER')
        def apply(s):
            self._expire(s)
            if s['status']=='paused': return None
            r=s['rounds'].get(s['currentRoundId'])
            if not r or r['status'] in TERMINAL or r.get('permissionTools'): return None
            if any(x['roundId']==r['roundId'] and x['status'] in ('running','waiting') for x in s['steps'].values()):return None
            for st in s['steps'].values():
                if st['roundId']!=r['roundId'] or st['status']!='pending':continue
                local=st['kind'] in ('diagnosis_prepare','diagnosis_skill','diagnosis_accept','action_prepare','action_accept','verification_prepare','verification_accept')
                if s.get('mode')=='coaching' and st['kind'].startswith('diagnosis_') and not local and r['counts']['model']>=s['limits']['model']-2:
                    self._stop(s,r,'failed','ACTION_REVIEW_BUDGET_RESERVED');return None
                metric='tool' if st['kind']=='tool' else 'model'
                execution=st['kind']=='tool' and st['payload']['toolName'] in EXECUTION
                if not local and r['counts'][metric]>=s['limits'][metric] or execution and r['counts']['execution']>=s['limits']['execution']:
                    self._stop(s,r,'failed','BUDGET_EXHAUSTED',cancel=True);return None
                if st['kind']=='tool' and st['payload']['toolName'] not in s['allowedTools']:
                    self._stop(s,r,'failed','PERMISSION_REVOKED');return None
                if not local:r['counts'][metric]+=1
                if execution:r['counts']['execution']+=1
                st.update(status='running',owner=worker,token=identity('lease'),leaseUntil=min(r['deadline'],self.clock()+lease_seconds),messageSeq=len(s['messages']))
                return deepcopy(st)
            return None
        return self._change(session,{'kind':'claim','worker':worker},apply)

    def complete(self,session,step_id,token,result):
        safe_payload(result)
        try:validate('ManagerStepResult',result)
        except Exception:raise ManagerError('INVALID_STEP_RESULT') from None
        def apply(s):
            st=s['steps'].get(step_id)
            if not st or st.get('token')!=token or st['status'] not in ('running','waiting') or self.clock()>=st['leaseUntil']: raise ManagerError('STALE_OWNER')
            r=s['rounds'][st['roundId']]
            kind=result.get('kind')
            allowed={'model':{'candidate','tool','failure'},'tool':{'tool_result','failure'},'check':{'checked','failure'},'verify':{'verified','failure'}}
            for name in ('prepare','model','skill','stage','progress','summary','accept'):
                allowed['diagnosis_'+name]={'diagnosis_next','diagnosis_ready','tool','failure'}
            for name in ('prepare','model','review','repair','summary','accept'):
                allowed['action_'+name]={'diagnosis_next','action_ready','tool','failure'}
            for name in ('prepare','model','accept'):
                allowed['verification_'+name]={'verification_next','verification_ready','tool','failure'}
            if kind not in allowed[st['kind']]: raise ManagerError('INVALID_STEP_RESULT')
            if kind=='tool_result':
                value=result.get('result',{});call=st['payload']
                if any(value.get(k)!=v for k,v in {'callId':call['callId'],'loopId':r['roundId'],'toolName':call['toolName'],'goalContext':s['goalContext'],'problemRef':s['problemRef']}.items()):raise ManagerError('TOOL_RESULT_MISMATCH')
                if value.get('codeVersion',r['codeVersion'])!=call['arguments'].get('codeVersion',value.get('codeVersion',r['codeVersion'])):raise ManagerError('TOOL_VERSION_MISMATCH')
                if value.get('status') not in ('succeeded','failed','rejected','running'):raise ManagerError('INVALID_TOOL_STATUS')
            if kind=='candidate' and (not isinstance(result.get('content'),str) or not result['content'].strip()):raise ManagerError('EMPTY_CANDIDATE')
            if kind in ('checked','verified') and result.get('source')!='developerFixture': raise ManagerError('ROLE_NOT_INTEGRATED')
            st.update(result=deepcopy(result),status='done',finishedAt=self.clock())
            self._expire(s)
            if kind=='tool_result' and result['result']['status']=='running':
                st['status']='waiting'
                if r['status'] not in TERMINAL:
                    r['status']='waiting_permission' if r.get('permissionTools') else 'waiting_tool'
                return {'status':'waiting'}
            if r['status'] in TERMINAL:return {'status':'retained','roundId':r['roundId']}
            if st['messageSeq']!=len(s['messages']):
                self._step(s,r,'model');return {'status':'superseded_response'}
            if kind=='failure': self._stop(s,r,'failed',result.get('code','STEP_FAILED'))
            elif kind=='verification_next':
                if r.get('phase')!='verification':raise ManagerError('INVALID_VERIFICATION_PHASE')
                self._step(s,r,result['nextKind'],result['payload'])
            elif kind=='verification_ready':
                report=result['report'];validate('ProcessVerificationReport',report)
                from focusmate.core.process_verification import validate_saved_report
                from focusmate.core.process_evidence import collect_evidence
                try:
                    bundle=r['inputBundle']['materials']['processVerification']
                    evidence,_=collect_evidence(bundle,s,st)
                    validate_saved_report(report,bundle,evidence)
                except (ValueError,KeyError) as exc:raise ManagerError('VERIFICATION_REPORT_MISMATCH') from exc
                request=r['verificationRequest'];vr=report['verifierResult'];snapshot=report['inputSnapshot']['request']
                if (report['actionId']!=request['actionId'] or vr['goalContext']!={'goalId':s['goalContext']['goalId'],'scope':'subgoal','subgoalId':request['subgoalId']}
                        or snapshot['contractVersion']!=s['contractVersion'] or snapshot['problemRef']!=s['problemRef']
                        or snapshot['currentCodeRef'] is not None and vr['inputVersionRefs'].get('codeVersion')!=r['codeVersion']):
                    raise ManagerError('VERIFICATION_REPORT_MISMATCH')
                if s['status']=='paused' or r.get('permissionTools'):
                    self._step(s,r,'verification_accept',{'report':deepcopy(report)})
                    return {'status':'retained_while_paused'}
                r.update(status='finished',verification=deepcopy(report),verificationAccepted=True)
                s['status']='waiting_user'
            elif kind=='diagnosis_next':
                if s.get('mode') not in ('diagnosis','coaching'):raise ManagerError('INVALID_DIAGNOSIS_MODE')
                if result['nextKind'].startswith('action_') and s.get('mode')!='coaching':raise ManagerError('INVALID_ACTION_MODE')
                if st['kind'].startswith('action_') and result['nextKind']=='diagnosis_prepare':
                    if r.get('diagnosisReturns'):raise ManagerError('DIAGNOSIS_RETURN_LIMIT')
                    r['diagnosisReturns']=1;r.pop('phase',None)
                    r['plannerReorientation']={**deepcopy(result['payload']['reorientation']),
                        'semanticStatus':'notVerified',
                        'previousDiagnosis':{k:deepcopy(v) for k,v in r['diagnosis'].items() if k!='inputSnapshot'}}
                self._step(s,r,result['nextKind'],result['payload'])
            elif kind=='diagnosis_ready':
                report=result['report'];validate('DiagnosisReport',report)
                if any(report[k]!=s[k] for k in ('goalContext','problemRef','contractVersion')):raise ManagerError('DIAGNOSIS_SCOPE_MISMATCH')
                if s['status']=='paused':
                    self._step(s,r,'diagnosis_accept',{'report':deepcopy(report)})
                    return {'status':'retained_while_paused','roundId':r['roundId']}
                if s.get('mode')=='coaching':
                    r.update(diagnosis=deepcopy(report),phase='action')
                    self._step(s,r,'action_prepare')
                else:
                    r.update(status='finished',diagnosis=deepcopy(report),handoff='action_planning_not_integrated')
                    if s['status']!='paused':s['status']='waiting_user'
            elif kind=='action_ready':
                action=result['action'];validate('LearningActionV2',action)
                if s.get('mode')!='coaching' or action['goalContext']!=s['goalContext'] or action['contractVersion']!=s['contractVersion']:
                    raise ManagerError('ACTION_SCOPE_MISMATCH')
                if action['diagnosisId']!=r.get('diagnosis',{}).get('diagnosisId'):raise ManagerError('ACTION_DIAGNOSIS_MISMATCH')
                if s['status']=='paused':
                    self._step(s,r,'action_accept',{'action':deepcopy(action)})
                else:
                    r.update(learningAction=deepcopy(action),candidate=action['content'],checked=True,
                             checkSource='independentModelReview',messageSeq=st['messageSeq'],
                             status='waiting_permission' if r.get('permissionTools') else 'ready_to_publish')
            elif kind=='candidate':
                r.update(candidate=result['content'],messageSeq=st['messageSeq'],checked=False)
                self._step(s,r,'check',{'candidate':result['content']})
            elif kind=='tool':
                call=result.get('call')
                try: validate_call(call)
                except Exception: self._stop(s,r,'failed','INVALID_TOOL_REQUEST');return {'status':'failed'}
                if call['toolName'] not in r['allowedTools'] or call['arguments'].get('codeVersion',r['codeVersion'])!=r['codeVersion']:
                    self._stop(s,r,'failed','TOOL_NOT_ALLOWED');return {'status':'failed'}
                if any(x['kind']=='tool' and x['roundId']==r['roundId'] and x['payload']['callId']==call['callId'] for x in s['steps'].values()):
                    self._stop(s,r,'failed','DUPLICATE_TOOL_CALL');return {'status':'failed'}
                self._step(s,r,'tool',call)
            elif kind=='tool_result': self._step(s,r,'model')
            elif kind=='checked':
                if result.get('accepted') is not True:self._stop(s,r,'failed','ANSWER_NOT_ACCEPTED')
                else:r.update(checked=True,checkSource='developerFixture',status='waiting_permission' if r.get('permissionTools') else 'ready_to_publish')
            elif kind=='verified':
                if result.get('actionId')!=st['payload']['actionId'] or result.get('codeVersion')!=st['payload']['codeVersion'] or result.get('status') not in ('passed','failed','inconclusive','notReady'):
                    raise ManagerError('VERIFICATION_MISMATCH')
                r.update(status='waiting_permission' if r.get('permissionTools') else 'finished',verification=deepcopy(result))
                if not r.get('permissionTools'):s['status']='waiting_user'
            return {'status':r['status'],'roundId':r['roundId']}
        # Idempotent callback; different content using same completion identity is rejected.
        return self._change(session,{'kind':'complete','stepId':step_id,'token':token,'result':result},apply,'complete:'+step_id+':'+token+(':'+str(result.get('result',{}).get('status')) if result.get('kind')=='tool_result' else ''))

    def verification_report(self,session,result_id):
        for r in self.read(session)['rounds'].values():
            report=r.get('verification',{})
            if r.get('verificationAccepted') and report.get('verifierResult',{}).get('verifierResultId')==result_id:
                return deepcopy(report)
        raise ManagerError('ACCEPTED_VERIFICATION_NOT_FOUND')

    def publish(self,session):
        def apply(s):
            self._expire(s);r=s['rounds'].get(s['currentRoundId'])
            if s['status']=='paused' or not r or r.get('permissionTools') or r['status']!='ready_to_publish':return None
            if not r['checked'] or r['messageSeq']!=len(s['messages']) or r['codeVersion']!=s['codeVersion']:return None
            action={'actionId':identity('action'),'roundId':r['roundId'],'goalContext':deepcopy(s['goalContext']),
                    'codeVersion':r['codeVersion'],'content':r['candidate'],'source':r['checkSource'],'publishedAt':self.clock(),
                    'messageSeq':r['messageSeq'],'status':'available','readAcknowledged':False}
            from datetime import datetime,timezone
            learning={'actionId':action['actionId'],'schemaVersion':'1.0.0','createdAt':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat(),
                      'goalContext':deepcopy(s['goalContext']),'actionType':'hint','content':r['candidate'],
                      'reasonEvidenceRefs':[r['roundId']],'expectedUserAction':'开发模拟：回应这条提示或提供相关修改。',
                      'verificationPlan':{'requiredChecks':[{'capability':'developerFixtureOnly'}]},
                      'status':'proposed','coachPolicy':{'completeSolutionAllowed':False}}
            if 'learningAction' in r:
                learning=deepcopy(r['learningAction']);action['actionId']=learning['actionId']
                validate('LearningActionV2',learning)
            else:validate('LearningAction',learning)
            action['learningAction']=learning
            s['outbox'].append(action);r['status']='finished';s['status']='waiting_user'
            return action
        return self._change(session,{'kind':'publish'},apply)

    def recover(self,session):
        def apply(s):
            self._expire(s)
            for st in s['steps'].values():
                # Waiting for a saved OJ result owns no execution lease. Its
                # durable receipt is reconciled by advance_final_verification.
                if st['kind']=='final_oj': continue
                if st['status'] in ('running','waiting') and self.clock()>=st['leaseUntil']:
                    r=s['rounds'][st['roundId']]
                    if r['status'] not in TERMINAL:self._stop(s,r,'interrupted','EXECUTION_UNKNOWN',cancel=st['kind']=='tool')
                    st['status']='interrupted'
            return {'status':s['status']}
        return self._change(session,{'kind':'recover'},apply)

    def attach_tool_receipt(self,session,step_id,data):
        """Internal adapter receipt, not a public user command or new execution."""
        safe_payload(data)
        def apply(s):
            st=s['steps'].get(step_id)
            if not st or st['kind']!='tool':raise ManagerError('UNKNOWN_TOOL_STEP')
            if st['status']=='done':return None
            expected={'loopId':st['roundId'],'goalContext':s['goalContext'],'problemRef':s['problemRef'],'codeVersion':st['codeVersion']}
            if any(data.get(k)!=v for k,v in expected.items()):raise ManagerError('RUN_CONTEXT_MISMATCH')
            if data.get('status') not in ('succeeded','failed','cancelled','interrupted'):return None
            # A terminal task receipt fences the original worker; no external side effect is replayed.
            st.update(status='running',token=identity('receipt'),leaseUntil=self.clock()+5)
            result={'schemaVersion':'1.0.0','callId':st['payload']['callId'],'toolName':st['payload']['toolName'],
                    **expected,'status':'succeeded' if data['status']=='succeeded' else 'failed','data':deepcopy(data),
                    'ref':'recovered-'+data['runId'],'toolResultId':'recovered-'+data['runId']}
            return {'token':st['token'],'result':result}
        receipt=self._change(session,{'kind':'attach_tool_receipt','stepId':step_id,'data':data},apply)
        if receipt:return self.complete(session,step_id,receipt['token'],{'kind':'tool_result','result':receipt['result']})
        return None

    def record_cancellation(self,session,step_id,result):
        safe_payload(result)
        def apply(s):
            request=next((x for x in s['cancellations'] if x['stepId']==step_id),None)
            if not request:raise ManagerError('UNKNOWN_CANCELLATION')
            request.update(result=deepcopy(result),status=result['status'])
            return deepcopy(request)
        return self._change(session,{'kind':'cancellation_result','stepId':step_id,'result':result},apply)

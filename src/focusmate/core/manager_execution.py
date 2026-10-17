"""Run one claimed step outside SQLite. Semantic roles remain explicit fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
from focusmate.contracts.manager import ManagerError
from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
from focusmate.core.tool_dialogue import TOOL_PROMPT
from focusmate.core.tool_steps import request_model_step, execute_tool_step


def tool_context(state,round_record):
    return {'loopId':round_record['roundId'],'goalContext':deepcopy(state['goalContext']),
            'problemRef':deepcopy(state['problemRef']),'allowedCodeVersions':[round_record['codeVersion']] if round_record['codeVersion'] is not None else [],
            'allowedTools':list(round_record['allowedTools']),
            'deadlineAt':datetime.fromtimestamp(round_record['deadline'],timezone.utc).isoformat()}


class ManagerExecutor:
    def __init__(self,provider,gateway,backend,*,fixture_roles=False,budget=None,materials=None):
        self.provider=provider;self.gateway=gateway;self.backend=backend;self.fixture_roles=fixture_roles
        self.budget=budget;self.materials=deepcopy(materials or {})

    def execute(self,state,step):
        r=state['rounds'][step['roundId']];ctx=tool_context(state,r)
        if step['kind']=='tool':
            checked=execute_tool_step(ctx,step['payload'],self.gateway)
            return {'kind':'failure','code':checked['failure']} if 'failure' in checked else {'kind':'tool_result','result':checked['result']}
        if step['kind']=='check':
            if not self.fixture_roles:return {'kind':'failure','code':'CHECKER_NOT_INTEGRATED'}
            return {'kind':'checked','accepted':True,'source':'developerFixture','notice':'Only a workflow fixture, not content review.'}
        if step['kind']=='verify':
            if not self.fixture_roles:return {'kind':'failure','code':'VERIFIER_NOT_INTEGRATED'}
            return {'kind':'verified','source':'developerFixture','actionId':step['payload']['actionId'],
                    'codeVersion':step['payload']['codeVersion'],'status':'inconclusive','reason':'Development fixture; no learning conclusion.'}
        try:
            code=self.backend.load_code(ctx,r['codeVersion'])
            if code['codeVersion']!=r['codeVersion'] or hashlib.sha256(code['sourceCode'].encode()).hexdigest()!=code['contentHash']:
                raise ValueError('code hash')
        except Exception:return {'kind':'failure','code':'SOURCE_UNAVAILABLE'}
        messages=[{'role':'system','content':TOOL_PROMPT}, {'role':'user','content':json.dumps({
            'goalContext':state['goalContext'],'problemRef':state['problemRef'],'contractVersion':state['contractVersion'],
            'currentCode':code,'materials':r.get('inputBundle',{}).get('materials',self.materials),
            'dialogue':state['messages'][:step['messageSeq']],
            'source':'developer-caller; no production routing or permission inference'},ensure_ascii=False)}]
        # Include complete pairs only. Results from previous rounds stay explicitly historical.
        current_pairs=[];historical=[]
        for st in state['steps'].values():
            result=st.get('result')
            if st['kind']!='tool' or st['status']!='done' or not result or result.get('kind')!='tool_result':continue
            if st['roundId']!=r['roundId']:
                historical.append({'roundId':st['roundId'],'codeVersion':st['codeVersion'],'result':result['result']});continue
            call=st['payload']
            current_pairs.extend([{'role':'assistant','content':'','tool_calls':[{'id':call['callId'],'type':'function','function':{
                'name':call['toolName'],'arguments':json.dumps(call['arguments'],ensure_ascii=False)}}]},
                {'role':'tool','tool_call_id':call['callId'],'content':json.dumps(result['result'],ensure_ascii=False)}])
        if historical:messages.append({'role':'user','content':json.dumps({'historicalToolEvidence':historical},ensure_ascii=False)})
        messages.extend(current_pairs)
        tools=[t for t in TOOL_DEFINITIONS if t['function']['name'] in ctx['allowedTools']]
        deadline=time.monotonic()+max(0,r['deadline']-time.time())
        raw=request_model_step(ctx,messages,tools,TOOL_PROMPT,self.provider,deadline=deadline,budget=self.budget)
        audit={'modelConfig':deepcopy(getattr(self.provider,'public_config',{}))}
        if 'contextSnapshot' in raw:audit['contextSnapshot']=raw['contextSnapshot']
        if 'failure' in raw:return {'kind':'failure','code':raw['failure'],**audit}
        response=raw['response'];audit['visibleResponse']=response
        calls=response['toolCalls']
        if len(calls)>1:return {'kind':'failure','code':'MULTIPLE_TOOL_CALLS',**audit}
        if not calls:return {'kind':'candidate','content':response['content'],**audit}
        call=deepcopy(calls[0])
        try:
            if isinstance(call.get('arguments'),str):call['arguments']=strict_json(call['arguments'])
        except Exception:return {'kind':'failure','code':'INVALID_ARGUMENTS',**audit}
        return {'kind':'tool','call':call,**audit}


def reconcile_runs(manager,session,lookup):
    report={'receipts':[],'failures':[]}
    state=manager.read(session)
    for request in state['cancellations']:
        if request['status']=='done':continue
        step=state['steps'][request['stepId']]
        try:manager.record_cancellation(session,step['stepId'],lookup.cancel(state,step))
        except Exception as exc:report['failures'].append({'stepId':step['stepId'],'code':str(exc) if isinstance(exc,ManagerError) else 'CANCEL_UNAVAILABLE'})
    for step in state['steps'].values():
        if step['kind']!='tool' or step['status'] not in ('running','waiting','interrupted'):continue
        if step['payload']['toolName'] not in ('run_public_samples','run_local_code'):continue
        try:
            data=lookup.lookup(state,step)
            if data is not None and data['status'] not in ('waiting','running'):
                report['receipts'].append(manager.attach_tool_receipt(session,step['stepId'],data))
        except Exception as exc:report['failures'].append({'stepId':step['stepId'],'code':str(exc) if isinstance(exc,ManagerError) else 'LOOKUP_UNAVAILABLE'})
    return report


def advance_once(manager,session,executor,*,worker='developer-worker',lookup=None):
    manager.pin_materials(session,executor.materials,executor.budget)
    if lookup is not None:reconcile_runs(manager,session,lookup)
    manager.recover(session)
    if lookup is not None:reconcile_runs(manager,session,lookup)
    step=manager.claim(session,worker)
    if step is None:return {'status':'idle'}
    state=manager.read(session)
    try: result=executor.execute(state,step)
    except Exception as exc: result={'kind':'failure','code':getattr(exc,'code','STEP_UNAVAILABLE')}
    try:return manager.complete(session,step['stepId'],step['token'],result)
    except ManagerError as exc:
        # Never restart an uncertain external call. The owning worker may have expired.
        manager.recover(session)
        return {'status':'not_applied','failure':str(exc)}

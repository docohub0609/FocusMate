"""One durable verification step; Manager owns every request and execution."""
from copy import deepcopy
import json
import time
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS, validate_call
from focusmate.contracts.skill_files import strict_json
from focusmate.core.manager_execution import ManagerExecutor
from focusmate.core.context_builder import build_context, MapContextResolver, hash_content
from focusmate.core.process_evidence import collect_evidence
from focusmate.core.process_verification import VERSION, PROMPT, MODEL_RESULT, finish_verification, presented_record, expand_public_cases


class ProcessExecutor(ManagerExecutor):
    def __init__(self,provider,gateway,backend,*,bundle):
        self.bundle=deepcopy(bundle)
        super().__init__(provider,gateway,backend,budget=bundle['request']['budget'],materials={'processVerification':bundle})

    def _evidence(self,state,step):
        return collect_evidence(self.bundle,state,step)

    def execute(self,state,step):
        if step['kind']=='tool':return super().execute(state,step)
        if not step['kind'].startswith('verification_'):return {'kind':'failure','code':'VERIFICATION_STEP_REQUIRED'}
        req=self.bundle['request'];r=state['rounds'][step['roundId']]
        expected=r['verificationRequest']
        if (req['actionId']!=expected['actionId'] or req['goalContext']['subgoalId']!=expected['subgoalId']
                or req['materialRefs']!=expected['materialRefs'] or req['contractVersion']!=state['contractVersion']
                or req['problemRef']!=state['problemRef']):return {'kind':'failure','code':'VERIFICATION_REQUEST_MISMATCH'}
        current=next((x for x in self.bundle['records'] if x['ref']==req['currentCodeRef']),None)
        if current is not None and current['version']!=r['codeVersion']:return {'kind':'failure','code':'VERIFICATION_CODE_MISMATCH'}
        published=next((a['learningAction'] for a in state['outbox'] if a['actionId']==req['actionId']),None)
        if published!=self.bundle['action']:return {'kind':'failure','code':'PUBLISHED_ACTION_MISMATCH'}
        if step['kind']=='verification_accept':return {'kind':'verification_ready','report':step['payload']['report']}
        try:extra,history=self._evidence(state,step)
        except ValueError as exc:return {'kind':'failure','code':str(exc)}
        if step['kind']=='verification_prepare':
            tools=[]
            if current is not None and r['counts']['tool']<state['limits']['tool'] and r['counts']['model']+1<state['limits']['model']:
                tools=[deepcopy(t) for t in TOOL_DEFINITIONS if t['function']['name'] in r['allowedTools']
                    and (t['function']['name'] not in ('run_public_samples','run_local_code') or r['counts']['execution']<state['limits']['execution'])]
            materials=[presented_record(record) for record in expand_public_cases(self.bundle['records']+extra)]
            payload={'goalContext':req['goalContext'],'requirements':self.bundle['checks'],'subgoal':self.bundle['subgoal'],
                'materials':materials,'previousHelp':self.bundle['action'],'outputSchema':MODEL_RESULT,
                'evidenceCatalog':[x['ref'] for x in materials]}
            messages=[{'role':'system','content':PROMPT},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]+history
            built=build_context({'goalContext':req['goalContext'],'purpose':'processVerification','policyVersion':VERSION,
                'systemPrompt':PROMPT,'mandatoryPayload':{'messages':messages,'tools':tools},'dialogueRefs':[],
                'sourceRefs':[],'budget':req['budget']},MapContextResolver([]))
            if built['status']!='ready':return {'kind':'failure','code':built['failure']['code']}
            return {'kind':'verification_next','nextKind':'verification_model','payload':{'snapshot':built['snapshot']},'contextSnapshot':built['snapshot']}
        snapshot=step['payload']['snapshot'];payload=snapshot['modelPayload']
        if self.budget['reservedOutputTokens']<getattr(self.provider,'public_config',{}).get('maxTokens',0):
            return {'kind':'failure','code':'OUTPUT_RESERVE_TOO_SMALL'}
        response=self.provider.complete(messages=deepcopy(payload['messages']),tools=deepcopy(payload['tools']),
            deadline=time.monotonic()+max(0,r['deadline']-time.time()))
        audit={'visibleResponse':response,'modelConfig':getattr(self.provider,'public_config',{})}
        if response.get('toolCalls'):
            if len(response['toolCalls'])!=1:return {'kind':'failure','code':'MULTIPLE_TOOL_CALLS',**audit}
            call=deepcopy(response['toolCalls'][0])
            try:
                if isinstance(call['arguments'],str):call['arguments']=strict_json(call['arguments'])
                validate_call(call)
                if call['toolName'] not in [t['function']['name'] for t in payload['tools']]:raise ValueError()
            except Exception:return {'kind':'failure','code':'INVALID_VERIFICATION_TOOL',**audit}
            return {'kind':'tool','call':call,**audit}
        try:report=finish_verification(strict_json(response['content']),self.bundle,extra_records=extra)
        except ValueError as exc:return {'kind':'failure','code':str(exc),**audit}
        return {'kind':'verification_ready','report':report,**audit}

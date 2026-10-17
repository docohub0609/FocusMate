"""One action step per Manager claim; no retry or tool loop lives here."""
from copy import deepcopy, copy
from datetime import datetime, timezone
import json
import time
from uuid import uuid4
from jsonschema import Draft202012Validator
from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.schema_validation import validate
from focusmate.core.context_builder import build_context, MapContextResolver
from focusmate.core.diagnosis_execution import DiagnosisExecutor, DeferredSummary
from focusmate.core.diagnosis import _validate_history_snapshot
from focusmate.core.action_planning import (
    VERSION, ACTOR_PROMPT, REVIEW_PROMPT, CANDIDATE_SCHEMA, REVIEW_SCHEMA,
    validate_action_candidate, review_transition, review_issues, action_from_candidate,
)


def next_action(kind,payload,**audit):
    return {'kind':'diagnosis_next','nextKind':'action_'+kind,'payload':payload,**audit}


class ActionExecutor(DiagnosisExecutor):
    actor_prompt=ACTOR_PROMPT
    review_prompt=REVIEW_PROMPT
    track_review_issues=True

    def review_request(self, material, candidate, previous_issues):
        payload=deepcopy(material)
        payload.update(candidate=candidate,reviewSchema=REVIEW_SCHEMA)
        if previous_issues:payload['previousIssues']=deepcopy(previous_issues)
        return self.review_prompt,payload

    def repair_request(self, material, candidate, review, previous_issues):
        payload=deepcopy(material)
        payload.update(candidate=candidate,outputSchema=CANDIDATE_SCHEMA,correction=review)
        if previous_issues:payload['previousIssues']=deepcopy(previous_issues)
        return self.actor_prompt,payload
    def _payload(self,state,step,extra,skill):
        payload=super()._payload(state,step,extra,skill)
        reorientation=state['rounds'][step['roundId']].get('plannerReorientation')
        if reorientation and step['kind'].startswith('diagnosis_'):
            payload['plannerReorientation']=deepcopy(reorientation)
        return payload

    def _records(self,state,step):
        extra,skill,summaries=super()._records(state,step)
        if not step['kind'].startswith('action_'):return extra,skill,summaries
        snapshot=state['rounds'][step['roundId']].get('diagnosis',{}).get('inputSnapshot',{})
        seen={r['ref']:r for r in extra}
        for record in snapshot.get('actualMaterials',[]):
            if record['ref'] in seen and seen[record['ref']]!=record:raise ValueError('ACTION_MATERIAL_CONFLICT')
            if record['ref'] not in seen:extra.append(deepcopy(record));seen[record['ref']]=record
        return extra,skill or snapshot.get('selectedSkill'),summaries

    def _tools(self,state,step):
        r=state['rounds'][step['roundId']]
        if not step['kind'].startswith('action_') and state['limits']['model']-r['counts']['model']<=3:
            return []  # Preserve one diagnosis response, one help, one review.
        return super()._tools(state,step)

    def __init__(self,*args,diagnosis=None,review_provider=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.review_provider=review_provider if review_provider is not None else self.provider
        contract=next(r['content'] for r in self.bundle['records'] if r['ref']==self.bundle['request']['contractRef'])
        if contract['contractStatus']!='frozen':raise ValueError('ACTION_CURRENT_CONTRACT_REQUIRED')
        self.seed=deepcopy(diagnosis)
        if self.seed is not None:
            validate('DiagnosisReport',self.seed)
            _validate_history_snapshot(self.seed,self.bundle['request'])
            if any(self.seed[k]!=self.bundle['request'][k] for k in ('goalContext','problemRef','contractVersion')):
                raise ValueError('ACTION_DIAGNOSIS_SCOPE_MISMATCH')
            if (self.seed['inputSnapshot']['request']!=self.bundle['request']
                    or self.seed['inputSnapshot']['records']!=self.bundle['records']):
                raise ValueError('ACTION_DIAGNOSIS_INPUT_MISMATCH')
            self.materials['seedDiagnosis']=self.seed

    def execute(self,state,step):
        r=state['rounds'][step['roundId']]
        if not step['kind'].startswith('action_'):
            if step['kind']=='diagnosis_prepare' and self.seed is not None and step['messageSeq']==1 and not r.get('diagnosisReturns'):
                return {'kind':'diagnosis_ready','report':deepcopy(self.seed)}
            return super().execute(state,step)
        req=self.bundle['request']
        if any(state[k]!=req[k] for k in ('goalContext','problemRef','contractVersion')):
            return {'kind':'failure','code':'ACTION_SCOPE_MISMATCH'}
        diagnosis=r.get('diagnosis')
        if not diagnosis:return {'kind':'failure','code':'MISSING_DIAGNOSIS'}
        # The latest diagnosis pins the exact current message and code, including
        # Manager messages appended after the initial developer request.
        bundle={'request':deepcopy(diagnosis['inputSnapshot']['request']),
                'records':deepcopy(diagnosis['inputSnapshot']['records'])}
        current=next((x for x in bundle['records'] if x['ref']==bundle['request'].get('currentCodeRef')),None)
        if (current['version'] if current else None)!=r['codeVersion']:
            return {'kind':'failure','code':'ACTION_CODE_MISMATCH'}
        if state['messages'][step['messageSeq']-1]['text']!=next(x for x in bundle['records'] if x['ref']==bundle['request']['userRequestRef'])['content']['text']:
            return {'kind':'failure','code':'ACTION_MESSAGE_MISMATCH'}
        extra,skill,summaries=self._records(state,step)
        refs=[v['ref'] for v in bundle['records']+extra]
        deadline=time.monotonic()+max(0,r['deadline']-time.time())
        p=step['payload']
        if step['kind']=='action_accept':return {'kind':'action_ready','action':p['action']}
        if step['kind']=='action_summary':
            response=self.provider.complete(messages=[{'role':'system','content':p['system_prompt']},
                {'role':'user','content':p['user_prompt']}],tools=[],deadline=deadline)
            if response['toolCalls']:return {'kind':'failure','code':'SUMMARY_TOOL_FORBIDDEN','visibleResponse':response}
            return next_action('prepare',{'key':p['key'],'summaryResponse':response},visibleResponse=response)
        if step['kind']=='action_prepare':
            if state['limits']['model']-r['counts']['model']<2:return {'kind':'failure','code':'ACTION_REVIEW_BUDGET_UNAVAILABLE'}
            view=copy(self);view.bundle={**self.bundle,**bundle}
            payload=view._payload(state,step,extra,skill)
            visible_diagnosis={k:deepcopy(v) for k,v in diagnosis.items() if k!='inputSnapshot'}
            payload.update(outputSchema=CANDIDATE_SCHEMA,diagnosis=visible_diagnosis,
                           publishedActions=[a['learningAction'] for a in state['outbox']])
            payload.pop('skillCatalog',None)
            tools=[t for t in self._tools(state,step) if t['function']['name'] not in ('read_skill','analyze_stage','analyze_progress')]
            if not r.get('diagnosisReturns') and state['limits']['model']-r['counts']['model']>=4:
                tools.append({'type':'function','function':{'name':'reconsider_diagnosis',
                    'description':'只有问题或目标关联需要重做时返回诊断一次；局部错话自行纠正。',
                    'parameters':{'type':'object','properties':{'reason':{'type':'string','minLength':1},
                        'evidenceRefs':{'type':'array','items':{'type':'string'},'minItems':1,'uniqueItems':True}},
                        'required':['reason','evidenceRefs'],'additionalProperties':False}}})
            payload['availableTools']=tools
            deferred=DeferredSummary(summaries)
            built=build_context({'goalContext':req['goalContext'],'purpose':'actionPlanning','policyVersion':VERSION,
                'systemPrompt':self.actor_prompt,'mandatoryPayload':payload,'dialogueRefs':bundle['request']['dialogueRefs'],
                'sourceRefs':[],'budget':req['budget']},MapContextResolver(bundle['records']),deferred,deadline=deadline)
            if deferred.pending:return next_action('summary',deferred.pending)
            if built['status']!='ready':return {'kind':'failure','code':built['failure']['code']}
            return next_action('model',{'snapshot':built['snapshot'],'tools':tools},contextSnapshot=built['snapshot'])
        if step['kind'] not in ('action_model','action_repair','action_review'):
            return {'kind':'failure','code':'UNKNOWN_ACTION_STEP'}
        is_review=step['kind']=='action_review'
        if step['kind']=='action_model':
            snapshot=p['snapshot'];payload=snapshot['modelPayload'];prompt=snapshot['systemPrompt'];tools=p['tools']
        else:
            tools=[]
            if is_review:
                prompt,payload=self.review_request(p['materialPayload'],p['candidate'],p.get('previousIssues',[]))
            else:
                prompt,payload=self.repair_request(p['materialPayload'],p['candidate'],p['review'],p.get('previousIssues',[]))
            # Hooks may remove generation instructions, but preserve the pinned
            # evidence/history. Review does not retrieve or summarize it again.
            built=build_context({'goalContext':req['goalContext'],'purpose':'actionReview' if is_review else 'actionPlanning','policyVersion':VERSION,
                'systemPrompt':prompt,'mandatoryPayload':{'originalAnalyzedMaterials':payload},'dialogueRefs':[],'sourceRefs':[],
                'budget':req['budget']},MapContextResolver([]))
            if built['status']!='ready':return {'kind':'failure','code':built['failure']['code']}
        provider=self.review_provider if is_review else self.provider
        if req['budget']['reservedOutputTokens']<getattr(provider,'public_config',{}).get('maxTokens',0):
            return {'kind':'failure','code':'OUTPUT_RESERVE_TOO_SMALL'}
        response=provider.complete(messages=[{'role':'system','content':prompt},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],tools=tools,deadline=deadline)
        audit={'visibleResponse':response,'modelConfig':getattr(provider,'public_config',{})}
        if response['toolCalls']:
            if step['kind']!='action_model' or len(response['toolCalls'])!=1:return {'kind':'failure','code':'INVALID_ACTION_TOOL_REQUEST',**audit}
            call=deepcopy(response['toolCalls'][0]);args=call['arguments']
            try:
                if isinstance(args,str):args=strict_json(args)
                definition=next(t for t in tools if t['function']['name']==call['toolName'])
                Draft202012Validator(definition['function']['parameters']).validate(args)
            except Exception:return {'kind':'failure','code':'INVALID_ACTION_TOOL_REQUEST',**audit}
            call['arguments']=args
            if call['toolName']=='reconsider_diagnosis':
                if not set(args['evidenceRefs'])<=set(refs):return {'kind':'failure','code':'INVALID_ACTION_REFERENCE',**audit}
                return {'kind':'diagnosis_next','nextKind':'diagnosis_prepare',
                        'payload':{'reorientation':args},**audit}
            return {'kind':'tool','call':call,**audit}
        try:
            value=strict_json(response['content'])
            if not is_review:
                candidate=validate_action_candidate(value,bundle,diagnosis,extra)
                material=deepcopy(payload)
                for key in ('candidate','correction','previousIssues'):material.pop(key,None)
                return next_action('review',{'candidate':candidate,'materialPayload':material,
                    'repaired':step['kind']=='action_repair','previousIssues':deepcopy(p.get('previousIssues',[]))},**audit)
            transition=review_transition(value,p['candidate'],refs,repaired=p['repaired'],
                remaining_model_calls=state['limits']['model']-r['counts']['model'],previous_issues=p.get('previousIssues',[]))
            if transition=='repair':
                return next_action('repair',{'candidate':p['candidate'],'review':value,'materialPayload':p['materialPayload'],
                    'previousIssues':review_issues(value,p['candidate']) if self.track_review_issues else []},**audit)
            if transition!='accept':return {'kind':'failure','code':'ACTION_REVIEW_'+transition.upper(),**audit}
            action=action_from_candidate(p['candidate'],bundle,diagnosis,action_id='action-'+uuid4().hex,
                                        created_at=datetime.now(timezone.utc).isoformat())
            validate('LearningActionV2',action)
            return {'kind':'action_ready','action':action,'audit':audit}
        except ValueError as exc:return {'kind':'failure','code':str(exc),**audit}

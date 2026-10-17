"""Coach uses the provider's JSON response mode, retaining native tool calls."""
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from apps.api.deepseek_provider import DeepSeekConfig
from dataclasses import replace
import json
from copy import deepcopy
from urllib.parse import urlsplit
from jsonschema import Draft202012Validator,ValidationError
from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.tool_calls import validate_call
from focusmate.contracts.model_response import ModelResponseError, bounded_text


class DeepSeekCoachProvider(DeepSeekToolChatProvider):
    @classmethod
    def from_environment(cls):
        return cls(replace(DeepSeekConfig.from_environment(),max_tokens=8192))

    def request_body(self,messages,tools):
        return {**super().request_body(messages,tools),'response_format':{'type':'json_object'}}

    def _read_response(self,response,deadline):
        raw=super()._read_response(response,deadline)
        try:
            usage=json.loads(raw).get('usage',{})
            self.last_usage={k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens')
                if type(usage.get(k)) is int and usage[k]>=0}
        except (ValueError,TypeError):self.last_usage={}
        return raw

    def complete(self,**kwargs):
        self.last_usage={}
        return {**super().complete(**kwargs),'usage':self.last_usage}


def strict_transport_schema(schema):
    """Provider subset only; the complete local schema remains authoritative."""
    s=deepcopy(schema)
    if 'anyOf' in s:return {'anyOf':[strict_transport_schema(branch) for branch in s['anyOf']]}
    if 'allOf' in s:
        # Dynamic target edits: creation/revision need content; reuse/withdraw do not.
        if s.get('properties',{}).get('kind',{}).get('enum')!=['create','revise','reuse','withdraw']:
            raise ValueError('UNSUPPORTED_STRICT_SCHEMA')
        branches=[]
        for kinds in (['create','revise'],['reuse','withdraw']):
            keys=list(s['properties']) if kinds[0]=='create' else s['required']
            props={k:s['properties'][k] for k in keys};props['kind']={'enum':kinds}
            branches.append(strict_transport_schema({'type':'object','properties':props,'required':keys}))
        return {'anyOf':branches}
    result={k:v for k,v in s.items() if k in ('type','enum','const','minimum','maximum','pattern','description')}
    if 'enum' in result and 'type' not in result:result['type']='string'
    if 'const' in result and 'type' not in result:result['type']='string' if isinstance(result['const'],str) else 'integer'
    if 'properties' in s:
        optional=[k for k in s['properties'] if k not in s.get('required',[])]
        if optional:
            if len(optional)>4:raise ValueError('UNSUPPORTED_OPTIONAL_STRICT_FIELD')
            variants=[]
            for mask in range(1<<len(optional)):
                keys=list(s.get('required',[]))+[k for i,k in enumerate(optional) if mask & (1<<i)]
                variants.append(strict_transport_schema({'type':'object','properties':{k:s['properties'][k] for k in keys},'required':keys}))
            return {'anyOf':variants}
        result.update(properties={k:strict_transport_schema(v) for k,v in s['properties'].items()},
                      required=list(s['properties']),additionalProperties=False)
    if 'items' in s:result['items']=strict_transport_schema(s['items'])
    return result


class StrictDeepSeekCoachProvider(DeepSeekCoachProvider):
    """Candidate transport. A formatting call never executes an application tool."""
    def __init__(self,config,**kwargs):
        if urlsplit(config.base_url).hostname!='api.deepseek.com':raise ValueError('STRICT_PROVIDER_HOST_UNSUPPORTED')
        super().__init__(replace(config,base_url='https://api.deepseek.com/beta'),**kwargs)
        self.public_config['structuredTransport']='strict-function-envelope-v1'

    @staticmethod
    def schema(messages):
        payload=strict_json(messages[-1]['content'])
        schema=payload.get('outputSchema') or payload.get('reviewSchema')
        if not isinstance(schema,dict):raise ValueError('COACH_OUTPUT_SCHEMA_REQUIRED')
        return schema

    def request_body(self,messages,tools):
        response={'type':'object','properties':{'kind':{'type':'string','enum':['response']},
            'value':strict_transport_schema(self.schema(messages))},'required':['kind','value'],'additionalProperties':False}
        variants=[response]
        if tools:
            variants.append({'type':'object','properties':{'kind':{'type':'string','enum':['toolRequest']},
                'call':{'type':'object','properties':{'callId':{'type':'string'},
                    'toolName':{'type':'string','enum':[t['function']['name'] for t in tools]},
                    'arguments':{'type':'string','description':'JSON object encoded as a string, using the requested tool schema.'}},
                    'required':['callId','toolName','arguments'],'additionalProperties':False}},
                'required':['kind','call'],'additionalProperties':False})
        messages=deepcopy(messages)
        messages[0]['content']+='\nReturn the response through return_coach_step. It only formats output. Application tool requests remain subject to normal permission and version checks. Available tools: '+json.dumps(tools,ensure_ascii=False)
        body=super().request_body(messages,[]);body.pop('response_format',None)
        body['tools']=[{'type':'function','function':{'name':'return_coach_step','strict':True,
            'description':'Return exactly one final structured response or one application tool request.',
            'parameters':{'type':'object','properties':{'step':{'anyOf':variants}},'required':['step'],'additionalProperties':False}}}]
        body['tool_choice']={'type':'function','function':{'name':'return_coach_step'}}
        return body

    def complete(self,**kwargs):
        reply=super().complete(**kwargs);calls=reply.get('toolCalls',[])
        text=calls[0].get('arguments','') if calls else reply.get('content','')
        try:return self._decode(reply,kwargs)
        except ModelResponseError as exc:
            exc.rejected_response=bounded_text(text)
            raise
        except (ValueError,KeyError,TypeError,AttributeError,ValidationError) as exc:
            raise ModelResponseError(text,exc,channel='toolArguments' if calls else 'content',
                audit=reply.get('responseAudit',{})) from exc

    def _decode(self,reply,kwargs):
        calls=reply.get('toolCalls',[])
        if len(calls)!=1 or calls[0]['toolName']!='return_coach_step':raise ValueError('STRICT_RESPONSE_REQUIRED')
        envelope=strict_json(calls[0]['arguments'])
        if set(envelope)!={'step'}:raise ValueError('INVALID_STRICT_ENVELOPE')
        value=envelope['step']
        if value.get('kind')=='response' and set(value)=={'kind','value'}:
            try:Draft202012Validator(self.schema(kwargs['messages'])).validate(value['value'])
            except ValidationError as exc:raise ValueError('STRICT_RESPONSE_INVALID') from exc
            return {**reply,'content':json.dumps(value['value'],ensure_ascii=False),'toolCalls':[]}
        if value.get('kind')=='toolRequest' and set(value)=={'kind','call'}:
            call=deepcopy(value['call']);allowed={t['function']['name'] for t in kwargs['tools']}
            if set(call)!={'callId','toolName','arguments'} or not isinstance(call['callId'],str) or not call['callId']:
                raise ValueError('INVALID_TOOL_REQUEST')
            if call.get('toolName') not in allowed:raise ValueError('TOOL_NOT_AVAILABLE')
            try:call['arguments']=strict_json(call['arguments'])
            except (ValueError,TypeError) as exc:
                raise ModelResponseError(call['arguments'],exc,channel='applicationToolArguments',
                    audit=reply.get('responseAudit',{})) from exc
            parameters=next(t['function']['parameters'] for t in kwargs['tools'] if t['function']['name']==call['toolName'])
            Draft202012Validator(parameters).validate(call['arguments'])
            if call['toolName']!='read_skill':validate_call(call)
            return {**reply,'content':'','toolCalls':[call]}
        raise ValueError('INVALID_STRICT_ENVELOPE')

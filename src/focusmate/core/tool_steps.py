"""One context/model step and one checked tool result, shared by both drivers."""
from copy import deepcopy
import time
from focusmate.core.tool_context import prepare_tool_context


def request_model_step(context, messages, tools, prompt, provider, *, deadline, budget=None, clock=time.monotonic):
    if clock()>=deadline:return {'failure':'DEADLINE_EXCEEDED'}
    built=prepare_tool_context(context,messages,tools,prompt,budget=budget)
    if built['status']!='ready':return {'failure':'CONTEXT_BUILD_FAILED','contextFailure':built['failure']}
    snapshot=built['snapshot']; result={'contextSnapshot':snapshot}
    if snapshot['budget']['reservedOutputTokens']<getattr(provider,'public_config',{}).get('maxTokens',0):
        return {**result,'failure':'OUTPUT_RESERVE_TOO_SMALL'}
    result['modelRequested']=True
    try:
        response=provider.complete(messages=deepcopy(snapshot['modelPayload']['messages']),
                                  tools=deepcopy(snapshot['modelPayload']['tools']),deadline=deadline)
    except Exception as exc:return {**result,'failure':getattr(exc,'code','PROVIDER_UNAVAILABLE')}
    if clock()>=deadline:return {**result,'failure':'DEADLINE_EXCEEDED'}
    if not isinstance(response,dict) or not isinstance(response.get('content'),str) or not isinstance(response.get('toolCalls'),list):
        return {**result,'failure':'PROVIDER_OUTPUT_INVALID'}
    result['response']={k:deepcopy(response[k]) for k in ('content','toolCalls','model') if k in response}
    return result


def execute_tool_step(context,call,gateway):
    try:result=gateway.invoke(context,call)
    except Exception:return {'failure':'TOOL_EXECUTION_FAILED'}
    if not isinstance(result,dict) or any(result.get(key)!=expected for key,expected in {
        'callId':call['callId'],'goalContext':context['goalContext'],'problemRef':context['problemRef'],
        'loopId':context['loopId'],'toolName':call['toolName']}.items()):
        return {'failure':'TOOL_RESULT_MISMATCH'}
    return {'result':deepcopy(result)}

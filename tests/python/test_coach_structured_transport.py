import json
import pytest
from apps.api.deepseek_provider import DeepSeekConfig
from focusmate.adapters.coach_chat import StrictDeepSeekCoachProvider,strict_transport_schema
from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
from focusmate.core.dynamic_coach_roles import PLAN_SCHEMA
from jsonschema import Draft202012Validator


def test_goal_change_variants_keep_reuse_minimal_and_create_required():
    schema=strict_transport_schema(PLAN_SCHEMA)['properties']['goalChanges']['items']
    Draft202012Validator(schema).validate({'kind':'reuse','key':'g','expectedVersion':1,'reason':'same'})
    with pytest.raises(Exception):Draft202012Validator(schema).validate({'kind':'create','key':'new:g','expectedVersion':None,'reason':'new'})


class Response:
    def __init__(self,value):self.value=value
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,limit):return json.dumps({'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[
        {'id':'format','function':{'name':'return_coach_step','arguments':json.dumps({'step':self.value})}}]}}]}).encode()


def provider(value):
    return StrictDeepSeekCoachProvider(DeepSeekConfig('secret','https://api.deepseek.com','m'),
        opener=lambda *a,**kw:Response(value),clock=lambda:0)


def messages():return [{'role':'user','content':json.dumps({'outputSchema':{'type':'object','properties':{'content':{'type':'string'}},'required':['content'],'additionalProperties':False}})}]


def test_formatting_response_is_not_an_application_tool_call():
    p=provider({'kind':'response','value':{'content':'answer'}})
    result=p.complete(messages=messages(),tools=[],deadline=10)
    assert strict_json(result['content'])=={'content':'answer'} and result['toolCalls']==[]
    body=p.request_body(messages(),[])
    assert p.config.base_url=='https://api.deepseek.com/beta'
    assert body['tools'][0]['function']['strict'] is True


def test_actual_tool_request_still_reaches_normal_permission_checks():
    p=provider({'kind':'toolRequest','call':{'callId':'a','toolName':'run_public_samples','arguments':'{"codeVersion":1}'}})
    result=p.complete(messages=messages(),tools=TOOL_DEFINITIONS,deadline=10)
    assert result['toolCalls'][0]['toolName']=='run_public_samples'
    with pytest.raises(ValueError):p.complete(messages=messages(),tools=[],deadline=10)


def test_strict_transport_does_not_accept_locally_invalid_response():
    p=provider({'kind':'response','value':{'content':'answer','invented':True}})
    with pytest.raises(ValueError):p.complete(messages=messages(),tools=[],deadline=10)


def test_all_role_schemas_compile_without_mutating_local_contracts():
    from copy import deepcopy
    from focusmate.core.dynamic_coach_roles import diagnosis_schema,DYNAMIC_REVIEW_SCHEMA
    from focusmate.contracts.process_verification import MODEL_RESULT
    for schema in (diagnosis_schema(),PLAN_SCHEMA,DYNAMIC_REVIEW_SCHEMA,MODEL_RESULT):
        original=deepcopy(schema)
        Draft202012Validator.check_schema(strict_transport_schema(schema))
        assert schema==original


def test_invalid_tool_arguments_are_not_sent_to_execution():
    p=provider({'kind':'toolRequest','call':{'callId':'a','toolName':'run_public_samples','arguments':'{"codeVersion":1,"codeVersion":2}'}})
    with pytest.raises(ValueError):p.complete(messages=messages(),tools=TOOL_DEFINITIONS,deadline=10)


def test_skill_arguments_use_the_callers_schema():
    offered=[{'type':'function','function':{'name':'read_skill','parameters':{
        'type':'object','properties':{'path':{'type':'string','enum':['allowed.md']}},
        'required':['path'],'additionalProperties':False}}}]
    p=provider({'kind':'toolRequest','call':{'callId':'skill','toolName':'read_skill','arguments':'{"path":"allowed.md"}'}})
    assert p.complete(messages=messages(),tools=offered,deadline=10)['toolCalls'][0]['arguments']=={'path':'allowed.md'}
    bad=provider({'kind':'toolRequest','call':{'callId':'skill','toolName':'read_skill','arguments':'{"path":"other.md"}'}})
    with pytest.raises(Exception):bad.complete(messages=messages(),tools=offered,deadline=10)


def test_nested_tool_argument_error_keeps_its_own_position_and_outer_reply():
    p=provider({'kind':'toolRequest','call':{'callId':'a','toolName':'run_public_samples','arguments':'{"codeVersion":1}}'}})
    with pytest.raises(ValueError) as error:p.complete(messages=messages(),tools=TOOL_DEFINITIONS,deadline=10)
    detail=error.value.response_diagnostic
    assert detail['channel']=='applicationToolArguments'
    assert detail['text']=='{"codeVersion":1}}' and detail['position']==17
    assert 'toolRequest' in error.value.rejected_response['text']


def test_tool_schema_failure_keeps_original_response():
    p=provider({'kind':'toolRequest','call':{'callId':'a','toolName':'run_public_samples','arguments':'{}'}})
    with pytest.raises(ValueError) as error:p.complete(messages=messages(),tools=TOOL_DEFINITIONS,deadline=10)
    assert error.value.response_diagnostic['position'] is None
    assert 'toolRequest' in error.value.rejected_response['text']

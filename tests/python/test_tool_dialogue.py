from copy import deepcopy
from datetime import datetime, timedelta, timezone

from focusmate.core.tool_dialogue import run_tool_dialogue
from focusmate.core.tool_context import prepare_tool_context


def context():
    return {'loopId': 'loop-1', 'goalContext': {'goalId': 'g1', 'scope': 'goal'},
            'problemRef': {'problemId': 'p1', 'problemVersion': 1}, 'allowedCodeVersions': [2],
            'allowedTools': ['read_code_excerpt'],
            'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}


class Provider:
    public_config = {'maxTokens': 100}

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.responses)


class Gateway:
    def __init__(self):
        self.calls = []

    def invoke(self, ctx, call):
        self.calls.append(call)
        return {'callId': call['callId'], 'toolName': call['toolName'], 'loopId': ctx['loopId'],
                'goalContext': ctx['goalContext'], 'problemRef': ctx['problemRef'],
                'status': 'succeeded', 'data': {'text': 'print(5)'}}


def test_native_pair_snapshot_and_final_response():
    provider = Provider([{'content': '', 'toolCalls': [{'callId': 'a', 'toolName': 'read_code_excerpt', 'arguments': {'codeVersion': 'v2'}}]},
                         {'content': '只看到了局部代码。', 'toolCalls': []}])
    gateway = Gateway()
    report = run_tool_dialogue(context(), {'userRequest': '检查代码'}, provider, gateway, tools=[])
    assert report['status'] == 'answered'
    assert len(gateway.calls) == 1
    assert provider.requests[1]['messages'][-1]['tool_call_id'] == 'a'
    assert report['contextSnapshots'][1]['modelPayload']['messages'] == provider.requests[1]['messages']


def test_multiple_requests_not_executed():
    call = {'callId': 'a', 'toolName': 'read_code_excerpt', 'arguments': {}}
    p = Provider([{'content': '', 'toolCalls': [call, {**call, 'callId': 'b'}]}, {'content': '无需执行。', 'toolCalls': []}])
    g = Gateway()
    report = run_tool_dialogue(context(), {}, p, g, tools=[])
    assert not g.calls
    assert report['status'] == 'answered'
    assert len(p.requests[1]['messages']) == 5


def test_orphan_tool_result_and_small_budget_rejected():
    r = prepare_tool_context(context(), [{'role': 'tool', 'tool_call_id': 'missing', 'content': '{}'}], [], 'rules')
    assert r['failure']['code'] == 'INVALID_TOOL_MESSAGE_PAIRS'
    r = prepare_tool_context(context(), [{'role': 'user', 'content': 'x' * 1000}], [], 'rules', budget={
        'contextWindowTokens': 100, 'reservedOutputTokens': 50, 'safetyMarginTokens': 1})
    assert r['status'] == 'failed'


def test_result_wrong_scope_never_reaches_model():
    class Wrong(Gateway):
        def invoke(self, ctx, call):
            return {**super().invoke(ctx, call), 'loopId': 'other'}
    p = Provider([{'content': '', 'toolCalls': [{'callId': 'a', 'toolName': 'read_code_excerpt', 'arguments': {}}]}])
    r = run_tool_dialogue(context(), {}, p, Wrong(), tools=[])
    assert r['failure']['code'] == 'TOOL_RESULT_MISMATCH'
    assert len(p.requests) == 1


def test_two_invalid_requests_stop():
    p = Provider([{'content': '', 'toolCalls': [{'callId': str(i), 'toolName': 'read_code_excerpt', 'arguments': 'broken'}]} for i in range(2)])
    g = Gateway()
    r = run_tool_dialogue(context(), {}, p, g, tools=[])
    assert r['failure']['code'] == 'CONSECUTIVE_INVALID_REQUESTS'
    assert not g.calls


def test_four_model_requests_limit_and_tool_limit():
    p = Provider([{'content': '', 'toolCalls': [{'callId': str(i), 'toolName': 'read_code_excerpt', 'arguments': {}}]} for i in range(4)])
    g = Gateway()
    r = run_tool_dialogue(context(), {}, p, g, tools=[])
    assert r['failure']['code'] == 'TOOL_LIMIT_EXCEEDED'
    assert len(g.calls) == 3


def test_direct_materials_not_fabricated_tool_messages():
    p = Provider([{'content': 'cannot verify', 'toolCalls': []}])
    r = run_tool_dialogue(context(), {'observation': 'prior user report'}, p, Gateway(), tools=[])
    assert r['status'] == 'answered'
    assert all(m['role'] != 'tool' for m in p.requests[0]['messages'])


def test_bad_context_never_sent_and_tools_are_filtered():
    p = Provider([{'content': 'no run', 'toolCalls': []}])
    assert run_tool_dialogue({}, {}, p, Gateway(), tools=[])['failure']['code'] == 'INVALID_CONTEXT'
    assert not p.requests
    run_tool_dialogue(context(), {}, p, Gateway(), tools=[{'function': {'name': 'run_public_samples'}},
                                                        {'function': {'name': 'read_code_excerpt'}}])
    assert [x['function']['name'] for x in p.requests[0]['tools']] == ['read_code_excerpt']


def test_rejected_public_version_can_be_corrected():
    class RejectFirst(Gateway):
        def invoke(self,ctx,call):
            result=super().invoke(ctx,call)
            result['status']='rejected' if call['arguments']['codeVersion']==999 else 'succeeded'
            return result
    p=Provider([{'content':'','toolCalls':[{'callId':str(v),'toolName':'run_public_samples','arguments':{'codeVersion':v}}]} for v in (999,2)]+[{'content':'本次结果。','toolCalls':[]}])
    g=RejectFirst();ctx=context();ctx['allowedTools']=['run_public_samples']
    run_tool_dialogue(ctx,{},p,g,tools=[])
    assert len(g.calls)==2


def test_short_round_deadline_reaches_gateway():
    class Capture(Gateway):
        def invoke(self,ctx,call):
            self.deadline=datetime.fromisoformat(ctx['deadlineAt'])
            return super().invoke(ctx,call)
    p=Provider([{'content':'','toolCalls':[{'callId':'c','toolName':'read_code_excerpt','arguments':{}}]}, {'content':'结果。','toolCalls':[]}])
    g=Capture();start=datetime.now(timezone.utc)
    run_tool_dialogue(context(),{},p,g,tools=[],duration_seconds=2)
    assert (g.deadline-start).total_seconds()<2.1

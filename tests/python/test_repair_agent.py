import json
from copy import deepcopy
from urllib.error import URLError

import pytest

from apps.api.deepseek_provider import DeepSeekConfig, StructuredChatTransport, ProviderFailure
from focusmate.adapters.repair_agent import DeepSeekRepairProvider
from focusmate.core.repair_agent import (RepairExecutor, RepairValidation, RepairBudgetExceeded,
                                        bind_candidate, new_repair_record)


SCHEMA = {'type': 'object', 'properties': {'source': {'type': 'string'}},
          'required': ['source'], 'additionalProperties': False}


def record(review=0):
    return new_repair_record(repair_id='repair-1', owner={'goalId': 'goal-1', 'codeVersion': 3},
        failure={'code': 'INVALID_SOURCE'}, materials={'input-1': {'error': 'unexpected token'}},
        original={'source': 'broken'}, review_calls=review)


def call(name='submit_repair', args=None):
    return {'content': '', 'toolCalls': [{'callId': 'call-1', 'toolName': name,
            'arguments': json.dumps(args if args is not None else {'replacement': {'source': 'fixed'}})}]}


class Provider:
    def __init__(self, *replies): self.replies = list(replies); self.calls = []
    def complete(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return reply


def execute(provider, original=None, validator=None, **kwargs):
    writes = []
    result = RepairExecutor(provider, clock=lambda: 1).execute(original or record(), candidate_schema=SCHEMA,
        validate=validator or (lambda candidate, budget: RepairValidation(True, {'source': candidate['source']})),
        checkpoint=lambda state: writes.append(deepcopy(state)), guard=kwargs.pop('guard', lambda: None),
        deadline=kwargs.pop('deadline', 10), **kwargs)
    return result, writes


def test_single_candidate_verified_then_resumes_without_model_summary_or_replay():
    provider = Provider(call())
    result, writes = execute(provider)
    assert result['status'] == 'succeeded' and result['modelCalls'] == 1
    assert result['validation']['result'] == {'source': 'fixed'}
    assert [s['status'] for s in writes] == ['running', 'running', 'running', 'submitted', 'validating', 'succeeded']
    assert writes[1]['modelCalls'] == 1 and writes[1]['calls'][0]['status'] == 'started'
    resumed, resumed_writes = execute(provider, result)
    assert resumed == result and resumed_writes == [] and len(provider.calls) == 1


def test_read_registered_material_then_submit_is_two_requests():
    provider = Provider(call('read_material', {'materialIds': ['input-1']}), call())
    result, _ = execute(provider)
    assert result['status'] == 'succeeded' and result['modelCalls'] == 2
    second = provider.calls[1]['messages']
    assert json.loads(second[-1]['content']) == {'input-1': {'error': 'unexpected token'}}
    assert {t['function']['name'] for t in provider.calls[0]['tools']} == {'read_material', 'submit_repair'}


def test_read_tool_advertises_registered_packet_keys_not_nested_evidence_refs():
    original=record()
    original['materials']={'request':{'codeRef':'code:20'},'instructions':'use frozen evidence'}
    provider=Provider(call('read_material',{'materialIds':['request']}),call())
    result,_=execute(provider,original)
    read_tool=provider.calls[0]['tools'][0]['function']
    ids=read_tool['parameters']['properties']['materialIds']['items']['enum']
    assert set(ids)=={'request','instructions'} and 'code:20' not in ids
    assert json.loads(provider.calls[1]['messages'][-1]['content'])=={'request':{'codeRef':'code:20'}}
    assert result['status']=='succeeded' and result['modelCalls']==2


def test_mandatory_review_counts_same_two_calls_and_does_not_allow_another():
    def validate(candidate, budget):
        budget.model_call('review')
        with pytest.raises(RepairBudgetExceeded): budget.model_call('another-review')
        return RepairValidation(True)
    charged = []
    result, _ = execute(Provider(call()), record(review=1), validate, charge_model=charged.append)
    assert result['modelCalls'] == 2 and charged == ['repair', 'review']


def test_review_reservation_prevents_read_loop_consuming_review():
    provider = Provider(call('read_material', {'materialIds': ['input-1']}))
    result, _ = execute(provider, record(review=1))
    assert result['status'] == 'exhausted' and result['modelCalls'] == 1 and result['candidate'] is None
    assert [t['function']['name'] for t in provider.calls[0]['tools']] == ['submit_repair']


def test_duplicate_json_keys_do_not_silently_choose_one_candidate():
    reply = call()
    reply['toolCalls'][0]['arguments'] = '{"replacement":{"source":"a"},"replacement":{"source":"b"}}'
    result, _ = execute(Provider(reply), validator=lambda *_: pytest.fail('duplicate accepted'))
    assert result['status'] == 'failed' and result['candidate'] is None


@pytest.mark.parametrize('reply', [call('shell', {'command': 'anything'}),
    call('read_material', {'materialIds': ['C:/secret']}),
    call('read_material', {'materialIds': ['input-1'], 'path': 'database'}),
    {'toolCalls': [call()['toolCalls'][0], call()['toolCalls'][0]]},
    call(args={'replacement': {'source': 'fixed', 'referenceSource': 'changed'}})])
def test_unauthorized_tool_or_replacement_never_reaches_validator(reply):
    def validate(*_): pytest.fail('untrusted candidate reached validator')
    result, _ = execute(Provider(reply), validator=validate)
    assert result['status'] == 'failed' and result['modelCalls'] == 1


def test_bad_candidate_and_valid_negative_verdict_are_separate():
    result, _ = execute(Provider(call()), validator=lambda c, b: RepairValidation(True, {'status': 'failed'}))
    assert result['status'] == 'succeeded' and result['validation']['result']['status'] == 'failed'
    rejected, _ = execute(Provider(call()), validator=lambda c, b: RepairValidation(False, reason='INVALID_REFERENCE'))
    assert rejected['status'] == 'failed' and rejected['reason'] == 'INVALID_REFERENCE'


def test_same_candidate_is_idempotent_changed_candidate_conflicts():
    state = record()
    assert bind_candidate(state, {'source': 'fixed'})
    assert not bind_candidate(state, {'source': 'fixed'})
    with pytest.raises(ValueError, match='CONFLICT'): bind_candidate(state, {'source': 'different'})


@pytest.mark.parametrize('status', ['running', 'submitted', 'validating'])
def test_crash_does_not_blindly_replay_request_or_validator(status):
    state = record(); state.update(status=status, modelCalls=1)
    result, _ = execute(Provider(), state, validator=lambda *_: pytest.fail('replayed'))
    assert result['status'] == 'interrupted' and result['modelCalls'] == 1


def test_provider_failure_and_abstention_have_no_retries():
    provider = Provider(RuntimeError('private-error'))
    failed, _ = execute(provider)
    assert failed['status'] == 'failed' and len(provider.calls) == 1
    assert 'private-error' not in json.dumps(failed)
    abstained, _ = execute(Provider({'content': '{"cannotRepair":true,"reason":"需要修改参考程序"}'}))
    assert abstained['status'] == 'abstained' and abstained['candidate'] is None


def test_expired_deadline_or_owner_guard_prevents_publication():
    result, _ = execute(Provider(), deadline=0)
    assert result['status'] == 'exhausted' and result['modelCalls'] == 0
    def guard(): raise ValueError('STALE_OWNER')
    with pytest.raises(ValueError, match='STALE_OWNER'): execute(Provider(), guard=guard)


def test_original_materials_are_frozen_by_copy():
    state = record(); copy = deepcopy(state)
    execute(Provider(call()), state)
    assert state == copy


def test_repair_native_provider_never_retries_network_failure():
    requests = []
    def opener(*args, **kwargs):
        requests.append(1); raise URLError('unavailable')
    provider = DeepSeekRepairProvider(DeepSeekConfig('key', 'https://example.com', 'model'), opener=opener, clock=lambda: 0)
    with pytest.raises(Exception): provider.complete(messages=[], tools=[], deadline=10)
    assert len(requests) == 1


def test_json_review_transport_one_attempt_is_explicit(monkeypatch):
    requests = []
    def opener(*args, **kwargs):
        requests.append(1); raise URLError('unavailable')
    monkeypatch.setattr('apps.api.deepseek_provider.urlopen', opener)
    import time
    provider = StructuredChatTransport(DeepSeekConfig('key', 'https://example.com', 'model'), attempts=1)
    with pytest.raises(ProviderFailure): provider.complete(system_prompt='', user_prompt='', deadline=time.monotonic()+5)
    assert len(requests) == 1

"""Request policy boundaries: T042 opts in; older DeepSeek callers retain defaults."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from apps.api import deepseek_provider as deepseek


class Response:
    def __init__(self, message, finish_reason=None, usage=None):
        self.raw = json.dumps({'id': 'response-1', 'model': 'model-1', 'choices': [{'message': message, 'finish_reason': finish_reason}], 'usage': usage}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit=-1):
        return self.raw if limit < 0 else self.raw[:limit]


def capture(monkeypatch, *, reasoning=None, transient_failure=False):
    calls = []
    def open_response(request, timeout):
        calls.append((json.loads(request.data), timeout))
        if transient_failure and len(calls) == 1:
            raise URLError('simulated network failure')
        message = {'content': '{"items":[]}'}
        if reasoning is not None:
            message['reasoning_content'] = reasoning
        return Response(message)
    monkeypatch.setattr(deepseek, 'urlopen', open_response)
    return calls


def config(**kwargs):
    return deepseek.DeepSeekConfig('key', 'https://unit.invalid', 'model-1', **kwargs)


def test_thinking_opt_in_changes_request_parameters(monkeypatch):
    calls = capture(monkeypatch)
    transport = deepseek.StructuredChatTransport(config(thinking_enabled=True))
    transport.complete(system_prompt='review', user_prompt='{}', deadline=time.monotonic() + 5)
    body = calls[0][0]
    assert body['thinking'] == {'type': 'enabled'}
    assert body['reasoning_effort'] == 'low'
    assert 'temperature' not in body
    assert body['response_format'] == {'type': 'json_object'}
    assert 0 < calls[0][1] <= 5


def test_default_structured_request_remains_non_thinking(monkeypatch):
    calls = capture(monkeypatch)
    deepseek.StructuredChatTransport(config()).complete(system_prompt='other-role', user_prompt='{}',
                                                         deadline=time.monotonic() + 5)
    assert calls[0][0]['thinking'] == {'type': 'disabled'}
    assert calls[0][0]['temperature'] == 0
    assert 'reasoning_effort' not in calls[0][0]


def test_transport_discards_reasoning_and_never_sends_it_in_next_request(monkeypatch):
    calls = capture(monkeypatch, reasoning='PRIVATE_CHAIN_OF_THOUGHT')
    transport = deepseek.StructuredChatTransport(config(thinking_enabled=True))
    response = transport.complete(system_prompt='review', user_prompt='first', deadline=time.monotonic() + 5)
    assert asdict(response) == {'response_id': 'response-1', 'model': 'model-1', 'content': '{"items":[]}'}
    transport.complete(system_prompt='review', user_prompt='second', deadline=time.monotonic() + 5)
    assert 'PRIVATE_CHAIN_OF_THOUGHT' not in json.dumps(calls)
    assert 'reasoning_content' not in vars(response)


def test_review_adapter_alone_enables_thinking_and_preserves_budgets(monkeypatch):
    from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
    from focusmate.adapters.goal_compilation import DeepSeekGoalCompilationProvider
    monkeypatch.setattr(deepseek.DeepSeekConfig, 'from_environment', classmethod(lambda cls: config()))
    calls = capture(monkeypatch, reasoning='INTERNAL_REASONING_NOT_RETURNED')
    review = DeepSeekGoalContractReviewProvider.from_environment().complete(system_prompt='review', user_prompt='{}')
    DeepSeekGoalCompilationProvider.from_environment().complete(system_prompt='generate', user_prompt='{}')
    assert calls[0][0]['thinking'] == {'type': 'enabled'}
    assert calls[0][0]['max_tokens'] == 16384
    assert calls[1][0]['thinking'] == {'type': 'disabled'}
    assert calls[1][0]['max_tokens'] == 8192
    assert 'INTERNAL_REASONING_NOT_RETURNED' not in repr(review)


@pytest.mark.parametrize('invalid', ['false', 0, 1, None, []])
def test_non_boolean_thinking_configuration_is_rejected(invalid):
    with pytest.raises(deepseek.ProviderFailure) as caught:
        deepseek.StructuredChatTransport(config(thinking_enabled=invalid))
    assert caught.value.code == 'LLM_REQUEST_REJECTED'
    assert caught.value.retryable is False


def test_legacy_extraction_body_stays_unchanged_even_when_config_opts_in(monkeypatch):
    calls = capture(monkeypatch, reasoning='IGNORED')
    problem = {key: 'public data' for key in ('statement', 'inputSpec', 'outputSpec', 'constraints')}
    problem['examples'] = [{'input': '1', 'output': '1'}]
    deepseek.DeepSeekProvider(config(thinking_enabled=True)).extract(problem, evidence_catalog=[])
    assert calls[0][0]['thinking'] == {'type': 'disabled'}
    assert calls[0][0]['temperature'] == 0
    assert 'reasoning_effort' not in calls[0][0]


def test_thinking_request_keeps_one_network_retry_without_content_repair(monkeypatch):
    calls = capture(monkeypatch, transient_failure=True)
    monkeypatch.setattr(deepseek.time, 'sleep', lambda seconds: None)
    response = deepseek.StructuredChatTransport(config(thinking_enabled=True)).complete(
        system_prompt='review', user_prompt='{}', deadline=time.monotonic() + 5)
    assert response.content == '{"items":[]}'
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert calls[0][0]['thinking'] == {'type': 'enabled'}


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_structured_effort_is_explicit_and_transport_still_discards_reasoning(monkeypatch, effort):
    calls = capture(monkeypatch, reasoning='PRIVATE_REASONING')
    transport = deepseek.StructuredChatTransport(config(thinking_enabled=True, reasoning_effort=effort))
    response = transport.complete(system_prompt='review', user_prompt='{}', deadline=time.monotonic() + 5)
    assert calls[0][0]['reasoning_effort'] == effort
    assert 'PRIVATE_REASONING' not in repr(response)


def test_unknown_reasoning_effort_is_rejected():
    with pytest.raises(deepseek.ProviderFailure):
        deepseek.StructuredChatTransport(config(reasoning_effort='guess'))


def test_output_diagnostics_keep_only_safe_finish_and_token_counts(monkeypatch):
    monkeypatch.setattr(deepseek, 'urlopen', lambda *a, **k: Response(
        {'content': '{"partial":', 'reasoning_content': 'PRIVATE_REASONING'}, 'length',
        {'completion_tokens': 8192, 'completion_tokens_details': {'reasoning_tokens': 7900},
         'untrusted_extra': 'SECRET'}))
    transport = deepseek.StructuredChatTransport(config(thinking_enabled=True))
    transport.complete(system_prompt='JSON', user_prompt='{}', deadline=time.monotonic()+5)
    assert transport.last_response_metadata == {
        'finishReason': 'length', 'completionTokens': 8192, 'reasoningTokens': 7900}
    assert 'PRIVATE_REASONING' not in repr(transport.last_response_metadata)
    assert 'SECRET' not in repr(transport.last_response_metadata)

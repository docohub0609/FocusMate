import json

import pytest

from apps.api.deepseek_provider import DeepSeekConfig
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider, ToolChatError


class Response:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit):
        return json.dumps({'model': 'actual', 'choices': [{'finish_reason': 'tool_calls', 'message': {
            'content': None, 'reasoning_content': 'must-not-retain', 'tool_calls': [
                {'id': 'call-1', 'function': {'name': 'read_code_excerpt', 'arguments': '{"codeVersion":"v2"}'}}]}}]}).encode()


def test_native_transport_nonthinking_sanitized():
    requests = []
    def opener(request, timeout):
        requests.append((json.loads(request.data), timeout))
        return Response()
    provider = DeepSeekToolChatProvider(DeepSeekConfig('secret', 'https://api.deepseek.com', 'model'), opener=opener, clock=lambda: 10)
    result = provider.complete(messages=[{'role': 'user', 'content': 'read'}], tools=[{'type': 'function'}], deadline=20)
    assert requests[0][0]['thinking'] == {'type': 'disabled'}
    assert requests[0][1] == 10
    assert result['toolCalls'][0]['callId'] == 'call-1'
    assert 'must-not-retain' not in json.dumps(result)
    assert 'secret' not in json.dumps(provider.public_config)


def test_service_failure_never_retried_or_leaked():
    calls = []
    def opener(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('secret')
    provider = DeepSeekToolChatProvider(DeepSeekConfig('secret', 'https://api.deepseek.com', 'model'), opener=opener, clock=lambda: 0)
    with pytest.raises(ToolChatError, match='PROVIDER_UNAVAILABLE') as error:
        provider.complete(messages=[], tools=[], deadline=10)
    assert len(calls) == 1
    assert 'secret' not in str(error.value)


def test_deadline_no_network():
    p = DeepSeekToolChatProvider(DeepSeekConfig('x', 'https://api.deepseek.com', 'm'), opener=lambda *a, **k: pytest.fail(), clock=lambda: 10)
    with pytest.raises(ToolChatError, match='DEADLINE_EXCEEDED'):
        p.complete(messages=[], tools=[], deadline=9)


def test_payment_failure_is_specific_without_exposing_response_or_credentials():
    from urllib.error import HTTPError
    def opener(*args,**kwargs):raise HTTPError('https://example.invalid',402,'private response',{},None)
    p=DeepSeekToolChatProvider(DeepSeekConfig('secret','https://api.deepseek.com','m'),opener=opener,clock=lambda:0)
    with pytest.raises(ToolChatError,match='^PROVIDER_PAYMENT_REQUIRED$'):
        p.complete(messages=[],tools=[],deadline=10)


def test_slow_trickle_respects_absolute_deadline():
    now = [0]
    class Slow(Response):
        def read1(self, size):
            now[0] += 2
            return b' '
    p = DeepSeekToolChatProvider(DeepSeekConfig('x', 'https://api.deepseek.com', 'm'),
                                opener=lambda *a, **k: Slow(), clock=lambda: now[0])
    with pytest.raises(ToolChatError, match='DEADLINE_EXCEEDED'):
        p.complete(messages=[], tools=[], deadline=3)
    assert now[0] == 4


@pytest.mark.parametrize('status,retryable',[(400,False),(401,False),(402,False),(403,False),(408,True),(429,True),(500,True),(503,True)])
def test_only_transient_http_failures_allow_bounded_owner_retry(status,retryable):
    from urllib.error import HTTPError
    def opener(*args,**kwargs):raise HTTPError('https://example.invalid',status,'private',{},None)
    provider=DeepSeekToolChatProvider(DeepSeekConfig('secret','https://api.deepseek.com','m'),opener=opener,clock=lambda:0)
    with pytest.raises(ToolChatError) as error:provider.complete(messages=[],tools=[],deadline=10)
    assert error.value.retryable is retryable
    assert 'private' not in str(error.value) and 'secret' not in str(error.value)

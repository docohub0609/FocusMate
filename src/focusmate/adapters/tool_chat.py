"""One native non-thinking tool request. No retries, secrets or reasoning logs."""
import json
import time
from dataclasses import replace
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from apps.api.deepseek_provider import DeepSeekConfig
from focusmate.contracts.model_response import response_audit


class ToolChatError(Exception):
    def __init__(self, code, *, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class DeepSeekToolChatProvider:
    def __init__(self, config, *, opener=urlopen, clock=time.monotonic):
        self.config = replace(config, thinking_enabled=False)
        self.opener, self.clock = opener, clock
        self.public_config = {'provider': 'DeepSeek', 'model': config.model, 'maxTokens': config.max_tokens,
                              'thinkingEnabled': False, 'networkAttempts': 1, 'timeoutSeconds': config.timeout_seconds}

    @classmethod
    def from_environment(cls):
        return cls(DeepSeekConfig.from_environment())

    def request_body(self, messages, tools):
        body = {'model': self.config.model, 'messages': messages, 'stream': False,
                'max_tokens': self.config.max_tokens, 'temperature': 0, 'thinking': {'type': 'disabled'}}
        if tools:
            body.update(tools=tools, tool_choice='auto')
        return body

    def complete(self, *, messages, tools, deadline):
        self.last_response_audit = {}
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ToolChatError('DEADLINE_EXCEEDED')
        url = urlsplit(self.config.base_url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ToolChatError('PROVIDER_CONFIGURATION_INVALID')
        body = self.request_body(messages, tools)
        request = Request(self.config.base_url.rstrip('/') + '/chat/completions',
                          data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
                          headers={'Authorization': 'Bearer ' + self.config.api_key, 'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=min(remaining, self.config.timeout_seconds)) as response:
                raw = self._read_response(response, deadline)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            value = json.loads(raw)
            self.last_response_audit = response_audit(value)
            choice = value['choices'][0]
            if choice.get('finish_reason') not in ('stop', 'tool_calls'):
                raise ValueError()
            message = choice['message']
            content = message.get('content') or ''
            if not isinstance(content, str):
                raise ValueError()
            calls = [{'callId': c['id'], 'toolName': c['function']['name'],
                      'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
            if any(not isinstance(c['callId'], str) or not isinstance(c['toolName'], str) for c in calls):
                raise ValueError()
        except ToolChatError:
            raise
        except (KeyError, TypeError, ValueError):
            raise ToolChatError('PROVIDER_OUTPUT_INVALID') from None
        except HTTPError as exc:
            raise ToolChatError('PROVIDER_PAYMENT_REQUIRED' if exc.code == 402 else 'PROVIDER_UNAVAILABLE',
                                retryable=exc.code in (408,429) or 500 <= exc.code < 600) from None
        except Exception as exc:
            raise ToolChatError('PROVIDER_UNAVAILABLE', retryable=isinstance(exc,(TimeoutError,URLError,ConnectionError))) from None
        if self.clock() >= deadline:
            raise ToolChatError('DEADLINE_EXCEEDED')
        return {'content': content, 'toolCalls': calls, 'model': value.get('model', self.config.model),
                'responseAudit': self.last_response_audit}

    def _read_response(self, response, deadline):
        limit = 2 * 1024 * 1024
        read_one = getattr(response, 'read1', None)
        if read_one is None:
            data = response.read(limit + 1)
            if self.clock() >= deadline:
                raise ToolChatError('DEADLINE_EXCEEDED')
            return data
        data = bytearray()
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ToolChatError('DEADLINE_EXCEEDED')
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(min(remaining, self.config.timeout_seconds))
            part = read_one(min(65536, limit - len(data) + 1))
            if self.clock() >= deadline:
                raise ToolChatError('DEADLINE_EXCEEDED')
            if not part:
                return bytes(data)
            data.extend(part)
            if len(data) > limit:
                raise ValueError('response size')

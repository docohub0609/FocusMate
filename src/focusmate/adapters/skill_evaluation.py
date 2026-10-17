"""Structured transport for offline development evaluations; no Core imports."""
from dataclasses import replace
import time

from apps.api.deepseek_provider import DeepSeekConfig, StructuredChatTransport


class DeepSeekSkillEvaluationProvider:
    def __init__(self, config):
        self.transport = StructuredChatTransport(config)
        self.public_config = {'provider': 'DeepSeek', 'model': config.model,
                              'maxTokens': config.max_tokens, 'thinkingEnabled': config.thinking_enabled,
                              'timeoutSeconds': config.timeout_seconds, 'deadlineSeconds': 120,
                              'networkAttempts': 2, 'reasoningEffort': config.reasoning_effort if config.thinking_enabled else None}

    @classmethod
    def from_environment(cls):
        return cls(replace(DeepSeekConfig.from_environment(), max_tokens=16384,
                           thinking_enabled=True, reasoning_effort='high'))

    def complete(self, *, system_prompt, user_prompt):
        return self.transport.complete(system_prompt=system_prompt, user_prompt=user_prompt,
                                       deadline=time.monotonic() + 120)

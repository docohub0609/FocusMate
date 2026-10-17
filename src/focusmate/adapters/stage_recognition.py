"""DeepSeek transport adapter for stage recognition."""
from apps.api.deepseek_provider import DeepSeekConfig, ProviderFailure, StructuredChatTransport
from focusmate.contracts.skill_execution import ProviderError, ProviderResponse


class DeepSeekStageRecognitionProvider:
    def __init__(self, config, *, transport=None):
        self.transport = transport or StructuredChatTransport(config)
        self.public_config = {'provider': 'DeepSeek', 'model': config.model,
            'maxTokens': config.max_tokens, 'thinkingEnabled': config.thinking_enabled,
            'reasoningEffort': config.reasoning_effort if config.thinking_enabled else None,
            'timeoutSeconds': config.timeout_seconds, 'deadlineSeconds': 120, 'networkAttempts': 2}

    @classmethod
    def from_environment(cls):
        try:
            return cls(DeepSeekConfig.from_environment())
        except ProviderFailure:
            raise ProviderError('PROVIDER_UNAVAILABLE') from None

    def complete(self, *, system_prompt, user_prompt, deadline):
        try:
            value = self.transport.complete(system_prompt=system_prompt, user_prompt=user_prompt, deadline=deadline)
        except ProviderFailure as error:
            code = {'BUILD_BUDGET_EXCEEDED': 'DEADLINE_EXCEEDED',
                    'LLM_OUTPUT_INVALID': 'PROVIDER_OUTPUT_INVALID'}.get(error.code, 'PROVIDER_UNAVAILABLE')
            raise ProviderError(code) from None
        return ProviderResponse(value.content, value.model)

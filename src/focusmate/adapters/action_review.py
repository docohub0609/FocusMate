"""Tool-free review transport; reuse bounded HTTP I/O without changing tool mode."""
from dataclasses import replace
from apps.api.deepseek_provider import DeepSeekConfig
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider, ToolChatError


class DeepSeekActionReviewProvider(DeepSeekToolChatProvider):
    def __init__(self, config, *, thinking=False, **kwargs):
        if type(thinking) is not bool:raise ValueError('INVALID_REVIEW_MODE')
        super().__init__(config, **kwargs)
        self.config=replace(config,thinking_enabled=thinking)
        self.public_config.update(thinkingEnabled=thinking,role='actionReview',
                                  reasoningEffort='high' if thinking else None)

    @classmethod
    def from_environment(cls, *, thinking=False):
        return cls(replace(DeepSeekConfig.from_environment(),max_tokens=8192),thinking=thinking)

    def request_body(self, messages, tools):
        if tools:raise ToolChatError('REVIEW_TOOLS_FORBIDDEN')
        body=super().request_body(messages, [])
        body['response_format']={'type':'json_object'}
        if self.config.thinking_enabled:
            body.pop('temperature',None)
            body.update(thinking={'type':'enabled'},reasoning_effort='high')
        return body

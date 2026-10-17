"""DeepSeek communication adapter; it never imports Core orchestration."""

from __future__ import annotations

import time
from dataclasses import replace

from apps.api.deepseek_provider import DeepSeekConfig, ProviderFailure, StructuredChatTransport
from focusmate.contracts.goal_compilation import GoalCompilationError, GoalCompilationResponse


class DeepSeekGoalCompilationProvider:
    def __init__(self, transport: StructuredChatTransport) -> None:
        self.transport = transport

    @classmethod
    def from_environment(cls) -> "DeepSeekGoalCompilationProvider":
        try:
            config = replace(DeepSeekConfig.from_environment(), max_tokens=8192)
            return cls(StructuredChatTransport(config))
        except ProviderFailure as error:
            raise GoalCompilationError(error.code, "模型配置不可用。", error.retryable) from None

    def complete(self, *, system_prompt: str, user_prompt: str) -> GoalCompilationResponse:
        try:
            response = self.transport.complete(system_prompt=system_prompt, user_prompt=user_prompt,
                                               deadline=time.monotonic() + 120)
        except ProviderFailure as error:
            raise GoalCompilationError(error.code, "模型暂时无法生成合同草稿。", error.retryable) from None
        return GoalCompilationResponse(content=response.content, model=response.model, response_id=response.response_id)

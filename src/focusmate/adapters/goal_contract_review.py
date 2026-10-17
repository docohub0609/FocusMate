"""Fresh bounded DeepSeek review request; no repair and no Core dependency."""
from dataclasses import replace
import time

from apps.api.deepseek_provider import DeepSeekConfig, ProviderFailure, StructuredChatTransport
from focusmate.contracts.goal_compilation import GoalCompilationResponse
from focusmate.contracts.goal_contract_review import GoalContractReviewError


class DeepSeekGoalContractReviewProvider:
    def __init__(self, transport: StructuredChatTransport):
        self.transport = transport

    @classmethod
    def from_environment(cls):
        try:
            config = replace(DeepSeekConfig.from_environment(), max_tokens=16384, thinking_enabled=True)
            return cls(StructuredChatTransport(config))
        except ProviderFailure as error:
            raise GoalContractReviewError(error.code, '合同审核模型配置不可用。', retryable=error.retryable) from None

    def complete(self, *, system_prompt: str, user_prompt: str) -> GoalCompilationResponse:
        try:
            response = self.transport.complete(system_prompt=system_prompt, user_prompt=user_prompt,
                                               deadline=time.monotonic() + 120)
        except ProviderFailure as error:
            raise GoalContractReviewError(error.code, '合同审核服务暂时不可用。', retryable=error.retryable) from None
        return GoalCompilationResponse(response.content, response.model, response.response_id)

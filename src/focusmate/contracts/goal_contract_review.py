"""Neutral review boundary shared by Core, storage and model adapters."""
from typing import Protocol

from focusmate.contracts.goal_compilation import GoalCompilationResponse


class GoalContractReviewError(ValueError):
    def __init__(self, code: str, message: str, issues=None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = list(issues or [])
        self.retryable = retryable


class GoalContractReviewProvider(Protocol):
    def complete(self, *, system_prompt: str, user_prompt: str) -> GoalCompilationResponse: ...

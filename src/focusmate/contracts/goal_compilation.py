"""Neutral boundary between goal compilation and model adapters."""
from dataclasses import dataclass

@dataclass(frozen=True)
class GoalCompilationResponse:
    content: str
    model: str
    response_id: str

class GoalCompilationError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

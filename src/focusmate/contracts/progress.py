"""Progress estimation boundaries; providers and storage do not import orchestration."""
from typing import Protocol
from .skill_execution import ProviderResponse


class ProgressSourceError(ValueError):
    def __init__(self,code):
        super().__init__(code)
        self.code=code


class ProgressMaterialReader(Protocol):
    def resolve(self,ref: str) -> dict: ...


class ProgressProvider(Protocol):
    def complete(self,*,system_prompt: str,user_prompt: str,deadline: float) -> ProviderResponse: ...

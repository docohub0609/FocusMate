"""Transport-neutral visible response and bounded request protocol."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    model: str = 'unknown'


class ProviderError(Exception):
    def __init__(self, code='PROVIDER_FAILED'):
        self.code = code
        super().__init__(code)


class SkillProvider(Protocol):
    def complete(self, *, system_prompt: str, user_prompt: str, deadline: float) -> ProviderResponse: ...

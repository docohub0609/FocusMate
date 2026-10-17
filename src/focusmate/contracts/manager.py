"""Manager boundaries: commands are caller-owned, semantic roles are injected."""
from typing import Protocol, Callable
import json


class ManagerError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def safe_payload(value):
    """Reject known private envelopes, not a claim of understanding arbitrary prose."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace('_', '') in {'apikey','authorization','credentials','secret','hiddentests','oracle','reasoningcontent','backendlogs','internallogs'}:
                raise ManagerError('PRIVATE_FIELD')
            safe_payload(item)
    elif isinstance(value, list):
        for item in value: safe_payload(item)
    if len(encode(value).encode()) > 2 * 1024 * 1024:
        raise ManagerError('INPUT_TOO_LONG')


class ManagerStore(Protocol):
    def create(self, session_id: str, state: dict) -> dict: ...
    def read(self, session_id: str) -> dict: ...
    def change(self, session_id: str, request_id: str, command: dict, operation: Callable) -> dict: ...


class StepExecutor(Protocol):
    def execute(self, state: dict, step: dict) -> dict: ...

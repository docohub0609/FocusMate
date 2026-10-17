"""Collect observations only; receiving one never schedules an action."""
from typing import Protocol


class EventStore(Protocol):
    def receive(self, event: dict, *, trusted_source: str, trusted_actor: str | None = None) -> dict: ...
    def list_goal(self, goal_id: str, *, after_sequence: int = 0, limit: int = 100) -> list: ...


class RawEventCollector:
    def __init__(self, store: EventStore):
        self.store = store

    def collect(self, event, *, trusted_source, trusted_actor=None):
        return self.store.receive(event, trusted_source=trusted_source, trusted_actor=trusted_actor)

    def query(self, goal_id, *, after_sequence=0, limit=100):
        return self.store.list_goal(goal_id, after_sequence=after_sequence, limit=limit)

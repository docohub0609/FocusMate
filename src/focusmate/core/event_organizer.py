"""Incremental organization entry; storage owns its atomic write boundary."""
from datetime import datetime, timezone
from typing import Protocol

from ..contracts.observations import OrganizationPolicy, epoch


class ObservationStore(Protocol):
    def process(self, goal_id: str, *, now: float, policy: OrganizationPolicy) -> dict: ...


class EventOrganizer:
    def __init__(self, store: ObservationStore, *, policy: OrganizationPolicy | None = None):
        self.store = store
        self.policy = policy or OrganizationPolicy()

    def process(self, goal_id, *, now=None):
        if not isinstance(goal_id, str) or not goal_id:
            raise ValueError('goal is required')
        return self.store.process(goal_id, now=epoch(now or datetime.now(timezone.utc)), policy=self.policy)

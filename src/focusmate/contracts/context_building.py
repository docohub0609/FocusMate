"""Provider-neutral source resolution for model input assembly."""
from typing import Protocol


class ContextResolver(Protocol):
    """Resolve already-authorized immutable source records, never arbitrary URLs.

    A source includes ref, goalContext, kind, version, contentHash and content.
    Dialogue additionally supplies sequence and contiguous turnId grouping.
    Callers validate mandatoryPayload before building (T054 validates its package,
    materials and Goal context); resolver scope checks are defense in depth.
    """

    def resolve(self, ref: str) -> dict: ...

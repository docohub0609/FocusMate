"""Dependency-free health contract for the API shell.

Business endpoints intentionally do not belong to V1-T001.
"""

from __future__ import annotations

from typing import Final

SERVICE_NAME: Final = "focusmate-api"


def build_health_payload() -> dict[str, str]:
    """Return the stable process-health payload."""

    return {"service": SERVICE_NAME, "status": "ok", "version": "v1"}

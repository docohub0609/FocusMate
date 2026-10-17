"""Health contract for the isolated OJ worker shell."""

from __future__ import annotations

from typing import Final

SERVICE_NAME: Final = "focusmate-oj-worker"


def build_health_payload() -> dict[str, str]:
    return {"service": SERVICE_NAME, "status": "ok", "version": "v1"}

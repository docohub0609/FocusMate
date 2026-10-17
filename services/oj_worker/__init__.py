"""Independent OJ worker process boundary.

No user code execution is implemented in V1-T001.
"""

from .health import build_health_payload

__all__ = ["build_health_payload"]

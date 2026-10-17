"""Run the explicit, real-Docker OJ isolation suite."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    environment = os.environ.copy()
    environment["RUN_DOCKER_OJ_INTEGRATION"] = "1"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-vv", "tests/python/test_docker_runner_integration.py"],
        cwd=root,
        env=environment,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())

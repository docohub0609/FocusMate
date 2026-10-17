"""Read-only prerequisite check for the local Docker OJ runtime."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys


def check(command: list[str], label: str) -> bool:
    try:
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        print(f"[missing] {label}")
        return False
    if result.returncode == 0:
        print(f"[ok] {label}")
        return True
    print(f"[missing] {label}")
    return False


def main() -> int:
    healthy = True
    if platform.system() == "Windows":
        healthy = check(["wsl", "--status"], "WSL2 is available") and healthy
    if shutil.which("docker") is None:
        print("[missing] Docker CLI is available")
        return 1
    print("[ok] Docker CLI is available")
    healthy = check(["docker", "info"], "Docker daemon is running") and healthy
    healthy = check(
        ["docker", "image", "inspect", "focusmate-python-runner:v1"],
        "focusmate-python-runner:v1 image exists",
    ) and healthy
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())

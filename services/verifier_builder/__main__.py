"""Independent verifier-builder process. Only fixed safe status is printed."""

import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from services.oj_worker.docker_runner import DockerPythonRunner
from .build_jobs import VerifierBuildJobRepository
from .candidates import CandidateFailure, CandidateProvider
from .mutation_candidates import MutationProvider
from .worker import VerifierBuilderWorker


ROOT = Path(__file__).resolve().parents[2]


def preflight():
    provider = CandidateProvider.from_environment()
    for command in (["docker", "info"], ["docker", "image", "inspect", "focusmate-python-runner:v1"]):
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False, shell=False)
            if result.returncode != 0:
                raise OSError("runtime unavailable")
        except (OSError, subprocess.SubprocessError):
            raise CandidateFailure("VERIFIER_BUILDER_INTERNAL_ERROR", "The Docker runtime is unavailable.", True) from None
    return provider


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the FocusMate verifier builder")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--health", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--poll", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--build-seconds", type=float, default=600.0,
                        help="Total preparation budget; does not change per-program limits")
    args = parser.parse_args(argv)
    if args.health:
        print(json.dumps({"service": "focusmate-verifier-builder", "status": "ok"}))
        return 0
    if not math.isfinite(args.build_seconds) or not 0 < args.build_seconds <= 1800:
        parser.error("build duration must be finite and in (0, 1800]")
    if args.poll_seconds <= 0 or args.lease_seconds <= 0:
        parser.error("poll and lease durations must be positive")
    try:
        # Resolve configuration/runtime before opening the job database or claiming.
        provider = preflight()
        mutation_provider = MutationProvider.from_environment()
        jobs = VerifierBuildJobRepository(Path(os.getenv("FOCUSMATE_DB_PATH", str(ROOT / "data" / "focusmate.db"))))
        jobs.requeue_configuration_paused()
        worker = VerifierBuilderWorker(jobs, provider, DockerPythonRunner(startup_timeout_seconds=10), f"builder-{uuid4()}",
            Path(os.getenv("FOCUSMATE_BUILDER_WORKSPACE_ROOT", str(ROOT / "data" / "verifier-workspaces"))),
            lease_seconds=args.lease_seconds, build_seconds=args.build_seconds,
            mutation_provider=mutation_provider)
        while True:
            outcome = worker.run_once()
            if outcome is not None:
                print(json.dumps(asdict(outcome)))
            if args.once:
                if outcome is None: print(json.dumps({"status": "idle"}))
                return 0
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except CandidateFailure as error:
        print(json.dumps({"status": "unavailable", "issueCode": error.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "unavailable", "issueCode": "VERIFIER_BUILDER_INTERNAL_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

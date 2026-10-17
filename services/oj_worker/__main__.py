"""Run the independent OJ worker process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from uuid import uuid4

from .docker_runner import DockerPythonRunner
from .health import build_health_payload
from .judge import JudgeSubmissionProcessor, OJResultRepository
from .queue import WorkerQueue
from .sqlite_assets import SQLiteTrustedAssetProvider
from .worker import Worker


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "focusmate.db"
DEFAULT_WORKSPACE_ROOT = PROJECT_ROOT / "data" / "oj-workspaces"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FocusMate OJ worker")
    parser.add_argument("--health", action="store_true", help="Print health JSON and exit")
    parser.add_argument("--once", action="store_true", help="Attempt one queue poll and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument('--task-kind', choices=['submissions','runs'], default='submissions')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.health:
        print(json.dumps(build_health_payload()))
        return 0
    if args.poll_seconds <= 0 or args.lease_seconds <= 0:
        print("poll and lease durations must be positive", file=sys.stderr)
        return 2

    database = Path(os.getenv("FOCUSMATE_DB_PATH", str(DEFAULT_DATABASE)))
    workspace_root = Path(
        os.getenv("FOCUSMATE_OJ_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_ROOT))
    )
    worker_id = os.getenv(
        "FOCUSMATE_WORKER_ID",
        f"{socket.gethostname()}-{uuid4().hex[:12]}",
    )
    if args.task_kind == 'runs':
        from .editor_runs import RunRepository, RunWorker, report_worker_error
        worker = RunWorker(RunRepository(database), workspace_root / 'editor-runs')
        print(json.dumps({'service':'focusmate-run-worker','status':'polling'}), flush=True)
        try:
            while True:
                try:
                    processed = worker.process_once()
                except Exception as exc:
                    # Claim/storage failures may happen outside the per-task boundary.
                    # Existing leases expire normally; do not re-enqueue unknown executions.
                    report_worker_error('poll', exc)
                    if args.once: return 1
                    time.sleep(args.poll_seconds)
                    continue
                if args.once: return 0
                if processed is None: time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            return 0
    queue = WorkerQueue(database)
    processor = JudgeSubmissionProcessor(
        DockerPythonRunner(startup_timeout_seconds=10),
        SQLiteTrustedAssetProvider(database),
        OJResultRepository(database),
    )
    worker = Worker(
        queue, processor, worker_id, workspace_root,
        lease_seconds=args.lease_seconds,
    )
    print(json.dumps({
        "service": "focusmate-oj-worker", "status": "polling",
        "workerId": worker_id, "database": str(database),
    }))
    from .editor_runs import report_worker_error
    try:
        while True:
            try:
                processed = worker.process_once()
            except Exception as exc:
                report_worker_error('poll',exc,service='focusmate-oj-worker')
                if args.once:return 1
                time.sleep(args.poll_seconds)
                continue
            if args.once:
                print(json.dumps({'status':'idle' if processed is None else 'processed','submissionId':processed}))
                return 0
            if processed is None:time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

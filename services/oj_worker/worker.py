"""Reliable orchestration around a replaceable, future sandbox runner."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import tempfile
import time
from threading import Event, Lock, Thread
from typing import Any, Protocol

from .queue import Claim, QueueFailure, WorkItem, WorkerQueue


class SubmissionProcessor(Protocol):
    """Future Judge boundary; a single-case PythonRunner is not this component."""

    def process(self, item: WorkItem, workspace: Path) -> None: ...


class AtomicSubmissionProcessor(Protocol):
    """Optional processor path whose durable result is committed by the Worker."""

    def prepare_owned(self, item: WorkItem, workspace: Path) -> object: ...

    def finalize_owned(
        self, item: WorkItem, claim: Claim, prepared: object, queue: WorkerQueue,
    ) -> object: ...


class ProcessorTimeout(Exception):
    """Signal that processing exceeded its allowed orchestration time."""


@dataclass
class Worker:
    queue: WorkerQueue
    processor: SubmissionProcessor
    worker_id: str
    workspace_root: Path
    lease_seconds: int = 60

    def process_once(self) -> str | None:
        self.recover_expired()
        claim = self.queue.claim_next(self.worker_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        workspace: Path | None = None
        release_claim = False
        started, stage = time.monotonic(), 'load'
        try:
            item = self.queue.load_work_item(claim)
            actual_hash = hashlib.sha256(item.source_code.encode("utf-8")).hexdigest()
            if actual_hash != item.content_hash:
                raise QueueFailure("The code snapshot hash does not match its source.")
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(
                prefix=f"focusmate-{item.submission_id}-", dir=self.workspace_root,
            ))
            self.queue.set_workspace(claim, workspace)
            (workspace / "main.py").write_text(item.source_code, encoding="utf-8")
            self.queue.transition_owned(claim, "preparing", "running")
            stage = 'execute'
            prepare_owned = getattr(self.processor, "prepare_owned", None)
            finalize_owned = getattr(self.processor, "finalize_owned", None)
            with _LeaseHeartbeat(self.queue, claim, self.lease_seconds) as heartbeat:
                if callable(prepare_owned) and callable(finalize_owned):
                    prepared = prepare_owned(item, workspace)
                else:
                    self.processor.process(item, workspace)
                    prepared = None
                heartbeat.ensure_owned()

            # Close the small gap between stopping the heartbeat and committing.
            self.queue.renew(claim, lease_seconds=self.lease_seconds)
            stage = 'saveResult'
            if callable(prepare_owned) and callable(finalize_owned):
                finalize_owned(item, claim, prepared, self.queue)
                # Atomic finalization already removed this exact claim generation.
            else:
                self.queue.transition_owned(claim, "running", "finished")
                release_claim = True
        except Exception as exc:
            failure = getattr(exc, 'failure', None) or {'stage': stage, 'errorType': type(exc).__name__, 'retryCount': 0}
            failure = {**failure, 'taskElapsedMs': round((time.monotonic()-started)*1000)}
            release_claim = self._finish_safely(claim, 'expired' if isinstance(exc, ProcessorTimeout) else 'failed', failure=failure)
        finally:
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)
            if release_claim:
                self.queue.release(claim)
        return claim.submission_id

    def recover_expired(self) -> int:
        claims = self.queue.expire_stale()
        for claim in claims:
            if claim.workspace_path:
                self._remove_owned_workspace(Path(claim.workspace_path), claim)
        return len(claims)

    def _finish_safely(self, claim: Claim, target: str, *, failure=None) -> bool:
        for source in ("running", "preparing"):
            try:
                self.queue.transition_owned(claim, source, target, failure=failure)
                return True
            except QueueFailure:
                continue
        return False

    def _remove_owned_workspace(self, path: Path, claim: Claim) -> None:
        root = self.workspace_root.resolve()
        resolved = path.resolve()
        expected_prefix = f"focusmate-{claim.submission_id}-"
        if resolved.parent == root and resolved.name.startswith(expected_prefix):
            shutil.rmtree(resolved, ignore_errors=True)


class _LeaseHeartbeat:
    """Renew a live claim while an arbitrary processor performs blocking work."""

    def __init__(self, queue: WorkerQueue, claim: Claim, lease_seconds: int) -> None:
        self.queue = queue
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.interval = max(0.1, min(20.0, lease_seconds / 3))
        self._stop = Event()
        self._lock = Lock()
        self._failure: BaseException | None = None
        self._thread: Thread | None = None

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread = Thread(
            target=self._run,
            name=f"claim-heartbeat-{self.claim.submission_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.queue.renew(self.claim, lease_seconds=self.lease_seconds)
            except BaseException as exc:  # surfaced on the Worker thread below
                with self._lock:
                    self._failure = exc
                self._stop.set()
                return

    def ensure_owned(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise QueueFailure("The worker could not renew its claim.") from failure

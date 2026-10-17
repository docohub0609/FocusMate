from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from apps.api.__main__ import create_server
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from apps.api.schema_validation import validate
from services.verifier_builder.build_jobs import VerifierBuildJobRepository
from tests.python.test_problem_features import INTAKE
from tests.python.test_submission_storage import frozen_problem


NOW = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def jobs(
    path: Path,
    clock: MutableClock,
    tokens: Iterator[str] | None = None,
) -> VerifierBuildJobRepository:
    token_values = tokens or iter(["claim-one", "claim-two", "claim-three"])
    return VerifierBuildJobRepository(
        path,
        clock=clock,
        job_id_factory=lambda: "build-job-one",
        token_factory=lambda: next(token_values),
    )


def test_discovery_claims_one_frozen_problem_once_and_ignores_drafts(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    frozen_problem(path)
    draft = create_problem_package(INTAKE, id_factory=lambda: "problem-draft")
    ProblemRepository(path).create(draft)
    clock = MutableClock()
    repository = jobs(path, clock)

    claim = repository.discover_and_claim("builder-a", 30)

    assert claim is not None
    assert (claim.problem_id, claim.problem_version) == ("problem-submit", 1)
    assert claim.worker_id == "builder-a"
    assert claim.claim_token == "claim-one"
    assert repository.discover_and_claim("builder-b", 30) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM verifier_build_jobs").fetchone()[0] == 1


def test_two_connections_compete_for_one_queued_job_and_only_one_claims(tmp_path: Path) -> None:
    path = tmp_path / "competing-claims.db"
    frozen_problem(path)
    clock = MutableClock()
    first = VerifierBuildJobRepository(
        path,
        clock=clock,
        job_id_factory=lambda: "build-job-first",
        token_factory=lambda: "claim-first",
    )
    second = VerifierBuildJobRepository(
        path,
        clock=clock,
        job_id_factory=lambda: "build-job-second",
        token_factory=lambda: "claim-second",
    )
    barrier = threading.Barrier(3)
    claims: list[object] = []
    failures: list[BaseException] = []

    def claim(repository: VerifierBuildJobRepository, worker_id: str) -> None:
        try:
            barrier.wait(timeout=2)
            claims.append(repository.discover_and_claim(worker_id, 30))
        except BaseException as error:
            failures.append(error)

    threads = [
        threading.Thread(target=claim, args=(first, "builder-a")),
        threading.Thread(target=claim, args=(second, "builder-b")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=3)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert len([item for item in claims if item is not None]) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM verifier_build_jobs").fetchone()[0] == 1


def test_new_claim_lease_starts_after_waiting_for_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "claim-after-lock.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    connection_requested = threading.Event()
    original_connect = repository._connect

    def tracked_connect() -> sqlite3.Connection:
        connection_requested.set()
        return original_connect()

    repository._connect = tracked_connect  # type: ignore[method-assign]
    result: list[object] = []
    failures: list[BaseException] = []
    blocker = sqlite3.connect(path, timeout=5)
    blocker.execute("BEGIN IMMEDIATE")

    def claim() -> None:
        try:
            result.append(repository.discover_and_claim("builder-a", 30))
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=claim)
    thread.start()
    assert connection_requested.wait(timeout=2)
    clock.value += timedelta(seconds=20)
    blocker.commit()
    blocker.close()
    thread.join(timeout=3)

    assert not failures
    assert not thread.is_alive()
    claim_result = result[0]
    assert claim_result is not None
    assert claim_result.claimed_at == "2026-09-09T09:00:20Z"
    assert claim_result.lease_expires_at == "2026-09-09T09:00:50Z"


def test_restart_preserves_running_job_and_safe_status(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    repository.discover_and_claim("builder-a", 30)

    reopened = jobs(path, clock, iter(["claim-after-restart"]))
    status = reopened.get("problem-submit", 1)

    validate("VerifierAssetBuildStatus", status)
    assert status == {
        "jobId": "build-job-one",
        "schemaVersion": "1.0.0",
        "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
        "status": "running",
        "phase": "generating",
        "attempt": 1,
        "retryable": False,
        "issueCode": None,
        "message": "Verifier asset candidates are being generated.",
        "allowedAction": "wait",
        "createdAt": "2026-09-09T09:00:00Z",
        "updatedAt": "2026-09-09T09:00:00Z",
    }


def test_expired_lease_pauses_job_without_rejecting_asset(tmp_path: Path) -> None:
    path = tmp_path / "expired.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    claim = repository.discover_and_claim("builder-a", 10)
    assert claim is not None
    clock.value += timedelta(seconds=10)

    assert repository.discover_and_claim("builder-b", 30) is None

    status = repository.get("problem-submit", 1)
    assert status["status"] == "paused"
    assert status["issueCode"] == "BUILDER_LEASE_EXPIRED"
    assert status["retryable"] is True
    assert status["allowedAction"] == "retry"
    assert ProblemRepository(path).get("problem-submit", 1)["problemPackage"]["verifierAssetStatus"] == "missing"


def test_claim_token_guards_transitions_and_retry_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "transitions.db"
    frozen_problem(path)
    clock = MutableClock()
    tokens = iter(["claim-one", "claim-two"])
    repository = jobs(path, clock, tokens)
    first = repository.discover_and_claim("builder-a", 30)
    assert first is not None
    assert repository.set_phase(first, "prechecking")["phase"] == "prechecking"
    renewed = repository.renew(first, 60)
    assert renewed.lease_expires_at == "2026-09-09T09:01:00Z"
    assert repository.pause(renewed, "MUTATION_EVIDENCE_INSUFFICIENT")["status"] == "paused"

    first_retry = repository.retry("problem-submit", 1)
    duplicate_retry = repository.retry("problem-submit", 1)
    assert duplicate_retry == first_retry
    assert first_retry["status"] == "queued"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM verifier_build_jobs").fetchone()[0] == 1

    second = repository.discover_and_claim("builder-b", 30)
    assert second is not None
    assert second.claim_token == "claim-two"
    assert repository.get("problem-submit", 1)["attempt"] == 2
    with pytest.raises(StorageFailure):
        repository.complete(first)
    assert repository.complete(second)["status"] == "completed"


@pytest.mark.parametrize("issue", [
    "CANDIDATE_SCHEMA_INVALID", "CANDIDATE_COMPILE_FAILED", "GENERATOR_NON_DETERMINISTIC",
    "GENERATOR_CASES_INVALID", "REFERENCE_PUBLIC_MISMATCH", "REFERENCE_DISAGREEMENT", "REFERENCE_EXECUTION_FAILED",
    "MUTATION_EVIDENCE_INSUFFICIENT",
])
def test_content_issue_retry_clears_checkpoint_atomically(tmp_path, issue):
    path = tmp_path / "retry-content.db"
    frozen_problem(path)
    repository = jobs(path, MutableClock())
    claim = repository.discover_and_claim("builder-a", 30)
    repository.save_checkpoint(claim, {"candidate": "old"}, {"outputs": "old"})
    repository.pause(claim, issue)

    assert repository.retry("problem-submit", 1)["status"] == "queued"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM verifier_candidate_sets").fetchone()[0] == 0

    new_claim = repository.discover_and_claim("builder-b", 30)
    repository.save_checkpoint(new_claim, {"candidate": "new"})
    assert repository.retry("problem-submit", 1)["status"] == "running"
    assert repository.checkpoint(new_claim) == {"candidate": "new"}


@pytest.mark.parametrize("issue", [
    "BUILD_BUDGET_EXCEEDED", "BUILDER_LEASE_EXPIRED", "VERIFIER_BUILDER_INTERNAL_ERROR",
    "LLM_TEMPORARILY_UNAVAILABLE",
])
def test_interruption_retry_keeps_checkpoint(tmp_path, issue):
    path = tmp_path / "retry-interruption.db"
    frozen_problem(path)
    repository = jobs(path, MutableClock())
    claim = repository.discover_and_claim("builder-a", 30)
    repository.save_checkpoint(claim, {"candidate": "reuse"}, {"outputs": "cached"})
    repository.pause(claim, issue)
    repository.retry("problem-submit", 1)
    recovered = repository.discover_and_claim("builder-b", 30)
    assert repository.checkpoint(recovered) == {"candidate": "reuse"}
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT prechecked_json FROM verifier_candidate_sets").fetchone()[0]) == {"outputs": "cached"}


def test_failed_retry_rolls_back_checkpoint_deletion(tmp_path):
    path = tmp_path / "retry-rollback.db"
    frozen_problem(path)
    repository = jobs(path, MutableClock())
    claim = repository.discover_and_claim("builder-a", 30)
    repository.save_checkpoint(claim, {"candidate": "keep-on-rollback"})
    repository.pause(claim, "GENERATOR_CASES_INVALID")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER abort_retry BEFORE UPDATE ON verifier_build_jobs
                      WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT, 'retry interrupted'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="retry interrupted"):
        repository.retry("problem-submit", 1)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status,issue_code FROM verifier_build_jobs").fetchone() == ("paused", "GENERATOR_CASES_INVALID")
        assert json.loads(db.execute("SELECT candidate_json FROM verifier_candidate_sets").fetchone()[0]) == {"candidate": "keep-on-rollback"}


@pytest.mark.parametrize("operation", ["renew", "set_phase", "pause", "complete"])
def test_owned_operation_rejects_claim_that_expires_while_waiting_for_lock(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / f"expired-during-{operation}.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    claim = repository.discover_and_claim("builder-a", 10)
    assert claim is not None
    connection_requested = threading.Event()
    original_connect = repository._connect

    def tracked_connect() -> sqlite3.Connection:
        connection_requested.set()
        return original_connect()

    repository._connect = tracked_connect  # type: ignore[method-assign]
    failures: list[BaseException] = []
    blocker = sqlite3.connect(path, timeout=5)
    blocker.execute("BEGIN IMMEDIATE")

    def operate() -> None:
        try:
            if operation == "renew":
                repository.renew(claim, 30)
            elif operation == "set_phase":
                repository.set_phase(claim, "prechecking")
            elif operation == "pause":
                repository.pause(claim, "BUILD_BUDGET_EXCEEDED")
            else:
                repository.complete(claim)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=operate)
    thread.start()
    assert connection_requested.wait(timeout=2)
    clock.value += timedelta(seconds=10)
    blocker.commit()
    blocker.close()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], StorageFailure)
    assert failures[0].code == "BUILD_JOB_CLAIM_LOST"


def test_retry_timestamp_is_read_after_waiting_for_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "retry-after-lock.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    claim = repository.discover_and_claim("builder-a", 30)
    assert claim is not None
    repository.pause(claim, "BUILD_BUDGET_EXCEEDED")
    connection_requested = threading.Event()
    original_connect = repository._connect

    def tracked_connect() -> sqlite3.Connection:
        connection_requested.set()
        return original_connect()

    repository._connect = tracked_connect  # type: ignore[method-assign]
    results: list[dict[str, object]] = []
    failures: list[BaseException] = []
    blocker = sqlite3.connect(path, timeout=5)
    blocker.execute("BEGIN IMMEDIATE")

    def retry() -> None:
        try:
            results.append(repository.retry("problem-submit", 1))
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=retry)
    thread.start()
    assert connection_requested.wait(timeout=2)
    clock.value += timedelta(seconds=20)
    blocker.commit()
    blocker.close()
    thread.join(timeout=3)

    assert not failures
    assert not thread.is_alive()
    assert results[0]["status"] == "queued"
    assert results[0]["updatedAt"] == "2026-09-09T09:00:20Z"


def _request(base: str, method: str, path: str, body: object | None = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_http_exposes_only_safe_status_and_rejects_browser_state_control(tmp_path: Path) -> None:
    path = tmp_path / "api.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    claim = repository.discover_and_claim("builder-secret", 30)
    assert claim is not None
    repository.pause(claim, "CANDIDATE_COMPILE_FAILED")
    server = create_server(
        port=0,
        problem_repository=ProblemRepository(path),
        verifier_build_job_repository=repository,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = "/api/v1/problems/problem-submit/versions/1/verifier-asset-build"
    try:
        status_code, status = _request(f"http://127.0.0.1:{server.server_port}", "GET", endpoint)
        assert status_code == 200
        validate("VerifierAssetBuildStatus", status)
        assert set(status) == {
            "jobId", "schemaVersion", "problemRef", "status", "phase", "attempt",
            "retryable", "issueCode", "message", "allowedAction", "createdAt", "updatedAt",
        }
        serialized = json.dumps(status).lower()
        for secret in ["builder-secret", "claim-one", "sourcecode", "prompt", "stdin", "expectedoutput", "stacktrace"]:
            assert secret not in serialized

        rejected_code, rejected = _request(
            f"http://127.0.0.1:{server.server_port}", "POST", endpoint + "/retry",
            {"status": "completed", "phase": "sealing", "sourceCode": "secret"},
        )
        assert rejected_code == 422
        assert rejected["code"] == "INVALID_BUILD_RETRY_REQUEST"
        assert repository.get("problem-submit", 1)["status"] == "paused"

        retry_code, queued = _request(
            f"http://127.0.0.1:{server.server_port}", "POST", endpoint + "/retry", {},
        )
        duplicate_code, duplicate = _request(
            f"http://127.0.0.1:{server.server_port}", "POST", endpoint + "/retry", {},
        )
        assert (retry_code, duplicate_code) == (200, 200)
        assert duplicate == queued
        assert queued["status"] == "queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_retry_rejects_json_null_with_safe_422_response(tmp_path: Path) -> None:
    path = tmp_path / "api-null.db"
    frozen_problem(path)
    clock = MutableClock()
    repository = jobs(path, clock)
    claim = repository.discover_and_claim("builder-a", 30)
    assert claim is not None
    repository.pause(claim, "BUILD_BUDGET_EXCEEDED")
    server = create_server(
        port=0,
        problem_repository=ProblemRepository(path),
        verifier_build_job_repository=repository,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = "/api/v1/problems/problem-submit/versions/1/verifier-asset-build/retry"
    request = Request(
        f"http://127.0.0.1:{server.server_port}" + endpoint,
        data=b"null",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            payload = json.load(response)
            assert response.status == 422
        assert payload["code"] == "INVALID_BUILD_RETRY_REQUEST"
        assert set(payload) == {
            "requestId", "schemaVersion", "createdAt", "code", "message", "fieldErrors",
        }
        assert repository.get("problem-submit", 1)["status"] == "paused"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_requires_an_existing_frozen_problem_context(tmp_path: Path) -> None:
    path = tmp_path / "draft-api.db"
    ProblemRepository(path).create(create_problem_package(INTAKE, id_factory=lambda: "problem-draft"))
    repository = jobs(path, MutableClock())
    server = create_server(
        port=0,
        problem_repository=ProblemRepository(path),
        verifier_build_job_repository=repository,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for method, body in [("GET", None), ("POST", {})]:
            status, error = _request(
                base,
                method,
                "/api/v1/problems/problem-draft/versions/1/verifier-asset-build"
                + ("/retry" if method == "POST" else ""),
                body,
            )
            assert status == 409
            assert error["code"] == "BUILD_JOB_PROBLEM_NOT_FROZEN"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

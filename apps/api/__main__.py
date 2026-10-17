"""Run the minimal API health server with the Python standard library."""

from __future__ import annotations

import argparse
import json
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypeAlias

from .deepseek_provider import DeepSeekProvider, ProviderFailure
from .health import build_health_payload
from .problem_intake import (
    IntakeFailure,
    build_error_payload,
    create_problem_package,
    new_request_id,
    timestamp,
    utc_now,
)
from .problem_features import (
    FeatureExtractionFailure,
    build_human_feature_set,
    build_feature_error_payload,
    extract_problem_features,
    validate_feature_request,
)
from .problem_storage import ProblemRepository, StorageFailure
from .feature_jobs import FeatureJobs, ExtractionRuntime
from .intake_web import get_operation
from .editor_http import handle_editor
from .coach_http import handle_coach
from .schema_validation import validate, validation_errors
from .submission_storage import SubmissionFailure, SubmissionRepository
from .verifier_assets import VerifierAssetRepository
from services.verifier_builder.build_jobs import VerifierBuildJobRepository

ServerAddress: TypeAlias = tuple[str, int]
_REQUEST_BODY_FAILED = object()


class HealthHandler(BaseHTTPRequestHandler):
    """Expose the health check and the V1 problem-intake endpoint."""

    max_request_bytes = 1024 * 1024
    feature_provider_factory = staticmethod(DeepSeekProvider.from_environment)
    problem_repository: ProblemRepository
    submission_repository: SubmissionRepository
    verifier_asset_repository: VerifierAssetRepository
    verifier_build_job_repository: VerifierBuildJobRepository

    def _send_json(
        self,
        status: HTTPStatus,
        payload: object,
        *,
        request_id: str | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if request_id is not None:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(body)

    def _send_problem_error(
        self,
        status: HTTPStatus,
        request_id: str,
        code: str,
        message: str,
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        payload = build_error_payload(
            request_id,
            code,
            message,
            field_errors or [],
        )
        self._send_json(status, payload, request_id=request_id)

    def do_GET(self) -> None:  # noqa: N802 - method name is defined by BaseHTTPRequestHandler
        if handle_coach(self, 'GET'): return
        if handle_editor(self, 'GET'):
            return
        operation = get_operation(self)
        if operation:
            self._storage_call(operation)
            return
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, build_health_payload())
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)", self.path)
        if match:
            self._storage_call(lambda: self.problem_repository.get(match.group(1)))
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions", self.path)
        if match:
            self._storage_call(
                lambda: {"problemId": match.group(1), "versions": self.problem_repository.list_versions(match.group(1))}
            )
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)", self.path)
        if match:
            self._storage_call(lambda: self.problem_repository.get(match.group(1), int(match.group(2))))
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)/features/current", self.path)
        if match:
            self._storage_call(
                lambda: self.problem_repository.current_feature_set(match.group(1), int(match.group(2)))
            )
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)/verifier-asset", self.path)
        if match:
            self._storage_call(
                lambda: self.verifier_asset_repository.verification_availability(
                    match.group(1), int(match.group(2)),
                )
            )
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)/verifier-asset-build", self.path)
        if match:
            self._build_job_call(
                lambda: self.verifier_build_job_repository.get(
                    match.group(1), int(match.group(2)),
                )
            )
            return
        match = re.fullmatch(r"/api/v1/goals/([^/]+)", self.path)
        if match:
            self._submission_call(lambda: self.submission_repository.get_goal(match.group(1)))
            return
        match = re.fullmatch(r"/api/v1/goals/([^/]+)/code-snapshots/(\d+)", self.path)
        if match:
            self._submission_call(
                lambda: self.submission_repository.get_code_snapshot(match.group(1), int(match.group(2)))
            )
            return
        match = re.fullmatch(r"/api/v1/submissions/([^/]+)", self.path)
        if match:
            self._submission_call(lambda: self.submission_repository.get_submission(match.group(1)))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/api/v1/problems/([^/]+)", self.path)
        if match:
            self._storage_call(lambda: self.problem_repository.set_deleted(match[1], True))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        if handle_editor(self, 'PUT'):
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)", self.path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("expectedDraftRevision"), int):
            self._send_storage_failure(
                request_id, StorageFailure(
                    "INVALID_PROBLEM_UPDATE",
                    "expectedDraftRevision and complete problem fields are required.", 422, [],
                ),
            )
            return
        intake = dict(payload)
        expected = intake.pop("expectedDraftRevision")
        try:
            candidate = create_problem_package(intake)
            existing = self.problem_repository.get(match.group(1), int(match.group(2)))["problemPackage"]
            candidate.update({
                "problemId": existing["problemId"], "version": existing["version"],
                "createdAt": existing["createdAt"], "verifierAssetStatus": existing["verifierAssetStatus"],
            })
            result = self.problem_repository.update_draft(
                match.group(1), int(match.group(2)), candidate, expected,
            )
        except IntakeFailure as failure:
            self._send_problem_error(
                HTTPStatus.UNPROCESSABLE_ENTITY, request_id, failure.code, failure.message, failure.field_errors,
            )
            return
        except StorageFailure as failure:
            self._send_storage_failure(request_id, failure)
            return
        self._send_json(HTTPStatus.OK, result, request_id=request_id)

    def do_POST(self) -> None:  # noqa: N802 - method name is defined by BaseHTTPRequestHandler
        if handle_coach(self, 'POST'): return
        if handle_editor(self, 'POST'):
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/restore", self.path)
        if match:
            request_id = new_request_id()
            payload = self._request_json(request_id)
            if payload is _REQUEST_BODY_FAILED:
                return
            if payload != {}:
                self._send_storage_failure(request_id, StorageFailure('INVALID_PROBLEM_UPDATE', 'Expected an empty object.', 422, []))
                return
            self._storage_call(lambda: self.problem_repository.set_deleted(match[1], False))
            return
        if self.path == '/api/v1/feature-extraction-jobs':
            request_id = new_request_id()
            payload = self._request_json(request_id)
            if payload is _REQUEST_BODY_FAILED:
                return
            try:
                result = self.feature_jobs.enqueue(payload, self.headers.get('Idempotency-Key'), self.extraction_runtime.owner)
            except StorageFailure as failure:
                self._send_storage_failure(request_id, failure)
                return
            self._send_json(HTTPStatus.ACCEPTED, result, request_id=request_id)
            return
        if self.path == "/api/v1/problems":
            self._handle_problem_intake()
            return
        if self.path == "/api/v1/problem-feature-extractions":
            self._handle_problem_feature_extraction()
            return
        if self.path == "/api/v1/goals":
            self._handle_goal_create()
            return
        if self.path == "/api/v1/submissions":
            self._handle_submission_create()
            return
        match = re.fullmatch(r"/api/v1/goals/([^/]+)/code-snapshots", self.path)
        if match:
            self._handle_code_snapshot_create(match.group(1))
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions", self.path)
        if match:
            self._handle_problem_revision(match.group(1))
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)/freeze", self.path)
        if match:
            self._handle_problem_freeze(match.group(1), int(match.group(2)))
            return
        match = re.fullmatch(r"/api/v1/problems/([^/]+)/versions/(\d+)/confirm-and-freeze", self.path)
        if match:
            self._handle_feature_confirmation(match.group(1), int(match.group(2)))
            return
        match = re.fullmatch(
            r"/api/v1/problems/([^/]+)/versions/(\d+)/verifier-asset-build/retry",
            self.path,
        )
        if match:
            self._handle_verifier_build_retry(match.group(1), int(match.group(2)))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_verifier_build_retry(self, problem_id: str, version: int) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        if payload != {}:
            self._send_build_job_failure(
                request_id,
                StorageFailure(
                    "INVALID_BUILD_RETRY_REQUEST",
                    "The retry request must be an empty JSON object.", 422, [],
                ),
            )
            return
        try:
            result = self.verifier_build_job_repository.retry(problem_id, version)
        except StorageFailure as failure:
            self._send_build_job_failure(request_id, failure)
            return
        self._send_json(HTTPStatus.OK, result, request_id=request_id)

    def _handle_problem_intake(self) -> None:
        request_id = new_request_id()
        if self.headers.get_content_type() != "application/json":
            self._discard_rejected_body()
            self._send_problem_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                request_id,
                "UNSUPPORTED_MEDIA_TYPE",
                "Content-Type must be application/json.",
            )
            return

        content_length = self._content_length()
        if content_length > self.max_request_bytes:
            self.close_connection = True
            self._send_problem_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                request_id,
                "REQUEST_BODY_TOO_LARGE",
                "The request body exceeds the 1 MiB limit.",
            )
            return

        try:
            payload = self._read_json(content_length)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_problem_error(
                HTTPStatus.BAD_REQUEST,
                request_id,
                "INVALID_JSON",
                "The request body must be a valid JSON object.",
            )
            return

        try:
            problem = create_problem_package(payload)
        except IntakeFailure as failure:
            self._send_problem_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                request_id,
                failure.code,
                failure.message,
                failure.field_errors,
            )
            return
        except Exception:
            self._send_problem_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                request_id,
                "PROBLEM_INTAKE_INTERNAL_ERROR",
                "The problem could not be created safely.",
            )
            return

        try:
            record = self.problem_repository.create(problem, operation_key=self.headers.get('Idempotency-Key'))
        except StorageFailure as failure:
            self._send_storage_failure(request_id, failure)
            return
        except Exception:
            self._send_storage_failure(
                request_id, StorageFailure(
                    "PROBLEM_STORAGE_INTERNAL_ERROR", "The problem could not be stored safely.", 500, [],
                ),
            )
            return
        self._send_json(HTTPStatus.CREATED, record, request_id=request_id)

    def _handle_problem_feature_extraction(self) -> None:
        request_id = new_request_id()
        if self.headers.get_content_type() != "application/json":
            self._discard_rejected_body()
            self._send_feature_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                request_id,
                FeatureExtractionFailure(
                    "UNSUPPORTED_MEDIA_TYPE",
                    "Content-Type must be application/json.",
                    False,
                    [],
                ),
            )
            return

        content_length = self._content_length()
        if content_length > self.max_request_bytes:
            self.close_connection = True
            self._send_feature_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                request_id,
                FeatureExtractionFailure(
                    "REQUEST_BODY_TOO_LARGE",
                    "The request body exceeds the 1 MiB limit.",
                    False,
                    [],
                ),
            )
            return

        try:
            payload = self._read_json(content_length)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_feature_error(
                HTTPStatus.BAD_REQUEST,
                request_id,
                FeatureExtractionFailure("INVALID_JSON", "The request body must be valid JSON.", False, []),
            )
            return

        problem_ref = self._extract_problem_ref(payload)
        try:
            errors = validation_errors("ProblemFeatureExtractionRequest", payload)
            if errors:
                raise FeatureExtractionFailure(
                    "INVALID_EXTRACTION_REQUEST", "The feature extraction request is invalid.", False, [],
                )
            stored_request = isinstance(payload, dict) and "problemRef" in payload
            if stored_request:
                ref = payload["problemRef"]
                record = self.problem_repository.get(ref["problemId"], ref["problemVersion"])
                extraction_request = {"problemPackage": record["problemPackage"]}
                problem_ref = ref
            else:
                extraction_request = payload
            validate_feature_request(extraction_request)
            if stored_request:
                result = self._stored_extraction(record, request_id)
            else:
                provider = self.feature_provider_factory()
                result = extract_problem_features(extraction_request, provider)
        except ProviderFailure as failure:
            self._send_feature_error(
                self._feature_error_status(failure.code),
                request_id,
                FeatureExtractionFailure(failure.code, failure.message, failure.retryable, []),
                problem_ref,
            )
            return
        except FeatureExtractionFailure as failure:
            self._send_feature_error(
                self._feature_error_status(failure.code),
                request_id,
                failure,
                problem_ref,
            )
            return
        except StorageFailure as failure:
            self._send_storage_failure(request_id, failure)
            return
        except Exception:
            self._send_feature_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                request_id,
                FeatureExtractionFailure(
                    "FEATURE_EXTRACTION_INTERNAL_ERROR",
                    "The problem features could not be created safely.",
                    True,
                    [],
                ),
                problem_ref,
            )
            return
        self._send_json(HTTPStatus.CREATED, result, request_id=request_id)

    def _stored_extraction(self, record, request_id):
        """Legacy synchronous callers join the same durable job as the webpage."""
        problem, lifecycle = record['problemPackage'], record['lifecycle']
        job = self.feature_jobs.enqueue({
            'problemId': problem['problemId'], 'problemVersion': problem['version'],
            'expectedDraftRevision': lifecycle['draftRevision'], 'contentHash': lifecycle['contentHash'],
        }, 'legacy-' + request_id, self.extraction_runtime.owner)
        deadline = time.monotonic() + 150
        while job['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.05)
            job = self.feature_jobs.get(job['jobId'])
        if job['status'] == 'succeeded':
            return self.problem_repository.get_feature_set(job['featureSetId'])
        if job['status'] == 'stale':
            raise StorageFailure('FEATURE_STALE', '题目内容已变化，请重新解析。', 409, [])
        code = job.get('failureCode', 'LLM_UNAVAILABLE')
        if not code.startswith('LLM_'):
            code = 'LLM_UNAVAILABLE'
        raise FeatureExtractionFailure(code, '本次解析未完成，可查询任务状态后重新发起。', False, [])

    def _handle_problem_revision(self, problem_id: str) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        try:
            validate("ProblemRevisionRequest", payload)
            result = self.problem_repository.create_revision(problem_id, payload["baseVersion"])
        except Exception as error:
            if isinstance(error, StorageFailure):
                self._send_storage_failure(request_id, error)
            else:
                self._send_storage_failure(
                    request_id, StorageFailure(
                        "INVALID_REVISION_REQUEST", "baseVersion is required and must be valid.", 422, [],
                    ),
                )
            return
        self._send_json(HTTPStatus.CREATED, result, request_id=request_id)

    def _handle_problem_freeze(self, problem_id: str, version: int) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        try:
            validate("ProblemFreezeRequest", payload)
            result = self.problem_repository.freeze(
                problem_id, version, payload["featureSetId"], payload["expectedDraftRevision"],
            )
        except Exception as error:
            if isinstance(error, StorageFailure):
                self._send_storage_failure(request_id, error)
            else:
                self._send_storage_failure(
                    request_id, StorageFailure(
                        "INVALID_FREEZE_REQUEST",
                        "featureSetId and expectedDraftRevision are required.", 422, [],
                    ),
                )
            return
        self._send_json(HTTPStatus.OK, result, request_id=request_id)

    def _handle_feature_confirmation(self, problem_id: str, version: int) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        try:
            if not isinstance(payload, dict):
                raise FeatureExtractionFailure(
                    "INVALID_FEATURE_CONFIRMATION", "The confirmation request must be an object.",
                    False, [],
                )
            record = self.problem_repository.get(problem_id, version)
            based_on = payload.get("basedOnFeatureSetId")
            base = self.problem_repository.get_feature_set(based_on) if based_on else None
            reviewed = build_human_feature_set(
                record["problemPackage"], payload, base_feature_set=base,
            )
            frozen = self.problem_repository.confirm_and_freeze(
                problem_id, version, reviewed, payload["expectedDraftRevision"], based_on,
            )
        except FeatureExtractionFailure as failure:
            self._send_feature_error(
                HTTPStatus.UNPROCESSABLE_ENTITY, request_id, failure,
                {"problemId": problem_id, "problemVersion": version},
            )
            return
        except StorageFailure as failure:
            self._send_storage_failure(request_id, failure)
            return
        except Exception:
            self._send_feature_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, request_id,
                FeatureExtractionFailure(
                    "FEATURE_CONFIRMATION_INTERNAL_ERROR",
                    "The reviewed features could not be saved safely.", False, [],
                ),
                {"problemId": problem_id, "problemVersion": version},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {"problemRecord": frozen, "featureSet": reviewed},
            request_id=request_id,
        )

    def _handle_goal_create(self) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        try:
            validate("GoalCreateRequest", payload)
            result = self.submission_repository.create_goal(payload["problemRef"])
        except SubmissionFailure as failure:
            self._send_submission_failure(request_id, failure)
            return
        except Exception:
            self._send_submission_failure(
                request_id, SubmissionFailure(
                    "INVALID_GOAL_REQUEST", "A valid frozen problem reference is required.", 422,
                ),
            )
            return
        self._send_json(HTTPStatus.CREATED, result, request_id=request_id)

    def _handle_code_snapshot_create(self, goal_id: str) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        try:
            validate("CodeSnapshotCreateRequest", payload)
            result = self.submission_repository.create_code_snapshot(
                goal_id, payload["sourceCode"], payload["language"],
            )
        except SubmissionFailure as failure:
            self._send_submission_failure(request_id, failure)
            return
        except Exception:
            self._send_submission_failure(
                request_id, SubmissionFailure(
                    "INVALID_CODE_SNAPSHOT", "Non-blank Python 3 source code is required.", 422,
                ),
            )
            return
        self._send_json(HTTPStatus.CREATED, result, request_id=request_id)

    def _handle_submission_create(self) -> None:
        request_id = new_request_id()
        payload = self._request_json(request_id)
        if payload is _REQUEST_BODY_FAILED:
            return
        try:
            validate("SubmissionCreateRequest", payload)
            result, created = self.submission_repository.create_submission(payload)
        except SubmissionFailure as failure:
            self._send_submission_failure(request_id, failure)
            return
        except Exception:
            self._send_submission_failure(
                request_id, SubmissionFailure(
                    "INVALID_SUBMISSION_REQUEST", "The submission request is invalid.", 422,
                ),
            )
            return
        self._send_json(HTTPStatus.CREATED if created else HTTPStatus.OK, result, request_id=request_id)

    def _request_json(self, request_id: str) -> object:
        if self.headers.get_content_type() != "application/json":
            self._discard_rejected_body()
            self._send_problem_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, request_id, "UNSUPPORTED_MEDIA_TYPE",
                "Content-Type must be application/json.",
            )
            return _REQUEST_BODY_FAILED
        length = self._content_length()
        if length > self.max_request_bytes:
            self._send_problem_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, request_id, "REQUEST_BODY_TOO_LARGE",
                "The request body exceeds the 1 MiB limit.",
            )
            return _REQUEST_BODY_FAILED
        try:
            return self._read_json(length)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_problem_error(
                HTTPStatus.BAD_REQUEST, request_id, "INVALID_JSON",
                "The request body must be valid JSON.",
            )
            return _REQUEST_BODY_FAILED

    def _storage_call(self, operation: object) -> None:
        request_id = new_request_id()
        try:
            result = operation()
        except StorageFailure as failure:
            self._send_storage_failure(request_id, failure)
            return
        self._send_json(HTTPStatus.OK, result, request_id=request_id)

    def _build_job_call(self, operation: object) -> None:
        request_id = new_request_id()
        try:
            result = operation()
        except StorageFailure as failure:
            self._send_build_job_failure(request_id, failure)
            return
        self._send_json(HTTPStatus.OK, result, request_id=request_id)

    def _send_build_job_failure(self, request_id: str, failure: StorageFailure) -> None:
        payload = {
            "requestId": request_id,
            "schemaVersion": "1.0.0",
            "createdAt": timestamp(utc_now()),
            "code": failure.code,
            "message": failure.message,
            "fieldErrors": failure.field_errors,
        }
        self._send_json(HTTPStatus(failure.status), payload, request_id=request_id)

    def _submission_call(self, operation: object) -> None:
        request_id = new_request_id()
        try:
            result = operation()
        except SubmissionFailure as failure:
            self._send_submission_failure(request_id, failure)
            return
        self._send_json(HTTPStatus.OK, result, request_id=request_id)

    def _send_submission_failure(self, request_id: str, failure: SubmissionFailure) -> None:
        payload = {
            "requestId": request_id, "schemaVersion": "1.0.0",
            "createdAt": timestamp(utc_now()), "code": failure.code,
            "message": failure.message, "fieldErrors": [],
        }
        validate("SubmissionError", payload)
        self._send_json(HTTPStatus(failure.status), payload, request_id=request_id)

    def _send_storage_failure(self, request_id: str, failure: StorageFailure) -> None:
        payload = {
            "requestId": request_id, "schemaVersion": "1.0.0",
            "createdAt": timestamp(utc_now()), "code": failure.code,
            "message": failure.message, "fieldErrors": failure.field_errors,
        }
        validate("ProblemStorageError", payload)
        self._send_json(HTTPStatus(failure.status), payload, request_id=request_id)

    def _content_length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length", "0")))
        except ValueError:
            return 0

    def _discard_rejected_body(self) -> None:
        """Avoid resetting a normal upload before its 415 response is received.

        Only drain a bounded declared body, with a total deadline even if the
        sender trickles bytes. This connection is never reused after rejection.
        """
        self.close_connection = True
        remaining = self._content_length()
        if not 0 < remaining <= self.max_request_bytes:
            return
        deadline = time.monotonic() + 0.25
        previous_timeout = self.connection.gettimeout()
        try:
            while remaining:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass  # Missing body must not turn a media-type rejection into a hang.
        finally:
            self.connection.settimeout(previous_timeout)

    def _read_json(self, content_length: int) -> object:
        raw_body = self.rfile.read(content_length)
        return json.loads(raw_body.decode("utf-8"))

    def _send_feature_error(
        self,
        status: HTTPStatus,
        request_id: str,
        failure: FeatureExtractionFailure,
        problem_ref: dict[str, object] | None = None,
    ) -> None:
        payload = build_feature_error_payload(
            request_id,
            failure,
            problem_ref=problem_ref,
        )
        self._send_json(status, payload, request_id=request_id)

    @staticmethod
    def _extract_problem_ref(payload: object) -> dict[str, object] | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("problemPackage"), dict):
            return None
        problem = payload["problemPackage"]
        if isinstance(problem.get("problemId"), str) and isinstance(problem.get("version"), int):
            return {"problemId": problem["problemId"], "problemVersion": problem["version"]}
        return None

    @staticmethod
    def _feature_error_status(code: str) -> HTTPStatus:
        if code == "INVALID_EXTRACTION_REQUEST":
            return HTTPStatus.UNPROCESSABLE_ENTITY
        if code in {"LLM_CONFIGURATION_MISSING", "LLM_UNAVAILABLE"}:
            return HTTPStatus.SERVICE_UNAVAILABLE
        if code in {
            "LLM_AUTHENTICATION_FAILED",
            "LLM_PAYMENT_REQUIRED",
            "LLM_REQUEST_REJECTED",
            "LLM_OUTPUT_EMPTY",
            "LLM_OUTPUT_INVALID",
            "LLM_EVIDENCE_INVALID",
        }:
            return HTTPStatus.BAD_GATEWAY
        return HTTPStatus.INTERNAL_SERVER_ERROR

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    feature_provider_factory: object | None = None,
    problem_repository: ProblemRepository | None = None,
    submission_repository: SubmissionRepository | None = None,
    verifier_asset_repository: VerifierAssetRepository | None = None,
    verifier_build_job_repository: VerifierBuildJobRepository | None = None,
) -> ThreadingHTTPServer:
    """Create, but do not start, the API health server."""

    class ConfiguredHandler(HealthHandler):
        pass

    if feature_provider_factory is not None:
        ConfiguredHandler.feature_provider_factory = staticmethod(feature_provider_factory)
    ConfiguredHandler.problem_repository = problem_repository or ProblemRepository()
    ConfiguredHandler.submission_repository = submission_repository or SubmissionRepository(
        ConfiguredHandler.problem_repository.path,
    )
    ConfiguredHandler.verifier_asset_repository = verifier_asset_repository or VerifierAssetRepository(
        ConfiguredHandler.problem_repository.path,
    )
    ConfiguredHandler.verifier_build_job_repository = (
        verifier_build_job_repository
        or VerifierBuildJobRepository(ConfiguredHandler.problem_repository.path)
    )
    ConfiguredHandler.feature_jobs = FeatureJobs(ConfiguredHandler.problem_repository)
    class IntakeServer(ThreadingHTTPServer):
        def server_close(self):
            runtime = getattr(ConfiguredHandler, 'extraction_runtime', None)
            if runtime:
                runtime.close()
            super().server_close()
    server = IntakeServer((host, port), ConfiguredHandler)
    try:
        ConfiguredHandler.extraction_runtime = ExtractionRuntime(ConfiguredHandler.feature_jobs, ConfiguredHandler.feature_provider_factory)
    except Exception:
        server.server_close()
        raise
    return server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FocusMate API shell")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = create_server(args.host, args.port)
    print(f"FocusMate API health server listening on http://{args.host}:{server.server_port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

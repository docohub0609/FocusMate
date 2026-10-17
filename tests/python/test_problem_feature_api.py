from __future__ import annotations

from contextlib import contextmanager
import json
import threading
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apps.api.__main__ import create_server
from apps.api.deepseek_provider import ProviderResponse
from apps.api.problem_features import FeatureProvider
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository
from apps.api.schema_validation import validate
from tests.python.test_problem_features import FakeProvider, INTAKE, base_features


class ApiFakeProvider(FeatureProvider):
    review = FakeProvider.review

    def extract(
        self, problem: dict[str, object], *, evidence_catalog: list[dict[str, str]],
        repair_reason: str | None = None,
    ) -> ProviderResponse:
        return ProviderResponse(
            "deepseek-response-api",
            "deepseek-v4-flash",
            json.dumps({"features": base_features()}),
        )


@contextmanager
def running_api() -> Iterator[str]:
    with TemporaryDirectory() as directory:
        server = create_server(
            port=0, feature_provider_factory=ApiFakeProvider,
            problem_repository=ProblemRepository(Path(directory) / "test.db"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def post(base_url: str, body: object) -> tuple[int, dict[str, object], str | None]:
    request = Request(
        f"{base_url}/api/v1/problem-feature-extractions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers.get("X-Request-ID")


def test_feature_extraction_http_endpoint_returns_created_features() -> None:
    problem = create_problem_package(INTAKE)
    with running_api() as base_url:
        status, result, request_id = post(base_url, {"problemPackage": problem})
    assert status == 201
    assert request_id and request_id.startswith("request-")
    validate("ProblemFeatures", result)


def test_feature_extraction_http_endpoint_returns_request_level_error() -> None:
    with running_api() as base_url:
        status, error, request_id = post(base_url, {"problemPackage": {"problemId": "broken"}})
    assert status == 422
    assert error["requestId"] == request_id
    assert "goalContext" not in error
    assert error["userMessage"]
    assert "createFeaturesManually" in error["allowedActions"]
    validate("ProblemFeatureExtractionError", error)


def test_invalid_request_does_not_construct_or_call_a_paid_provider() -> None:
    def forbidden_provider() -> ApiFakeProvider:
        raise AssertionError("provider must not be created for an invalid request")

    with TemporaryDirectory() as directory:
        server = create_server(
            port=0, feature_provider_factory=forbidden_provider,
            problem_repository=ProblemRepository(Path(directory) / "test.db"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, error, _ = post(
                f"http://127.0.0.1:{server.server_port}",
                {"problemPackage": {"problemId": "broken"}},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    assert status == 422
    assert error["code"] == "INVALID_EXTRACTION_REQUEST"

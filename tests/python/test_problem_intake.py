from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from apps.api.__main__ import create_server
from apps.api.problem_intake import IntakeFailure, create_problem_package
from apps.api.problem_storage import ProblemRepository
from apps.api.schema_validation import validate


BASE_REQUEST = {
    "statement": "Return the sum of two integers.",
    "inputSpec": "Two integers on one line.",
    "outputSpec": "Their sum.",
    "constraints": "-1000 <= a,b <= 1000",
    "examples": [{"input": "1 2\n", "output": "3\n"}],
    "language": "python3",
    "ioMode": "standardInputOutput",
}


@contextmanager
def running_api() -> Iterator[str]:
    with TemporaryDirectory() as directory:
        server = create_server(
            port=0, problem_repository=ProblemRepository(Path(directory) / "test.db"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def post(
    base_url: str,
    body: object,
    *,
    content_type: str = "application/json",
) -> tuple[int, dict[str, object], str | None]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    request = Request(
        f"{base_url}/api/v1/problems",
        data=data,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    with response:
        return (
            response.status,
            json.load(response),
            response.headers.get("X-Request-ID"),
        )


@pytest.mark.parametrize(
    "examples",
    [
        [{"input": "1 2\n", "output": "3\n"}],
        [
            {"input": "1 2\n", "output": "3\n"},
            {"input": "-4 4\n", "output": "0\n"},
        ],
        [{"input": "3\n1 2 3\n", "output": "6\n"}],
    ],
)
def test_create_problem_package_accepts_three_valid_problem_shapes(
    examples: list[dict[str, str]],
) -> None:
    request = deepcopy(BASE_REQUEST)
    request["examples"] = examples
    package = create_problem_package(
        request,
        clock=lambda: datetime(2026, 8, 14, 2, 30, tzinfo=timezone.utc),
        id_factory=lambda: "problem-fixed",
    )

    assert package["problemId"] == "problem-fixed"
    assert package["createdAt"] == "2026-08-14T02:30:00Z"
    assert package["examples"] == examples
    assert package["languagePolicy"] == {"allowed": ["python3"]}
    assert package["ioMode"] == "standardInputOutput"
    assert package["verifierAssetStatus"] == "missing"
    validate("ProblemPackage", package)


@pytest.mark.parametrize(
    ("path", "expected_field"),
    [
        ("statement", "statement"),
        ("inputSpec", "inputSpec"),
        ("outputSpec", "outputSpec"),
        ("constraints", "constraints"),
        ("examples", "examples"),
    ],
)
def test_missing_required_problem_fields_are_explained(
    path: str, expected_field: str
) -> None:
    request = deepcopy(BASE_REQUEST)
    del request[path]

    with pytest.raises(IntakeFailure) as caught:
        create_problem_package(request)

    assert caught.value.code == "INVALID_PROBLEM_INPUT"
    assert any(error["field"] == expected_field for error in caught.value.field_errors)


@pytest.mark.parametrize("missing", ["input", "output"])
def test_example_must_include_input_and_output(missing: str) -> None:
    request = deepcopy(BASE_REQUEST)
    del request["examples"][0][missing]

    with pytest.raises(IntakeFailure) as caught:
        create_problem_package(request)

    assert caught.value.field_errors[0]["field"] == f"examples.0.{missing}"


def test_blank_descriptions_and_unknown_fields_are_rejected() -> None:
    blank = deepcopy(BASE_REQUEST)
    blank["statement"] = " \n\t "
    with pytest.raises(IntakeFailure) as caught:
        create_problem_package(blank)
    assert caught.value.field_errors[0]["code"] == "blank"

    unknown = deepcopy(BASE_REQUEST)
    unknown["verifierAssetStatus"] = "ready"
    with pytest.raises(IntakeFailure) as caught:
        create_problem_package(unknown)
    assert caught.value.field_errors[0] == {
        "field": "verifierAssetStatus",
        "code": "unknownField",
        "message": "verifierAssetStatus is not accepted.",
    }


def test_http_success_returns_created_problem_and_request_id() -> None:
    with running_api() as base_url:
        status, body, request_id = post(base_url, BASE_REQUEST)

    assert status == 201
    assert request_id and request_id.startswith("request-")
    package = body["problemPackage"]
    assert package["problemId"].startswith("problem-")  # type: ignore[index,union-attr]
    assert package["verifierAssetStatus"] == "missing"  # type: ignore[index]
    assert body["lifecycle"]["status"] == "draft"  # type: ignore[index]
    validate("ProblemRecord", body)


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ({**BASE_REQUEST, "language": "cpp17"}, "UNSUPPORTED_LANGUAGE"),
        ({**BASE_REQUEST, "ioMode": "functionCall"}, "UNSUPPORTED_IO_MODE"),
    ],
)
def test_http_rejects_unsupported_v1_options(
    body: dict[str, object], expected_code: str
) -> None:
    with running_api() as base_url:
        status, error, request_id = post(base_url, body)

    assert status == 422
    assert error["code"] == expected_code
    assert error["requestId"] == request_id
    validate("ProblemIntakeError", error)


def test_http_rejects_invalid_json_without_leaking_internal_details() -> None:
    with running_api() as base_url:
        status, error, request_id = post(base_url, b"{not-json")

    assert status == 400
    assert error["code"] == "INVALID_JSON"
    assert error["requestId"] == request_id
    assert "Traceback" not in json.dumps(error)
    validate("ProblemIntakeError", error)


def test_http_rejects_non_json_content_type() -> None:
    with running_api() as base_url:
        status, error, request_id = post(
            base_url,
            BASE_REQUEST,
            content_type="text/plain",
        )

    assert status == 415
    assert error["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert error["requestId"] == request_id
    validate("ProblemIntakeError", error)


def test_http_rejects_oversized_request() -> None:
    with running_api() as base_url:
        host_port = base_url.removeprefix("http://")
        connection = HTTPConnection(host_port, timeout=2)
        connection.putrequest("POST", "/api/v1/problems")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(1024 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        status = response.status
        request_id = response.getheader("X-Request-ID")
        error = json.load(response)
        connection.close()

    assert status == 413
    assert error["code"] == "REQUEST_BODY_TOO_LARGE"
    assert error["requestId"] == request_id
    validate("ProblemIntakeError", error)


def test_http_field_error_is_a_request_level_error_without_fake_goal() -> None:
    request = deepcopy(BASE_REQUEST)
    del request["constraints"]
    with running_api() as base_url:
        status, error, request_id = post(base_url, request)

    assert status == 422
    assert error["requestId"] == request_id
    assert "goalContext" not in error
    assert error["fieldErrors"][0]["field"] == "constraints"  # type: ignore[index]
    validate("ProblemIntakeError", error)

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apps.api.__main__ import create_server
from apps.api.deepseek_provider import ProviderResponse
from apps.api.problem_storage import ProblemRepository
from apps.api.schema_validation import validate
from tests.python.test_problem_features import FakeProvider, INTAKE, base_features


class LifecycleProvider:
    review = FakeProvider.review

    def extract(
        self, problem: dict[str, object], *, evidence_catalog: list[dict[str, str]],
        repair_reason: str | None = None,
    ) -> ProviderResponse:
        return ProviderResponse(
            "response-lifecycle", "deepseek-v4-flash",
            json.dumps({"features": base_features()}),
        )


@contextmanager
def running_api(tmp_path: Path) -> Iterator[str]:
    server = create_server(
        port=0, feature_provider_factory=LifecycleProvider,
        problem_repository=ProblemRepository(tmp_path / "api.db"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(base: str, method: str, path: str, body: object | None = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    call = Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        response = urlopen(call, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_stored_problem_can_be_read_parsed_frozen_and_revised(tmp_path: Path) -> None:
    with running_api(tmp_path) as base:
        status, created = request(base, "POST", "/api/v1/problems", INTAKE)
        assert status == 201
        validate("ProblemRecord", created)
        problem = created["problemPackage"]
        problem_id = problem["problemId"]

        status, loaded = request(base, "GET", f"/api/v1/problems/{problem_id}/versions/1")
        assert status == 200
        assert loaded == created

        status, features = request(
            base, "POST", "/api/v1/problem-feature-extractions",
            {"problemRef": {"problemId": problem_id, "problemVersion": 1}},
        )
        assert status == 201
        validate("ProblemFeatures", features)

        status, current = request(
            base, "GET", f"/api/v1/problems/{problem_id}/versions/1/features/current",
        )
        assert status == 200
        assert current["featureSetId"] == features["featureSetId"]

        status, frozen = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions/1/freeze",
            {"featureSetId": features["featureSetId"], "expectedDraftRevision": 1},
        )
        assert status == 200
        assert frozen["lifecycle"]["status"] == "frozen"
        assert frozen["problemPackage"]["verifierAssetStatus"] == "missing"

        status, error = request(
            base, "PUT", f"/api/v1/problems/{problem_id}/versions/1",
            {**INTAKE, "expectedDraftRevision": 1},
        )
        assert status == 409
        assert error["code"] == "PROBLEM_VERSION_FROZEN"
        validate("ProblemStorageError", error)

        status, revised = request(
            base, "POST", f"/api/v1/problems/{problem_id}/versions", {"baseVersion": 1},
        )
        assert status == 201
        assert revised["problemPackage"]["version"] == 2
        assert revised["lifecycle"]["status"] == "draft"

        status, versions = request(base, "GET", f"/api/v1/problems/{problem_id}/versions")
        assert status == 200
        assert [item["version"] for item in versions["versions"]] == [1, 2]

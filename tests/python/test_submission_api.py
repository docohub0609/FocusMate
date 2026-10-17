from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from apps.api.__main__ import create_server
from apps.api.problem_storage import ProblemRepository
from apps.api.schema_validation import validate
from apps.api.submission_storage import SubmissionRepository
from tests.python.test_submission_storage import frozen_problem


@contextmanager
def running_api(tmp_path: Path) -> Iterator[str]:
    path = tmp_path / "submission-api.db"
    frozen_problem(path)
    problems = ProblemRepository(path)
    submissions = SubmissionRepository(
        path,
        goal_id_factory=lambda: "goal-api",
        snapshot_id_factory=lambda: "snapshot-api",
        submission_id_factory=lambda: "submission-api",
    )
    server = create_server(
        port=0, problem_repository=problems, submission_repository=submissions,
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


def test_real_api_flow_creates_goal_snapshot_and_awaiting_submission(tmp_path: Path) -> None:
    with running_api(tmp_path) as base:
        status, goal = request(
            base, "POST", "/api/v1/goals",
            {"problemRef": {"problemId": "problem-submit", "problemVersion": 1}},
        )
        assert status == 201
        validate("GoalRecord", goal)

        status, snapshot = request(
            base, "POST", "/api/v1/goals/goal-api/code-snapshots",
            {"language": "python3", "sourceCode": "print(input())\n"},
        )
        assert status == 201
        validate("CodeSnapshot", snapshot)

        payload = {
            "goalContext": {"goalId": "goal-api", "scope": "goal"},
            "problemRef": {"problemId": "problem-submit", "problemVersion": 1},
            "codeVersion": 1, "language": "python3", "idempotencyKey": "click-api-1",
        }
        status, submission = request(base, "POST", "/api/v1/submissions", payload)
        assert status == 201
        assert submission["status"] == "awaitingAssets"
        validate("Submission", submission)
        assert "sourceCode" not in submission
        assert "verdict" not in submission

        status, duplicate = request(base, "POST", "/api/v1/submissions", payload)
        assert status == 200
        assert duplicate == submission

        status, loaded = request(base, "GET", "/api/v1/submissions/submission-api")
        assert status == 200
        assert loaded == submission


def test_api_rejects_blank_code_and_unknown_goal_without_executing_code(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist.txt"
    dangerous_text = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    with running_api(tmp_path) as base:
        status, error = request(
            base, "POST", "/api/v1/goals/missing/code-snapshots",
            {"language": "python3", "sourceCode": dangerous_text},
        )
        assert status == 404
        assert error["code"] == "GOAL_NOT_FOUND"
        validate("SubmissionError", error)
        assert not marker.exists()

        status, goal = request(
            base, "POST", "/api/v1/goals",
            {"problemRef": {"problemId": "problem-submit", "problemVersion": 1}},
        )
        assert status == 201
        status, error = request(
            base, "POST", "/api/v1/goals/goal-api/code-snapshots",
            {"language": "python3", "sourceCode": "   \n"},
        )
        assert status == 422
        assert error["code"] == "INVALID_CODE_SNAPSHOT"

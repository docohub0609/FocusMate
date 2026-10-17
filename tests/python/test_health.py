from __future__ import annotations

import json
import threading
import urllib.request

from apps.api.__main__ import create_server
from apps.api.health import build_health_payload as api_health
from apps.api.problem_storage import ProblemRepository
from services.oj_worker.health import build_health_payload as worker_health


def test_health_payloads_are_stable_and_separate() -> None:
    assert api_health() == {"service": "focusmate-api", "status": "ok", "version": "v1"}
    assert worker_health() == {
        "service": "focusmate-oj-worker",
        "status": "ok",
        "version": "v1",
    }


def test_api_health_endpoint_runs_without_business_dependencies(tmp_path) -> None:
    server = create_server(port=0, problem_repository=ProblemRepository(tmp_path/'health.db'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/health", timeout=2
        ) as response:
            assert response.status == 200
            assert json.load(response) == api_health()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

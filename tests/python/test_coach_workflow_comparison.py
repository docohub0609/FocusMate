"""Measurement integrity only; scripted replies are not model-quality evidence."""
import json
from pathlib import Path

import pytest

from tests.python.test_coach_service import setup_service
from tests.python.test_dynamic_coach_loop import diagnosis, plan, review
from tools.experiments.t082_workflow_compare import (
    CASES_PATH, RecordedProvider, read_cases, rollup, turn, validate_destination, visible_candidate,
)


def test_held_out_cases_are_frozen_and_have_distinct_source_edits():
    cases = read_cases(CASES_PATH)
    assert {c["name"] for c in cases} == {
        "closed-interval-contact", "frequency-tie-lexical", "gcd-zero-second-number"}
    assert all(c["fixed"] != c["code"] for c in cases)


def test_measurement_cannot_write_to_user_data_or_overwrite_old_results(tmp_path):
    with pytest.raises(ValueError, match="isolated artifact"):
        validate_destination(Path("data").resolve())
    with pytest.raises(ValueError):
        validate_destination(tmp_path)


def test_placeholder_does_not_count_but_saved_report_counts_before_help():
    view = {"items": [{"kind": "message", "roundId": "r1", "content": "我改好了"}],
            "round": {"roundId": "r1", "batchId": "b1", "checkSummary": {"savedReportCount": 0}}}
    assert visible_candidate(view, "r1") is None
    view["round"]["checkSummary"]["savedReportCount"] = 1
    assert visible_candidate(view, "r1") == {"kind": "formalCheck", "id": "b1"}
    assert visible_candidate(view, "old-round") is None


def test_failed_provider_call_is_preserved_without_secret_config(tmp_path):
    class Broken:
        public_config = {"model": "fixture", "maxTokens": 8192}
        def complete(self, **kwargs):
            raise ValueError("simulated failure")
    provider = RecordedProvider(Broken(), tmp_path / "calls.json")
    with pytest.raises(ValueError):
        provider.complete(messages=[{"role": "user", "content": "question"}], tools=[])
    record = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    assert record["failure"] == "ValueError" and record["seconds"] >= 0
    assert "public_config" not in record


def test_existing_service_worker_path_measures_no_user_retry(tmp_path):
    from services.coach_worker.worker import CoachWorker
    from tests.python.test_coach_worker import Provider
    # setup_service returns the actual Goal, not a replacement experimental manager.
    _, goal, service = setup_service(tmp_path / "measurement")
    service.manager.workflow_version = 1
    session = service.workspace(goal, create=True)["session"]["sessionId"]
    checked = review()
    checked.pop("kind")
    provider = RecordedProvider(Provider([{"value": diagnosis(), "candidateGoals": []}, plan(), checked]),
                                tmp_path / "calls.json")
    result = turn(service, CoachWorker(service, provider), provider, session, tmp_path,
                  "ask", "ask", {"text": "什么是循环？"})
    assert result["status"] == "finished"
    assert result["modelCalls"] == 3
    assert result["firstVisibleCandidateSeconds"] is not None
    assert result["humanReview"] is None and result["userRetryCount"] == 0


def test_rollup_does_not_discard_failure_wait_or_claim_human_success():
    result = {"workflowVersion": 2, "turns": [
        {"label": "ask", "status": "failed", "failure": "MODEL_RESPONSE_INVALID", "wallSeconds": 60,
         "firstVisibleCandidateSeconds": None, "humanReview": None, "modelCalls": 2, "tokens": {"total_tokens": 100}},
        {"label": "check", "status": "finished", "failure": None, "wallSeconds": 8,
         "firstVisibleCandidateSeconds": 8, "humanReview": None, "modelCalls": 1, "tokens": {"total_tokens": 80}},
    ]}
    summary = rollup([result])["versions"]["2"]
    assert summary["allTurnWaitSeconds"] == [60, 8]
    assert summary["failures"][0]["wallSeconds"] == 60
    assert summary["visibleCandidateCount"] == 1 and summary["humanAcceptedCount"] == 0


def test_docker_extension_wires_real_services_with_explicit_fake_runner(tmp_path, monkeypatch):
    """Only extension orchestration is tested here; this is not Docker evidence."""
    from tools.experiments import t082_workflow_docker
    from tools.experiments.t082_backend_probe import seed
    from tests.python.test_oj_judge import run_result
    case = read_cases(CASES_PATH)[0]
    expected = {item["input"].encode(): item["output"].encode()
                for item in case["examples"] + case["privateExamples"]}
    class FakeRunner:
        def run(self, request):
            return run_result(expected.get(request.stdin, b""))
    monkeypatch.setattr(t082_workflow_docker, "DockerPythonRunner", FakeRunner)
    service, goal, session = seed(tmp_path / "isolated", case)
    service.editor.save_draft(goal, {"sourceCode": case["fixed"], "expectedRevision": 1, "operationId": "fix"})
    report = t082_workflow_docker.exercise(service, goal, session, case, tmp_path)
    assert report["status"] == "passed"
    assert report["goalState"]["status"] == "completed"
    assert report["modelCalls"] == 0


def test_reference_disagreement_does_not_seal_material(tmp_path, monkeypatch):
    from tools.experiments import t082_workflow_docker
    from tools.experiments.t082_backend_probe import seed
    from tests.python.test_oj_judge import run_result
    case = read_cases(CASES_PATH)[0]
    class WrongRunner:
        def run(self, request):
            return run_result(b"wrong\n")
    monkeypatch.setattr(t082_workflow_docker, "DockerPythonRunner", WrongRunner)
    service, goal, session = seed(tmp_path / "isolated", case)
    report = t082_workflow_docker.exercise(service, goal, session, case, tmp_path)
    assert report["status"] == "referenceCheckFailed"
    assert "asset" not in report and "completion" not in report


@pytest.mark.parametrize(("status", "cases"), [
    ("failed", [{"outcome": "passed"}]),
    ("finished", []),
    ("finished", [{"outcome": "outputMismatch"}]),
])
def test_docker_extension_does_not_hide_failed_public_run(tmp_path, monkeypatch, status, cases):
    from tools.experiments import t082_workflow_docker
    from tools.experiments.t082_backend_probe import seed
    from tests.python.test_oj_judge import run_result
    case = read_cases(CASES_PATH)[0]
    expected = {item["input"].encode(): item["output"].encode()
                for item in case["examples"] + case["privateExamples"]}
    class FakeRunner:
        def run(self, request):
            return run_result(expected.get(request.stdin, b""))
    def broken_run(worker):
        claim = worker.repo.claim()
        worker.repo.finish(claim, status, {"cases": cases})
    monkeypatch.setattr(t082_workflow_docker, "DockerPythonRunner", FakeRunner)
    monkeypatch.setattr(t082_workflow_docker.RunWorker, "process_once", broken_run)
    service, goal, session = seed(tmp_path / "isolated", case)
    service.editor.save_draft(goal, {"sourceCode": case["fixed"], "expectedRevision": 1, "operationId": "fix"})
    report = t082_workflow_docker.exercise(service, goal, session, case, tmp_path)
    assert report["status"] == "publicRunFailed"
    assert "submission" not in report and "completion" not in report


@pytest.mark.parametrize(("field", "value"), [("codeVersion", 1), ("codeHash", "wrong-hash"),
                                             ("resultId", "another-verifier")])
def test_docker_extension_rejects_unrelated_completion(tmp_path, monkeypatch, field, value):
    from tools.experiments import t082_workflow_docker
    from tools.experiments.t082_backend_probe import seed
    from tests.python.test_oj_judge import run_result
    case = read_cases(CASES_PATH)[0]
    expected = {item["input"].encode(): item["output"].encode()
                for item in case["examples"] + case["privateExamples"]}
    class FakeRunner:
        def run(self, request):
            return run_result(expected.get(request.stdin, b""))
    monkeypatch.setattr(t082_workflow_docker, "DockerPythonRunner", FakeRunner)
    service, goal, session = seed(tmp_path / "isolated", case)
    service.editor.save_draft(goal, {"sourceCode": case["fixed"], "expectedRevision": 1, "operationId": "fix"})
    original = service.completion
    monkeypatch.setattr(service, "completion", lambda g: {**original(g), field: value})
    report = t082_workflow_docker.exercise(service, goal, session, case, tmp_path)
    assert report["goalState"]["status"] == "completed"
    assert report["status"] == "finalVerificationIncomplete"

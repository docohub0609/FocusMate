from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest

from apps.api.deepseek_provider import ProviderFailure, ProviderResponse
from services.verifier_builder.candidates import CandidateFailure, CandidateProvider, CandidateSet, CoverageClaim, ProgramCandidate
from services.verifier_builder.cross_check import CheckedCase
from services.verifier_builder.input_repair import InputRepairProvider
from tests.python.test_verifier_candidates import response_document
from tests.python.test_submission_storage import frozen_problem


@pytest.fixture
def context(tmp_path):
    repository, record = frozen_problem(tmp_path / "input-repair.db")
    return record, repository.get_feature_set(record["lifecycle"]["confirmedFeatureSetId"])


@pytest.fixture
def candidates(context):
    record, features = context
    program = lambda role: ProgramCandidate("# PRIVATE-" + role + "\nprint(1)", "deepseek", "test-model", "response-original", "original-v1")
    return CandidateSet("problem-submit", 1, record["lifecycle"]["contentHash"], features["featureSetId"],
        tuple(item["featureKey"] for item in features["features"] if item["status"] == "confirmed"),
        program("generator"), (CoverageClaim("minimumSize", ("dataScale.n",)),),
        program("oracle"), program("reference"), ())


def review_document(payload):
    return {key: payload[key] for key in ("schemaVersion", "problemRef", "problemContentHash", "featureSetId")} | {
        "cases": [{"caseKey": case["caseKey"], "status": "legal", "reason": "Matches the confirmed input bounds and format.",
            "featureKeys": ["dataScale.n", "input.primary"]} for case in payload["cases"]]}


class Transport:
    def __init__(self, context, change=None):
        self.context, self.change, self.calls = context, change, []

    def complete(self, *, system_prompt, user_prompt, deadline):
        payload = json.loads(user_prompt)
        self.calls.append((system_prompt, payload, deadline))
        document = response_document("inputGenerator", self.context) if payload["role"] == "inputGenerator" else review_document(payload)
        if self.change:
            document = self.change(document)
        return ProviderResponse("response-fixed", "test-model", document if isinstance(document, str) else json.dumps(document))


def test_repair_changes_only_generator_and_keeps_deadline_and_private_roles_separate(context, candidates):
    transport = Transport(context)
    repair = InputRepairProvider(CandidateProvider(transport))
    feedback = {"reason": "insufficientSmallCoverage", "smallCount": 6}
    before = deepcopy((context, candidates, feedback))
    deadline = time.monotonic() + 5
    result = repair.repair(*context, candidates, feedback, deadline)
    assert len(transport.calls) == 1
    system, payload, received = transport.calls[0]
    assert received == deadline
    assert payload["previousGeneratorSource"] == candidates.generator.source
    assert payload["feedback"] == feedback
    assert payload["smallCasePolicy"]["maxEnumerationDimension"] == 8
    assert "PRIVATE-oracle" not in system + json.dumps(payload)
    assert "PRIVATE-reference" not in system + json.dumps(payload)
    assert result.generator.source.startswith("# PRIVATE-inputGenerator")
    assert result.generator.prompt_version == "verifier-input-generator-repair-v1"
    assert result.slow_oracle is candidates.slow_oracle and result.fast_reference is candidates.fast_reference
    assert result.mutants is candidates.mutants
    assert result.problem_content_hash == candidates.problem_content_hash
    assert (context, candidates, feedback) == before


@pytest.mark.parametrize("change", [lambda d: "SECRET malformed", lambda d: d | {"extra": "SECRET"},
    lambda d: d | {"source": ""}, lambda d: d | {"featureSetId": "other"},
    lambda d: d | {"coveragePlan": [{"tag": "minimumSize", "featureKeys": ["invented.fact"]}]}])
def test_repair_does_not_accept_bad_candidates_or_retry(context, candidates, change):
    transport = Transport(context, change)
    with pytest.raises(CandidateFailure) as error:
        InputRepairProvider(CandidateProvider(transport)).repair(*context, candidates,
            {"reason": "smallReferenceTimeout"}, time.monotonic() + 5)
    assert error.value.code == "GENERATOR_REPAIR_INVALID"
    assert "SECRET" not in str(error.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("field,value", [("problem_id", "other"), ("problem_version", 2),
    ("problem_content_hash", "sha256:" + "a" * 64), ("feature_set_id", "other"),
    ("confirmed_feature_keys", ("invented.fact",))])
def test_repair_rejects_candidates_from_other_context_without_calling_model(context, candidates, field, value):
    transport = Transport(context)
    with pytest.raises(CandidateFailure) as error:
        InputRepairProvider(CandidateProvider(transport)).repair(*context, replace(candidates, **{field: value}),
            {"reason": "smallReferenceTimeout"}, time.monotonic() + 5)
    assert error.value.code == "GENERATOR_REPAIR_INVALID"
    assert not transport.calls


def test_review_is_independent_and_covers_each_selected_input(context):
    transport = Transport(context)
    cases = (CheckedCase("first", "small", ("randomSmall",), b"1\n1\n", b"PRIVATE-ANSWER"),
        CheckedCase("second", "large", ("maximumSize",), b"2\n1 2\n", b"PRIVATE-ANSWER-2"))
    deadline = time.monotonic() + 5
    result = InputRepairProvider(CandidateProvider(transport)).review_cases(*context, cases, deadline)
    assert len(transport.calls) == 1
    system, payload, received = transport.calls[0]
    assert received == deadline
    assert payload["cases"] == [{"caseKey": "first", "stdin": "1\n1\n"}, {"caseKey": "second", "stdin": "2\n1 2\n"}]
    assert "PRIVATE-ANSWER" not in system + json.dumps(payload)
    assert "source" not in payload and "previousGeneratorSource" not in payload
    assert [row["caseKey"] for row in result["cases"]] == ["first", "second"]
    assert result["promptVersion"] == "verifier-input-legality-review-v1"
    assert result["caseSetHash"].startswith("sha256:")


@pytest.mark.parametrize("status", ["illegal", "unknown"])
def test_illegal_or_uncertain_inputs_cannot_proceed(context, status):
    def change(document):
        document["cases"][0]["status"] = status
        return document
    transport = Transport(context, change)
    with pytest.raises(CandidateFailure) as error:
        InputRepairProvider(CandidateProvider(transport)).review_cases(*context,
            (CheckedCase("first", "small", ("boundary",), b"1\n1\n"),), time.monotonic() + 5)
    assert error.value.code == "GENERATOR_INPUT_LEGALITY_UNCONFIRMED"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("bad", ["missing", "duplicate", "unknownCase", "unknownFeature", "emptyEvidence", "invalidStatus", "extra", "wrongContext", "json", "duplicateJson"])
def test_review_rejects_invalid_or_incomplete_evidence_without_leaking_it(context, bad, capsys, caplog):
    def change(document):
        row = document["cases"][0]
        if bad == "missing": document["cases"] = []
        elif bad == "duplicate": document["cases"].append(deepcopy(row))
        elif bad == "unknownCase": row["caseKey"] = "unrequested"
        elif bad == "unknownFeature": row["featureKeys"] = ["invented.fact"]
        elif bad == "emptyEvidence": row["featureKeys"] = []
        elif bad == "invalidStatus": row["status"] = "passed"
        elif bad == "extra": row["privateAnswer"] = "SECRET"
        elif bad == "wrongContext": document["problemRef"]["problemVersion"] = 2
        elif bad == "json": return "SECRET malformed"
        elif bad == "duplicateJson": return json.dumps(document)[:-1] + ',"cases":[]}'
        return document
    transport = Transport(context, change)
    with pytest.raises(CandidateFailure) as error:
        InputRepairProvider(CandidateProvider(transport)).review_cases(*context,
            (CheckedCase("first", "small", ("boundary",), b"SECRET\n"),), time.monotonic() + 5)
    assert error.value.code == "GENERATOR_REPAIR_INVALID"
    out = capsys.readouterr()
    assert "SECRET" not in str(error.value) + caplog.text + out.out + out.err
    assert len(transport.calls) == 1


@pytest.mark.parametrize("operation", ["repair", "review"])
def test_expired_deadline_makes_no_request(context, candidates, operation):
    transport = Transport(context)
    provider = InputRepairProvider(CandidateProvider(transport))
    with pytest.raises(CandidateFailure) as error:
        if operation == "repair":
            provider.repair(*context, candidates, {"reason": "smallReferenceTimeout"}, time.monotonic() - 1)
        else:
            provider.review_cases(*context, (CheckedCase("first", "small", (), b"1\n"),), time.monotonic() - 1)
    assert error.value.code == "BUILD_BUDGET_EXCEEDED"
    assert not transport.calls


@pytest.mark.parametrize("operation", ["repair", "review"])
@pytest.mark.parametrize("code,expected", [("LLM_PAYMENT_REQUIRED", "LLM_CONFIGURATION_REQUIRED"),
    ("LLM_UNAVAILABLE", "LLM_TEMPORARILY_UNAVAILABLE"), ("BUILD_BUDGET_EXCEEDED", "BUILD_BUDGET_EXCEEDED")])
def test_network_failures_keep_original_classification(context, candidates, operation, code, expected):
    class FailingTransport:
        def complete(self, **kwargs):
            raise ProviderFailure(code, "SECRET internal provider failure", False)
    provider = InputRepairProvider(CandidateProvider(FailingTransport()))
    with pytest.raises(CandidateFailure) as error:
        if operation == "repair":
            provider.repair(*context, candidates, {"reason": "smallReferenceTimeout"}, time.monotonic() + 5)
        else:
            provider.review_cases(*context, (CheckedCase("first", "small", (), b"1\n"),), time.monotonic() + 5)
    assert error.value.code == expected
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("feedback", [{"reason": "smallReferenceTimeout", "referenceOutput": "SECRET"},
    {"reason": "changeProblem"}, {"reason": "smallReferenceTimeout", "smallCount": -1},
    {"reason": "smallReferenceTimeout", "failureSummary": [{"stdin": "SECRET"}]}])
def test_unsafe_feedback_is_not_forwarded_to_the_model(context, candidates, feedback):
    transport = Transport(context)
    with pytest.raises(CandidateFailure) as error:
        InputRepairProvider(CandidateProvider(transport)).repair(*context, candidates, feedback, time.monotonic() + 5)
    assert error.value.code == "GENERATOR_REPAIR_INVALID"
    assert "SECRET" not in str(error.value)
    assert not transport.calls


def test_safe_timeout_summary_is_forwarded_without_input_or_reference_answers(context, candidates):
    transport = Transport(context)
    summary = CheckedCase("one", "small", ("boundary",), b"PRIVATE-INPUT").summary("smallReferenceTimeout").to_dict()
    InputRepairProvider(CandidateProvider(transport)).repair(*context, candidates,
        {"reason": "smallReferenceTimeout", "failureSummary": [summary]}, time.monotonic() + 5)
    payload = transport.calls[0][1]
    assert payload["feedback"]["failureSummary"] == [summary]
    assert "PRIVATE-INPUT" not in json.dumps(payload)


@pytest.mark.parametrize("operation", ["repair", "review"])
def test_response_arriving_after_original_deadline_is_not_accepted(context, candidates, operation, monkeypatch):
    deadline = time.monotonic() + 5
    class LateTransport(Transport):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            monkeypatch.setattr("services.verifier_builder.input_repair.time.monotonic", lambda: deadline + 1)
            return response
    transport = LateTransport(context)
    provider = InputRepairProvider(CandidateProvider(transport))
    with pytest.raises(CandidateFailure) as error:
        if operation == "repair":
            provider.repair(*context, candidates, {"reason": "smallReferenceTimeout"}, deadline)
        else:
            provider.review_cases(*context, (CheckedCase("first", "small", (), b"1\n"),), deadline)
    assert error.value.code == "BUILD_BUDGET_EXCEEDED"
    assert len(transport.calls) == 1


def test_repair_then_review_share_one_absolute_budget(context, candidates):
    transport = Transport(context)
    provider = InputRepairProvider(CandidateProvider(transport))
    deadline = time.monotonic() + 5
    provider.repair(*context, candidates, {"reason": "missingLargeCoverage"}, deadline)
    provider.review_cases(*context, (CheckedCase("first", "small", (), b"1\n"),), deadline)
    assert [call[2] for call in transport.calls] == [deadline, deadline]
    assert [call[1]["role"] for call in transport.calls] == ["inputGenerator", "inputLegalityReviewer"]

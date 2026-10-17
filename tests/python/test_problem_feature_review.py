"""Review-contract tests; fake verdicts do not prove model semantic accuracy."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from apps.api.deepseek_provider import ProviderResponse
from apps.api.problem_intake import create_problem_package
from apps.api.schema_validation import validate


NOW = "2026-09-12T04:00:00Z"


def problem(**changes):
    intake = {
        "statement": "给定 n 个整数，每个整数不超过 100000。",
        "inputSpec": "第一行给出整数 n。",
        "outputSpec": "输出一个整数。",
        "constraints": "1 <= n <= 2000",
        "examples": [{"input": "2\n1 2\n", "output": "3\n"}],
        "language": "python3", "ioMode": "standardInputOutput",
    }
    intake.update(changes)
    return create_problem_package(
        intake, id_factory=lambda: "problem-review",
        clock=lambda: datetime(2026, 9, 12, 4, tzinfo=timezone.utc),
    )


def features():
    return [{
        "featureKey": "dataScale.n", "category": "dataScale",
        "status": "confirmed", "value": {"variable": "n", "maximum": 100000},
        "confidence": 1, "reason": "First model believes this is certain.",
        "evidence": [{"evidenceId": "statement:0", "sourceField": "statement",
                      "sourcePath": "statement", "sourceText": "给定 n 个整数，每个整数不超过 100000。"}],
    }]


def verdict(item_id="feature-0", *, value="contradicted", issue="objectMismatch", refs=None):
    return {"itemId": item_id, "verdict": value, "issue": issue,
            "evidenceRefs": ["constraints:0"] if refs is None else refs}


class FakeReviewer:
    """Replace only the external semantic call, preserving the real validation."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def review(self, problem, *, features, evidence_catalog):
        self.calls.append(deepcopy({"problem": problem, "features": features,
                                   "evidenceCatalog": evidence_catalog}))
        if isinstance(self.response, Exception):
            raise self.response
        content = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return ProviderResponse("review-response-1", "deepseek-test", content)


def review(source, values, provider):
    from apps.api.problem_feature_review import review_problem_features
    return review_problem_features(source, values, provider, created_at=NOW)


@pytest.mark.parametrize("issue", ["objectMismatch", "numericMismatch"])
def test_review_records_mismatch_without_changing_the_claim(issue):
    values = features()
    before = deepcopy(values)
    result = review(problem(), values, FakeReviewer({"items": [verdict(issue=issue)]}))
    assert values == before
    assert result["status"] == "completed"
    assert result["items"] == [{
        "itemId": "feature-0", "featureKey": "dataScale.n", "category": "dataScale",
        "verdict": "contradicted", "issue": issue, "evidenceRefs": ["constraints:0"],
        "evidence": [{"evidenceId": "constraints:0", "sourceField": "constraints",
                      "sourcePath": "constraints", "sourceText": "1 <= n <= 2000"}],
    }]
    assert result["problemRef"] == {"problemId": "problem-review", "problemVersion": 1}
    assert result["promptVersion"] == "problem-feature-review-io-v5"
    assert result["createdAt"] == NOW
    validate("ProblemFeatureReviewResult", result)


def test_supported_review_preserves_existing_local_review_and_hypothesis():
    values = features()
    values[0].update(status="hypothesis", confidence=0.6, review={
        "status": "required", "issue": "locallyUnverifiable", "message": "请核对。",
        "allowedActions": ["confirmFeature", "editFeature"],
    })
    before = deepcopy(values)
    result = review(problem(), values, FakeReviewer({"items": [
        verdict(value="supported", issue="none", refs=["statement:0"]),
    ]}))
    assert result["items"][0]["verdict"] == "supported"
    assert values == before


def test_supported_unknown_can_confirm_absence_without_inventing_evidence_or_value():
    values = [{"featureKey": "resource.timeLimit", "category": "resourceConstraint",
               "status": "unknown", "reason": "No limit provided.", "evidence": [],
               "review": {"status": "required", "issue": "unknown", "message": "保留未知。",
                          "allowedActions": ["editFeature"]}}]
    before = deepcopy(values)
    result = review(problem(), values, FakeReviewer({"items": [
        verdict(value="supported", issue="none", refs=[]),
    ]}))
    assert result["status"] == "completed"
    assert result["items"][0]["evidence"] == []
    assert values == before
    validate("ProblemFeatureReviewResult", result)


def test_review_payload_omits_first_model_reason_confidence_and_local_review():
    source = problem()
    source["privateMetadata"] = "must not reach reviewer"
    values = features()
    values[0]["review"] = {"message": "The first checker trusts this."}
    provider = FakeReviewer({"items": [verdict()]})
    result = review(source, values, provider)
    sent = provider.calls[0]
    assert set(sent["problem"]) == {"statement", "inputSpec", "outputSpec", "constraints", "examples"}
    assert sent["features"] == [{
        "itemId": "feature-0", "featureKey": "dataScale.n", "category": "dataScale",
        "status": "confirmed", "value": {"variable": "n", "maximum": 100000},
        "evidenceRefs": ["statement:0"],
    }]
    assert result["items"][0]["featureKey"] == "dataScale.n"


def test_unknown_and_conflict_receive_distinct_item_ids_without_losing_candidates():
    values = features()
    original = deepcopy(values[0])
    values.append({"featureKey": "resource.limit", "category": "resourceConstraint",
                   "status": "unknown", "reason": "Missing.", "evidence": []})
    values.append({"featureKey": "dataScale.n", "category": "dataScale", "status": "conflict",
                   "reason": "Two candidates.", "evidence": [], "candidates": [
                       {"value": 100000, "evidence": original["evidence"]},
                       {"value": 2000, "evidence": [{"evidenceId": "constraints:0", "sourceField": "constraints",
                           "sourcePath": "constraints", "sourceText": "1 <= n <= 2000"}]},
                   ]})
    before = deepcopy(values)
    provider = FakeReviewer({"items": [
        verdict("feature-2", value="insufficient", issue="insufficientEvidence"),
        verdict(), verdict("feature-1", value="insufficient", issue="insufficientEvidence", refs=[]),
    ]})
    result = review(problem(), values, provider)
    assert [item["itemId"] for item in result["items"]] == ["feature-0", "feature-1", "feature-2"]
    assert provider.calls[0]["features"][1]["status"] == "unknown"
    assert provider.calls[0]["features"][2]["candidates"] == [
        {"value": 100000, "evidenceRefs": ["statement:0"]},
        {"value": 2000, "evidenceRefs": ["constraints:0"]},
    ]
    assert values == before


@pytest.mark.parametrize("text", ["n 的上限为十万。", "n does not exceed one hundred thousand.", "n ≤ 10⁵"])
def test_fake_supported_verdict_preserves_varied_source_expressions_exactly(text):
    source = problem(constraints=text)
    provider = FakeReviewer({"items": [verdict(value="supported", issue="none")]})
    result = review(source, features(), provider)
    assert result["items"][0]["evidence"][0]["sourceText"] == text


@pytest.mark.parametrize("payload", [
    {"items": []},
    {"items": [verdict(), verdict()]},
    {"items": [verdict("feature-999")]},
    {"items": [verdict(refs=["constraints:999"])]},
    {"items": [verdict(refs=["constraints:0", "constraints:0"])]},
    {"items": [dict(verdict(), reason="Use dynamic programming.")]},
    {"items": [dict(verdict(), sourceText="Invented source.")]},
    {"items": [verdict(value="supported", issue="objectMismatch")]},
    {"items": [verdict(value="contradicted", issue="none")]},
    {"items": [verdict(value="supported", issue="none", refs=[])]},
    {"items": [verdict()], "explanation": "extra text"},
    {"items": [dict(verdict(), evidenceRefs="constraints:0")]},
    {"items": [dict(verdict(), itemId=0)]},
])
def test_invalid_review_contract_yields_safe_unavailable_without_retry(payload):
    provider = FakeReviewer(payload)
    result = review(problem(), features(), provider)
    assert result["status"] == "unavailable"
    assert result["failureCode"] == "REVIEW_INVALID"
    assert result["items"] == []
    assert len(provider.calls) == 1
    assert "model" not in result
    validate("ProblemFeatureReviewResult", result)


@pytest.mark.parametrize("raw", [
    "", "```json\n{}\n```", '{"items": NaN}', '{"items": Infinity}',
    '{"items": [], "items": []}', '{"items": [], "\\u0069tems": []}',
    json.dumps({"items": [verdict()]} ) + " trailing text",
    " " * (128 * 1024 + 1), "{\"items\":" + "[" * 2000 + "]" * 2000 + "}",
], ids=["empty", "fenced", "nan", "infinity", "duplicate-key", "escaped-duplicate-key",
        "trailing", "oversized", "deep-nesting"])
def test_malformed_ambiguous_or_oversized_json_is_rejected(raw):
    result = review(problem(), features(), FakeReviewer(raw))
    assert result["status"] == "unavailable"
    assert result["failureCode"] == "REVIEW_INVALID"
    assert result["items"] == []


def test_json_escapes_are_decoded_but_never_replace_authoritative_source_text():
    payload = json.dumps({"items": [verdict()]})
    payload = payload.replace("feature-0", "feature-\\u0030").replace("constraints:0", "constraints:\\u0030")
    result = review(problem(), features(), FakeReviewer(payload))
    assert result["status"] == "completed"
    assert result["items"][0]["evidence"][0]["sourceText"] == "1 <= n <= 2000"


@pytest.mark.parametrize("edit", [
    {"evidenceId": "statement:999"},
    {"sourceText": "The model invented this."},
    {"sourceField": "outputSpec"},
    {"sourcePath": "examples.999.input"},
])
def test_unknown_or_forged_input_evidence_is_rejected_before_review_call(edit):
    values = features()
    values[0]["evidence"][0].update(edit)
    before = deepcopy(values)
    provider = FakeReviewer({"items": [verdict()]})
    result = review(problem(), values, provider)
    assert result["status"] == "unavailable"
    assert result["failureCode"] == "REVIEW_INVALID"
    assert result["items"] == []
    assert provider.calls == []
    assert values == before


def test_unavailable_provider_does_not_leak_failure_message():
    provider = FakeReviewer(RuntimeError("secret-key and raw problem content"))
    result = review(problem(), features(), provider)
    assert result["failureCode"] == "REVIEW_UNAVAILABLE"
    assert result["items"] == []
    assert "secret-key" not in json.dumps(result)
    assert len(provider.calls) == 1
    validate("ProblemFeatureReviewResult", result)


def test_provider_without_review_returns_explicit_unavailable():
    result = review(problem(), features(), object())
    assert result["status"] == "unavailable"
    assert result["failureCode"] == "REVIEW_UNAVAILABLE"
    assert result["items"] == []


def test_provider_mutation_cannot_change_source_features_or_hydrated_evidence():
    class MutatingReviewer:
        def review(self, problem, *, features, evidence_catalog):
            problem["constraints"] = "changed"
            features[0]["value"]["maximum"] = 1
            evidence_catalog[0]["sourceText"] = "changed"
            return ProviderResponse("review-mutator", "deepseek-test", json.dumps({"items": [
                verdict(refs=["statement:0"]),
            ]}))

    source, values = problem(), features()
    before = deepcopy((source, values))
    result = review(source, values, MutatingReviewer())
    assert (source, values) == before
    assert result["items"][0]["evidence"][0]["sourceText"] == "给定 n 个整数，每个整数不超过 100000。"


def test_hash_tracks_reviewed_facts_and_source_but_ignores_first_pass_opinions():
    source, values = problem(), features()
    first = review(source, values, FakeReviewer({"items": [verdict()]}))
    values[0].update(reason="Different first opinion.", confidence=0.1, review={"message": "changed"})
    second = review(source, values, FakeReviewer({"items": [verdict()]}))
    assert first["inputHash"] == second["inputHash"]
    assert first["problemContentHash"] == second["problemContentHash"]
    values[0]["value"]["maximum"] = 2000
    third = review(source, values, FakeReviewer({"items": [verdict()]}))
    assert third["inputHash"] != first["inputHash"]
    source["constraints"] = "1 <= n <= 3000"
    fourth = review(source, values, FakeReviewer({"items": [verdict()]}))
    assert fourth["inputHash"] != third["inputHash"]
    assert fourth["problemContentHash"] != third["problemContentHash"]

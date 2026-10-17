from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from apps.api.deepseek_provider import ProviderFailure, ProviderResponse
from apps.api.problem_features import FeatureExtractionFailure, extract_problem_features
from apps.api.problem_evidence import build_evidence_catalog
from apps.api.problem_intake import create_problem_package
from apps.api.schema_validation import validate


INTAKE = {
    "statement": "Given an integer array, find an interval satisfying the condition.",
    "inputSpec": "The first line is n. The second line contains n integers.",
    "outputSpec": "Output two integers: the left and right boundaries.",
    "constraints": "1 <= n <= 100000",
    "examples": [{"input": "3\n1 2 3\n", "output": "1 2\n"}],
    "language": "python3",
    "ioMode": "standardInputOutput",
}


def problem_request(**changes: object) -> dict[str, object]:
    intake = deepcopy(INTAKE)
    intake.update(changes)
    problem = create_problem_package(
        intake,
        clock=lambda: datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc),
        id_factory=lambda: "problem-fixed",
    )
    return {"problemPackage": problem}


def base_features(domain_tag: str = "array") -> list[dict[str, object]]:
    return [
        {
            "featureKey": "dataScale.n",
            "category": "dataScale",
            "status": "confirmed",
            "value": {"variable": "n", "minimum": 1, "maximum": 100000},
            "confidence": 1,
            "reason": "The numeric bounds are explicit.",
            "evidenceRefs": ["constraints:0"],
        },
        {
            "featureKey": "input.primary",
            "category": "inputStructure",
            "status": "confirmed",
            "value": {"kind": "integerSequence", "lengthVariable": "n"},
            "confidence": 1,
            "reason": "The input specification names the sequence.",
            "evidenceRefs": ["inputSpec:0"],
        },
        {
            "featureKey": "output.primary",
            "category": "outputStructure",
            "status": "confirmed",
            "value": {
                "valueCount": 2,
                "valueType": "integer",
                "meanings": ["leftBoundary", "rightBoundary"],
            },
            "confidence": 1,
            "reason": "The output count, type, and meanings are explicit.",
            "evidenceRefs": ["outputSpec:0"],
        },
        {
            "featureKey": "resource.primary",
            "category": "resourceConstraint",
            "status": "unknown",
            "reason": "No time or memory limit is supplied.",
            "evidenceRefs": [],
        },
        {
            "featureKey": f"domain.{domain_tag}",
            "category": "domainTag",
            "status": "confirmed",
            "value": domain_tag,
            "confidence": 1,
            "reason": "The statement names the domain structure.",
            "evidenceRefs": ["statement:0"],
        },
    ]


class FakeProvider:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[str | None] = []

    def review(self, problem, *, features, evidence_catalog):
        # Contract fake: semantic accuracy is measured separately with real labelled cases.
        items = []
        for feature in features:
            refs = list(feature.get("evidenceRefs", []))
            for candidate in feature.get("candidates", []):
                refs.extend(candidate.get("evidenceRefs", []))
            items.append({"itemId": feature["itemId"], "verdict": "supported",
                          "issue": "none", "evidenceRefs": list(dict.fromkeys(refs))})
        return ProviderResponse("fake-review", "fake-review", json.dumps({"items": items}))

    def extract(
        self,
        problem: dict[str, object],
        *,
        evidence_catalog: list[dict[str, str]],
        repair_reason: str | None = None,
    ) -> ProviderResponse:
        self.calls.append(repair_reason)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        content = response if isinstance(response, str) else json.dumps(response)
        return ProviderResponse("deepseek-response-test", "deepseek-v4-flash", content)


def test_semantic_output_structure_and_unknown_are_preserved() -> None:
    provider = FakeProvider([{"features": base_features()}])
    result = extract_problem_features(
        problem_request(),
        provider,
        clock=lambda: datetime(2026, 8, 15, 1, 1, tzinfo=timezone.utc),
        id_factory=lambda: "features-fixed",
    )

    output = next(item for item in result["features"] if item["category"] == "outputStructure")
    assert output["value"] == {
        "valueCount": 2,
        "valueType": "integer",
        "meanings": ["leftBoundary", "rightBoundary"],
    }
    assert result["status"] == "reviewRequired"
    assert result["problemRef"] == {"problemId": "problem-fixed", "problemVersion": 1}
    assert result["provenance"] == {"type": "llmGenerated"}
    assert output["evidence"][0]["evidenceId"] == "outputSpec:0"
    assert output["evidence"][0]["sourceText"] == INTAKE["outputSpec"]
    validate("ProblemFeatures", result)


def test_evidence_catalog_preserves_exact_multilingual_source_lines() -> None:
    problem = problem_request(
        statement="第一段。\n\nSecond line.",
        examples=[{"input": "1 2\n", "output": "3\n"}],
    )["problemPackage"]
    catalog = build_evidence_catalog(problem)  # type: ignore[arg-type]
    assert [(item["evidenceId"], item["sourceText"]) for item in catalog if item["sourceField"] == "statement"] == [
        ("statement:0", "第一段。"), ("statement:1", "Second line."),
    ]
    assert next(item for item in catalog if item["evidenceId"] == "examples:0:input")["sourceText"] == "1 2\n"


def test_model_selects_ids_and_python_hydrates_exact_source_text() -> None:
    result = extract_problem_features(
        problem_request(), FakeProvider([{"features": base_features()}]),
    )
    output = next(item for item in result["features"] if item["category"] == "outputStructure")
    assert output["evidence"] == [{
        "evidenceId": "outputSpec:0",
        "sourceField": "outputSpec", "sourcePath": "outputSpec",
        "sourceText": INTAKE["outputSpec"],
    }]


@pytest.mark.parametrize("output_spec", ["输出两个整数。", "输出一对整数。"])
def test_common_chinese_quantity_words_support_confirmed_count(output_spec: str) -> None:
    features = base_features()
    features[2]["value"] = {"valueCount": 2, "valueType": "integer"}
    result = extract_problem_features(
        problem_request(outputSpec=output_spec), FakeProvider([{"features": features}]),
    )
    output = next(item for item in result["features"] if item["category"] == "outputStructure")
    assert output["status"] == "confirmed"


@pytest.mark.parametrize(
    ("statement", "tag"),
    [
        ("Given a string, report the required result.", "string"),
        ("Given a graph, report the required result.", "graph"),
    ],
)
def test_unfamiliar_surface_domains_are_not_algorithm_labels(statement: str, tag: str) -> None:
    features = base_features(tag)
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(problem_request(statement=statement), provider)
    domain = next(item for item in result["features"] if item["category"] == "domainTag")
    assert domain["value"] == tag


def test_hypothesis_requires_explicit_confidence_and_evidence() -> None:
    features = base_features()
    features[2] = {
        "featureKey": "output.primary",
        "category": "outputStructure",
        "status": "hypothesis",
        "value": {"valueCount": 2},
        "confidence": 0.75,
        "reason": "Left and right boundaries imply two values, but the exact type is not explicit.",
        "evidenceRefs": ["outputSpec:0"],
    }
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(problem_request(), provider)
    output = next(item for item in result["features"] if item["category"] == "outputStructure")
    assert output["status"] == "hypothesis"
    assert output["confidence"] == 0.75


@pytest.mark.parametrize("constraint_text", ["1 <= n <= 1e5", "1 <= n <= 10万"])
def test_confirmed_scale_supports_common_numeric_notation(constraint_text: str) -> None:
    features = base_features()
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(problem_request(constraints=constraint_text), provider)
    assert result["features"][0]["value"]["maximum"] == 100000


def test_confirmed_scale_supports_negative_bounds() -> None:
    features = base_features()
    features[0]["featureKey"] = "dataScale.aiRange"
    features[0]["value"] = {"variable": "ai", "minimum": -10, "maximum": 10}
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(
        problem_request(constraints="1 <= n <= 100000, -10 <= ai <= 10"),
        provider,
    )
    assert result["features"][0]["value"]["minimum"] == -10


def test_unknown_null_placeholders_are_normalized() -> None:
    features = base_features()
    features[3]["value"] = None
    features[3]["confidence"] = None
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(problem_request(), provider)
    resource = next(
        item for item in result["features"] if item["category"] == "resourceConstraint"
    )
    assert "value" not in resource
    assert "confidence" not in resource


def test_confirmed_resource_units_are_rechecked_locally() -> None:
    features = base_features()
    features[3] = {
        "featureKey": "resource.limits",
        "category": "resourceConstraint",
        "status": "confirmed",
        "value": {"timeLimitMs": 1000, "memoryLimitMb": 256},
        "confidence": 1,
        "reason": "Both limits are explicit.",
        "evidenceRefs": ["constraints:0"],
    }
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(
        problem_request(constraints="1 <= n <= 100000。时间限制 1 秒，内存限制 256 MB。"),
        provider,
    )
    assert result["status"] == "complete"


def test_duplicate_disagreement_becomes_conflict_instead_of_overwrite() -> None:
    request = problem_request(constraints="1 <= n <= 100000. A legacy note says n <= 1000.")
    features = base_features()
    second = deepcopy(features[0])
    second["value"] = {"variable": "n", "maximum": 1000}
    second["evidenceRefs"] = ["constraints:0"]
    features.insert(1, second)
    provider = FakeProvider([{"features": features}])

    result = extract_problem_features(request, provider)
    scale = next(item for item in result["features"] if item["featureKey"] == "dataScale.n")
    assert scale["status"] == "conflict"
    assert len(scale["candidates"]) == 2
    assert result["status"] == "reviewRequired"
    assert scale["review"]["issue"] == "conflict"


def test_empty_or_invalid_output_gets_one_repair_attempt() -> None:
    provider = FakeProvider(["", {"features": base_features()}])
    result = extract_problem_features(problem_request(), provider)
    assert result["featureSetId"].startswith("features-")
    assert len(provider.calls) == 2
    assert provider.calls[1]


def test_repair_prompt_identifies_the_rejected_evidence_field() -> None:
    bad = deepcopy(base_features())
    bad[0]["evidenceRefs"] = ["constraints:999"]
    provider = FakeProvider([{"features": bad}, {"features": base_features()}])
    extract_problem_features(problem_request(), provider)
    assert "features.0.evidenceRefs.0" in provider.calls[1]  # type: ignore[operator]
    assert "evidenceRefUnknown" in provider.calls[1]  # type: ignore[operator]


def test_legacy_model_authored_source_text_is_rejected_by_candidate_schema() -> None:
    features = base_features()
    features[0].pop("evidenceRefs")
    features[0]["evidence"] = [{
        "sourceField": "constraints", "sourcePath": "constraints",
        "sourceText": "1 <= n <= 100000",
    }]
    provider = FakeProvider([{"features": features}, {"features": features}])
    with pytest.raises(FeatureExtractionFailure) as caught:
        extract_problem_features(problem_request(), provider)
    assert caught.value.code == "LLM_OUTPUT_INVALID"


def test_evidence_reference_from_incompatible_field_requires_review_without_retry() -> None:
    features = base_features()
    features[2]["evidenceRefs"] = ["inputSpec:0"]
    provider = FakeProvider([{"features": features}])
    result = extract_problem_features(problem_request(), provider)
    output = next(item for item in result["features"] if item["category"] == "outputStructure")
    assert result["status"] == "reviewRequired"
    assert output["review"]["issue"] == "evidenceContextMismatch"
    assert output["evidence"][0]["evidenceId"] == "inputSpec:0"
    assert len(provider.calls) == 1


def test_locally_unverifiable_number_requires_review_without_retry_or_data_loss() -> None:
    features = base_features()
    provider = FakeProvider([{"features": features}])

    result = extract_problem_features(
        problem_request(constraints="The maximum n is one hundred thousand."),
        provider,
    )

    scale = next(item for item in result["features"] if item["category"] == "dataScale")
    output = next(item for item in result["features"] if item["category"] == "outputStructure")
    assert result["status"] == "reviewRequired"
    assert scale["review"]["issue"] == "locallyUnverifiable"
    assert output["value"]["valueCount"] == 2
    assert len(result["features"]) == 5
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("bad_features", "expected_code"),
    [
        ([{"unexpected": True}], "LLM_OUTPUT_INVALID"),
        (base_features(), "LLM_EVIDENCE_INVALID"),
    ],
)
def test_invalid_model_output_is_rejected_after_one_repair(
    bad_features: list[dict[str, object]], expected_code: str
) -> None:
    if expected_code == "LLM_EVIDENCE_INVALID":
        bad_features = deepcopy(bad_features)
        bad_features[0]["evidenceRefs"] = ["constraints:999"]
    provider = FakeProvider([{"features": bad_features}, {"features": bad_features}])
    with pytest.raises(FeatureExtractionFailure) as caught:
        extract_problem_features(problem_request(), provider)
    assert caught.value.code == expected_code
    assert len(provider.calls) == 2


def test_strategy_tag_is_rejected() -> None:
    features = base_features()
    features[-1]["value"] = "dynamicProgramming"
    provider = FakeProvider([{"features": features}, {"features": features}])
    with pytest.raises(FeatureExtractionFailure) as caught:
        extract_problem_features(problem_request(), provider)
    assert caught.value.code == "LLM_OUTPUT_INVALID"


def test_strategy_hidden_in_a_non_tag_value_is_rejected() -> None:
    features = base_features()
    features[1]["value"] = {"kind": "integerSequence", "recommendedAlgorithm": "dynamicProgramming"}
    provider = FakeProvider([{"features": features}, {"features": features}])
    with pytest.raises(FeatureExtractionFailure) as caught:
        extract_problem_features(problem_request(), provider)
    assert caught.value.code == "LLM_OUTPUT_INVALID"


def test_provider_failure_keeps_safe_error_semantics() -> None:
    provider = FakeProvider([
        ProviderFailure("LLM_UNAVAILABLE", "The service is temporarily unavailable.", True)
    ])
    with pytest.raises(FeatureExtractionFailure) as caught:
        extract_problem_features(problem_request(), provider)
    assert caught.value.code == "LLM_UNAVAILABLE"
    assert caught.value.retryable is True


def test_invalid_request_is_rejected_before_provider_call() -> None:
    provider = FakeProvider([{"features": base_features()}])
    with pytest.raises(FeatureExtractionFailure) as caught:
        extract_problem_features({"problemPackage": {"problemId": "broken"}}, provider)
    assert caught.value.code == "INVALID_EXTRACTION_REQUEST"
    assert provider.calls == []

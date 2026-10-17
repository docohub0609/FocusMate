"""Validate and normalize LLM-derived features for an unfamiliar problem."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
from typing import Any, Callable, Protocol
from uuid import uuid4

from .deepseek_provider import ProviderFailure, ProviderResponse
from .problem_identity import problem_content_hash
from .problem_intake import timestamp, utc_now
from .problem_evidence import build_evidence_catalog
from .problem_feature_review import review_problem_features
from .schema_validation import validate, validation_errors


PROMPT_VERSION = "problem-features-io-proposal-v7"
from .feature_vocabulary import ALLOWED_DOMAIN_TAGS

FORBIDDEN_STRATEGY_TERMS = {
    "slidingwindow", "twopointers", "binarysearch", "dynamicprogramming",
    "greedy", "backtracking", "breadthfirstsearch", "depthfirstsearch",
}


class FeatureProvider(Protocol):
    def extract(
        self,
        problem: dict[str, Any],
        *,
        evidence_catalog: list[dict[str, str]],
        repair_reason: str | None = None,
    ) -> ProviderResponse: ...

    def review(
        self, problem: dict[str, Any], *, features: list[dict[str, Any]],
        evidence_catalog: list[dict[str, str]],
    ) -> ProviderResponse: ...


@dataclass(frozen=True)
class FeatureExtractionFailure(Exception):
    code: str
    message: str
    retryable: bool
    field_errors: list[dict[str, str]]


def new_feature_set_id() -> str:
    return f"features-{uuid4()}"


def _problem_ref(problem: dict[str, Any]) -> dict[str, Any]:
    return {"problemId": problem["problemId"], "problemVersion": problem["version"]}


def _safe_request_errors(instance: object) -> list[dict[str, str]]:
    errors = validation_errors("ProblemFeatureExtractionRequest", instance)
    return [
        {
            "field": ".".join(str(part) for part in error.absolute_path) or "body",
            "code": "invalidStructure",
            "message": "The feature extraction request is invalid.",
        }
        for error in errors[:10]
    ]


def _source_value(problem: dict[str, Any], source_field: str, source_path: str) -> str | None:
    if source_field in {"statement", "inputSpec", "outputSpec", "constraints"}:
        return problem.get(source_field) if source_path == source_field else None
    match = re.fullmatch(r"examples\.(\d+)\.(input|output)", source_path)
    if source_field != "examples" or not match:
        return None
    index = int(match.group(1))
    examples = problem.get("examples", [])
    if index >= len(examples):
        return None
    value = examples[index].get(match.group(2))
    return value if isinstance(value, str) else None


def _all_evidence(feature: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = list(feature.get("evidence", []))
    for candidate in feature.get("candidates", []):
        evidence.extend(candidate.get("evidence", []))
    return evidence


def _verify_evidence(problem: dict[str, Any], features: list[dict[str, Any]]) -> None:
    mismatches: list[dict[str, str]] = []
    for index, feature in enumerate(features):
        for evidence_index, evidence in enumerate(_all_evidence(feature)):
            source = _source_value(problem, evidence["sourceField"], evidence["sourcePath"])
            if source is None or evidence["sourceText"] not in source:
                mismatches.append({
                    "field": f"features.{index}.evidence.{evidence_index}.sourceText",
                    "code": "evidenceMismatch",
                    "message": "The quoted evidence does not occur in the declared problem field.",
                })
    if mismatches:
        raise FeatureExtractionFailure(
            "LLM_EVIDENCE_INVALID",
            "The model returned evidence that cannot be traced to the problem source.",
            True,
            mismatches[:10],
        )


def _hydrate_evidence(
    candidates: dict[str, Any], catalog: list[dict[str, str]], *,
    review_incompatible: bool = False,
) -> list[dict[str, Any]]:
    by_id = {item["evidenceId"]: item for item in catalog}
    allowed_fields = {
        "dataScale": {"statement", "inputSpec", "constraints", "examples"},
        "inputStructure": {"statement", "inputSpec", "constraints", "examples"},
        "outputStructure": {"statement", "outputSpec", "constraints", "examples"},
        "resourceConstraint": {"statement", "constraints"},
        "domainTag": {"statement", "inputSpec", "outputSpec", "constraints"},
    }

    def resolve(
        refs: list[str], category: str, field_path: str,
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        resolved: list[dict[str, str]] = []
        review_issues: list[dict[str, Any]] = []
        for index, evidence_id in enumerate(refs):
            entry = by_id.get(evidence_id)
            if entry is None:
                raise FeatureExtractionFailure(
                    "LLM_EVIDENCE_INVALID",
                    "The model selected an evidence reference that does not exist.", True,
                    [{
                        "field": f"{field_path}.{index}", "code": "evidenceRefUnknown",
                        "message": "The evidence reference is not in the supplied catalog.",
                    }],
                )
            source_field = entry["sourceField"]
            source_path = entry["sourcePath"]
            wrong_example_side = (
                source_field == "examples"
                and (
                    (category in {"dataScale", "inputStructure"} and source_path.endswith(".output"))
                    or (category == "outputStructure" and source_path.endswith(".input"))
                )
            )
            if source_field not in allowed_fields[category] or wrong_example_side:
                if not review_incompatible:
                    raise FeatureExtractionFailure(
                        "LLM_EVIDENCE_INVALID",
                        "The selected evidence comes from an incompatible problem field.", False,
                        [{
                            "field": f"{field_path}.{index}", "code": "evidenceContextMismatch",
                            "message": "The evidence source cannot support this feature category.",
                        }],
                    )
                review_issues.append({
                    "issue": "evidenceContextMismatch",
                    "message": "所选原文栏目可能无法支持这项结论，请人工确认或重新选择证据。",
                    "allowedActions": ["editFeature", "confirmFeature", "editProblem"],
                })
            resolved.append({
                "evidenceId": evidence_id,
                "sourceField": source_field,
                "sourcePath": source_path,
                "sourceText": entry["sourceText"],
            })
        return resolved, review_issues

    hydrated = deepcopy(candidates["features"])
    for feature_index, feature in enumerate(hydrated):
        refs = feature.pop("evidenceRefs")
        feature["evidence"], issues = resolve(
            refs, feature["category"], f"features.{feature_index}.evidenceRefs",
        )
        if issues:
            feature["review"] = {"status": "required", **issues[0]}
        for candidate_index, candidate in enumerate(feature.get("candidates", [])):
            candidate_refs = candidate.pop("evidenceRefs")
            candidate["evidence"], candidate_issues = resolve(
                candidate_refs, feature["category"],
                f"features.{feature_index}.candidates.{candidate_index}.evidenceRefs",
            )
            if candidate_issues and "review" not in feature:
                feature["review"] = {"status": "required", **candidate_issues[0]}
    return hydrated


def _numbers(value: object, path: str = "") -> list[tuple[str, float]]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [(path, float(value))]
    if isinstance(value, dict):
        found: list[tuple[str, float]] = []
        for key, child in value.items():
            found.extend(_numbers(child, f"{path}.{key}" if path else str(key)))
        return found
    if isinstance(value, list):
        found = []
        for index, child in enumerate(value):
            found.extend(_numbers(child, f"{path}.{index}" if path else str(index)))
        return found
    return []


def _numeric_candidates(text: str) -> set[float]:
    normalized = text.replace(",", "")
    values = {
        float(item)
        for item in re.findall(
            r"(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?",
            normalized,
        )
    }
    for exponent in re.findall(r"10\s*(?:\^|\*\*)\s*(\d+)", normalized):
        values.add(float(10 ** int(exponent)))
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(万|亿)", normalized):
        values.add(float(number) * (10000 if unit == "万" else 100000000))
    word_numbers = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "pair": 2,
    }
    for word, number in word_numbers.items():
        if re.search(rf"\b{word}\b", normalized, re.IGNORECASE):
            values.add(float(number))
    chinese_numbers = {
        "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "一对": 2, "两个": 2, "两 个": 2,
    }
    for expression, number in chinese_numbers.items():
        if expression in normalized:
            values.add(float(number))
    return values


def _resource_candidates(path: str, text: str) -> set[float]:
    values = _numeric_candidates(text)
    lower_path = path.lower()
    if "timelimitms" in lower_path:
        for number in re.findall(r"(\d+(?:\.\d+)?)\s*(?:s|sec|second|seconds|秒)\b", text, re.IGNORECASE):
            values.add(float(number) * 1000)
    if "memorylimitmb" in lower_path:
        for number in re.findall(r"(\d+(?:\.\d+)?)\s*(?:mb|mib|兆)", text, re.IGNORECASE):
            values.add(float(number))
    return values


def _review_confirmed_numbers(features: list[dict[str, Any]]) -> None:
    for feature in features:
        if feature["category"] not in {"dataScale", "resourceConstraint"}:
            continue
        supported_values = []
        if feature["status"] == "confirmed":
            supported_values.append((feature.get("value"), feature["evidence"]))
        elif feature["status"] == "conflict":
            supported_values.extend(
                (candidate["value"], candidate["evidence"])
                for candidate in feature["candidates"]
            )
        for value, evidence in supported_values:
            evidence_text = " ".join(item["sourceText"] for item in evidence)
            for path, number in _numbers(value):
                candidates = _resource_candidates(path, evidence_text)
                if not any(math.isclose(number, candidate) for candidate in candidates):
                    feature["review"] = {
                        "status": "required",
                        "issue": "locallyUnverifiable",
                        "message": "系统无法从所选原文中独立确认这个数值，请人工核对。",
                        "allowedActions": ["editFeature", "confirmFeature", "editProblem"],
                    }
                    break


def _add_status_reviews(features: list[dict[str, Any]]) -> None:
    messages = {
        "hypothesis": ("hypothesis", "这项内容是模型推测，请人工确认。"),
        "unknown": ("unknown", "题目材料不足，用户可以补充或保留未知。"),
        "conflict": ("conflict", "不同原文给出了冲突信息，冻结前必须人工解决。"),
    }
    for feature in features:
        if feature["status"] in messages and "review" not in feature:
            issue, message = messages[feature["status"]]
            feature["review"] = {
                "status": "required", "issue": issue, "message": message,
                "allowedActions": ["editFeature", "confirmFeature", "editProblem"],
            }


def _verify_domain_tags(features: list[dict[str, Any]]) -> None:
    for feature in features:
        if feature["category"] != "domainTag":
            continue
        values = []
        if feature["status"] in {"confirmed", "hypothesis"}:
            values.append(feature.get("value"))
        elif feature["status"] == "conflict":
            values.extend(candidate["value"] for candidate in feature["candidates"])
        tags = [
            tag
            for value in values
            for tag in (value if isinstance(value, list) else [value])
        ]
        for tag in tags:
            normalized = re.sub(r"[^a-z]", "", str(tag).lower())
            if (
                not isinstance(tag, str)
                or tag not in ALLOWED_DOMAIN_TAGS
                or normalized in FORBIDDEN_STRATEGY_TERMS
            ):
                raise FeatureExtractionFailure(
                    "LLM_OUTPUT_INVALID",
                    "The model returned a strategy label outside the allowed feature vocabulary.",
                    True,
                    [{
                        "field": feature["featureKey"],
                        "code": "invalidValue",
                        "message": "Only surface domain tags are allowed.",
                    }],
                )


def _verify_no_strategy_features(features: list[dict[str, Any]]) -> None:
    for feature in features:
        searchable = feature["featureKey"]
        if "value" in feature:
            searchable += json.dumps(feature["value"], ensure_ascii=False, sort_keys=True)
        for candidate in feature.get("candidates", []):
            searchable += json.dumps(candidate["value"], ensure_ascii=False, sort_keys=True)
        normalized = re.sub(r"[^a-z]", "", searchable.lower())
        if any(term in normalized for term in FORBIDDEN_STRATEGY_TERMS):
            raise FeatureExtractionFailure(
                "LLM_OUTPUT_INVALID",
                "The model returned a solution strategy as a problem feature.",
                True,
                [{
                    "field": feature["featureKey"],
                    "code": "invalidValue",
                    "message": "Solution strategies are not problem facts.",
                }],
            )


def validate_feature_request(request: object) -> dict[str, Any]:
    errors = _safe_request_errors(request)
    if errors:
        raise FeatureExtractionFailure(
            "INVALID_EXTRACTION_REQUEST",
            "The feature extraction request is invalid.",
            False,
            errors,
        )
    if not isinstance(request, dict) or "problemPackage" not in request:
        raise FeatureExtractionFailure(
            "INVALID_EXTRACTION_REQUEST",
            "A stored problem reference must be resolved before extraction.",
            False,
            [{"field": "problemRef", "code": "unresolvedReference", "message": "The problem reference was not resolved."}],
        )
    return request["problemPackage"]


def _deduplicate(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for feature in features:
        key = (feature["category"], feature["featureKey"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(deepcopy(feature))

    merged: list[dict[str, Any]] = []
    for key in order:
        group = grouped[key]
        substantive = [
            item
            for item in group
            if item["status"] in {"confirmed", "hypothesis"}
        ]
        distinct: dict[str, dict[str, Any]] = {}
        for item in substantive:
            distinct.setdefault(json.dumps(item["value"], sort_keys=True, ensure_ascii=False), item)
        if len(distinct) > 1:
            merged.append({
                "featureKey": key[1],
                "category": key[0],
                "status": "conflict",
                "reason": "Different source-backed values were extracted for the same feature.",
                "evidence": [],
                "candidates": [
                    {"value": item["value"], "evidence": item["evidence"]}
                    for item in distinct.values()
                ],
            })
            continue
        if substantive:
            selected = max(
                substantive,
                key=lambda item: (
                    item["status"] == "confirmed",
                    item.get("confidence", 0),
                ),
            )
            all_evidence = []
            seen = set()
            for item in substantive:
                if item.get("value") != selected.get("value"):
                    continue
                for evidence in item["evidence"]:
                    marker = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
                    if marker not in seen:
                        seen.add(marker)
                        all_evidence.append(evidence)
            selected["evidence"] = all_evidence
            merged.append(selected)
        else:
            merged.append(group[0])
    return merged


def _parse_candidates(content: str) -> dict[str, Any]:
    if not content.strip():
        raise FeatureExtractionFailure(
            "LLM_OUTPUT_EMPTY",
            "DeepSeek returned an empty extraction.",
            True,
            [],
        )
    try:
        candidates = json.loads(content)
    except json.JSONDecodeError as error:
        raise FeatureExtractionFailure(
            "LLM_OUTPUT_INVALID",
            "DeepSeek returned invalid JSON.",
            True,
            [],
        ) from error
    if isinstance(candidates, dict) and isinstance(candidates.get("features"), list):
        for feature in candidates["features"]:
            if isinstance(feature, dict) and feature.get("status") == "unknown":
                for field in ("value", "confidence", "candidates"):
                    if feature.get(field) is None:
                        feature.pop(field, None)
    errors = validation_errors("ProblemFeatureCandidates", candidates)
    if errors:
        required_categories = {'dataScale', 'inputStructure', 'outputStructure', 'resourceConstraint', 'domainTag'}
        present_categories = {
            item['category'] for item in candidates.get('features', [])
            if isinstance(item, dict) and isinstance(item.get('category'), str)
        } if isinstance(candidates, dict) and isinstance(candidates.get('features'), list) else set()
        missing = sorted(required_categories - present_categories)
        raise FeatureExtractionFailure(
            "LLM_OUTPUT_INVALID",
            "DeepSeek returned features that do not satisfy the FocusMate contract."
            + (" Missing required categories: " + ', '.join(missing) + "." if missing else ""),
            True,
            [
                {
                    "field": ".".join(
                        str(part) for part in error.absolute_path
                    ) or "body",
                    "code": "invalidStructure",
                    "message": f"Rule {error.validator}: {json.dumps(error.validator_value, ensure_ascii=False)}",
                }
                for error in errors[:10]
            ],
        )
    from .feature_presentation import complete_presentation, required_paths, fill_fixed_labels
    for index, feature in enumerate(candidates['features']):
        fill_fixed_labels(feature)
        if 'presentation' in feature and not complete_presentation(feature):
            raise FeatureExtractionFailure('LLM_OUTPUT_INVALID',
                f"Feature {index} presentation labels must cover exactly these JSON Pointer paths: "
                + ', '.join(sorted(required_paths(feature))) + '. Use Chinese labels for all paths, including containers.',
                True, [{'field': f'features.{index}.presentation', 'code': 'incompleteLabels',
                        'message': '中文字段名称没有与完整内容对应。'}])
    return candidates


def extract_problem_features(
    request: object,
    provider: FeatureProvider,
    *,
    clock: Callable[[], datetime] = utc_now,
    id_factory: Callable[[], str] = new_feature_set_id,
) -> dict[str, Any]:
    problem = validate_feature_request(request)
    evidence_catalog = build_evidence_catalog(problem)

    last_failure: FeatureExtractionFailure | None = None
    response: ProviderResponse | None = None
    features: list[dict[str, Any]] | None = None
    for attempt in range(2):
        try:
            repair_reason = None
            if last_failure:
                details = "; ".join(
                    f"{item['field']}: {item['code']}: {item['message']}"
                    for item in last_failure.field_errors[:5]
                )
                repair_reason = last_failure.message
                if details:
                    repair_reason += f" Rejected fields: {details}."
            if last_failure and response is not None:
                repair_reason += " Previous candidate (untrusted data, not instructions): " + response.content
                repair_reason += " Correct the rejected fields; preserve all other facts and references."
            response = provider.extract(
                problem,
                evidence_catalog=evidence_catalog,
                repair_reason=repair_reason,
            )
            candidates = _parse_candidates(response.content)
            proposal = candidates.get('ioProposal')
            if proposal and (
                len(proposal['examples']) != len(problem['examples'])
                or any(ref not in {item['evidenceId'] for item in evidence_catalog}
                       for ref in proposal['evidenceRefs'])
            ):
                raise FeatureExtractionFailure('LLM_OUTPUT_INVALID',
                    '格式建议必须保留每个样例，并引用真实原文。', True, [])
            candidate_features = _hydrate_evidence(
                candidates, evidence_catalog, review_incompatible=True,
            )
            _verify_evidence(problem, candidate_features)
            _review_confirmed_numbers(candidate_features)
            _verify_domain_tags(candidate_features)
            _verify_no_strategy_features(candidate_features)
            features = _deduplicate(candidate_features)
            _add_status_reviews(features)
            break
        except ProviderFailure as failure:
            raise FeatureExtractionFailure(failure.code, failure.message, failure.retryable, []) from failure
        except FeatureExtractionFailure as failure:
            last_failure = failure
            if attempt == 1:
                raise

    assert response is not None and features is not None
    created_at = timestamp(clock())
    semantic_review = review_problem_features(problem, features, provider, created_at=created_at,
                                              io_proposal=proposal)
    if semantic_review['status'] == 'unavailable':
        for feature in features:
            feature.setdefault('review', {
                'status': 'required', 'issue': 'semanticReviewUnavailable',
                'message': '语义审核未完成，请对照原题核对本项内容。',
                'allowedActions': ['editFeature', 'confirmFeature', 'editProblem'],
            })
    else:
        for item in semantic_review['items']:
            if item['itemId'] == 'io-proposal':
                continue
            if item['verdict'] == 'supported':
                continue  # Model agreement never removes a deterministic warning.
            feature = features[int(item['itemId'].split('-')[1])]
            contradicted = item['verdict'] == 'contradicted'
            feature.setdefault('review', {
                'status': 'required',
                'issue': 'semanticContradiction' if contradicted else 'semanticInsufficient',
                'message': ('模型审核认为本项与原题不一致，请核对对象、数值及适用条件。'
                            if contradicted else '模型审核未找到充分依据，请对照原题核对本项内容。'),
                'allowedActions': ['editFeature', 'confirmFeature', 'editProblem'],
            })
    status = "reviewRequired" if any("review" in item for item in features) else "complete"
    result = {
        "featureSetId": id_factory(),
        "schemaVersion": "1.0.0",
        "createdAt": created_at,
        "problemRef": _problem_ref(problem),
        "problemContentHash": problem_content_hash(problem),
        "extractor": {
            "provider": "deepseek",
            "model": response.model,
            "promptVersion": PROMPT_VERSION,
            "providerResponseId": response.response_id,
        },
        "provenance": {"type": "llmGenerated"},
        "status": status,
        "features": features,
        "semanticReview": semantic_review,
    }
    if proposal:
        result['ioProposal'] = {
            'suggestion': deepcopy(proposal),
            'original': {key: deepcopy(problem[key]) for key in ('inputSpec', 'outputSpec', 'examples')},
        }
        result['status'] = 'reviewRequired'
    try:
        validate("ProblemFeatures", result)
    except Exception as error:
        raise FeatureExtractionFailure(
            "LLM_OUTPUT_INVALID",
            "The normalized features do not satisfy the FocusMate contract.",
            True,
            [],
        ) from error
    return result


def build_human_feature_set(
    problem: dict[str, Any],
    request: dict[str, Any],
    *,
    base_feature_set: dict[str, Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
    id_factory: Callable[[], str] = new_feature_set_id,
) -> dict[str, Any]:
    """Build a human-authoritative report without applying LLM semantic checks."""

    if isinstance(request, dict) and isinstance(request.get('features'), list):
        keys = [item.get('featureKey') for item in request['features'] if isinstance(item, dict)]
        from .feature_presentation import complete_presentation
        if len(keys) != len(set(keys)) or any(
            ('presentation' in item or request.get('requireCompletePresentation')) and not complete_presentation(item) for item in request['features'] if isinstance(item, dict)
        ):
            raise FeatureExtractionFailure('INVALID_FEATURE_CONFIRMATION', '条件存在重复或尚未完整展示，请重新核对。', False, [])

    errors = validation_errors("ProblemFeatureConfirmationRequest", request)
    if errors:
        raise FeatureExtractionFailure(
            "INVALID_FEATURE_CONFIRMATION",
            "The human-reviewed feature report is incomplete or malformed.", False,
            [{
                "field": ".".join(str(part) for part in error.absolute_path) or "body",
                "code": "invalidStructure",
                "message": "请保留五类题目特征、解决冲突并为确定内容选择原文证据。",
            } for error in errors[:10]],
        )

    based_on = request.get("basedOnFeatureSetId")
    if based_on and (
        base_feature_set is None
        or base_feature_set.get("featureSetId") != based_on
    ):
        raise FeatureExtractionFailure(
            "FEATURE_CONTEXT_MISMATCH", "The base feature report was not found.", False,
            [{"field": "basedOnFeatureSetId", "code": "contextMismatch",
              "message": "选择的AI解析报告不属于当前题目内容。"}],
        )

    if base_feature_set and base_feature_set.get('ioProposal') and request.get('ioProposalDecision') != 'keepOriginal':
        raise FeatureExtractionFailure('INVALID_FEATURE_CONFIRMATION',
            '请先处理输入输出建议：修改题目后重新整理，或明确选择保留原格式。', False, [])

    try:
        features = _hydrate_evidence(
            {"features": request["features"]}, build_evidence_catalog(problem),
        )
    except FeatureExtractionFailure as failure:
        raise FeatureExtractionFailure(
            "INVALID_FEATURE_CONFIRMATION", failure.message, False, failure.field_errors,
        ) from failure

    result: dict[str, Any] = {
        "featureSetId": id_factory(), "schemaVersion": "1.0.0",
        "createdAt": timestamp(clock()), "problemRef": _problem_ref(problem),
        "problemContentHash": problem_content_hash(problem),
        "provenance": {
            "type": "humanEdited" if based_on else "humanCreated",
            **({"basedOnFeatureSetId": based_on} if based_on else {}),
            "confirmedAt": timestamp(clock()),
        },
        "status": (
            "partial"
            if any(item["status"] in {"hypothesis", "unknown"} for item in features)
            else "complete"
        ),
        "features": features,
    }
    if base_feature_set and "extractor" in base_feature_set:
        result["extractor"] = deepcopy(base_feature_set["extractor"])
    if base_feature_set and base_feature_set.get('ioProposal'):
        result['ioProposal'] = deepcopy(base_feature_set['ioProposal'])
        result['ioProposalDecision'] = 'keepOriginal'
    try:
        validate("ProblemFeatures", result)
    except Exception as error:
        raise FeatureExtractionFailure(
            "INVALID_FEATURE_CONFIRMATION",
            "The human-reviewed feature report does not satisfy the FocusMate contract.",
            False, [],
        ) from error
    return result


def build_feature_error_payload(
    request_id: str,
    failure: FeatureExtractionFailure,
    *,
    problem_ref: dict[str, Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    configuration_codes = {
        "LLM_CONFIGURATION_MISSING", "LLM_AUTHENTICATION_FAILED", "LLM_PAYMENT_REQUIRED",
    }
    if failure.code in configuration_codes:
        user_message = "题目解析服务尚未正确配置；题目草稿已经保留。"
        allowed_actions = ["checkConfiguration", "createFeaturesManually"]
    elif failure.code in {"INVALID_FEATURE_CONFIRMATION", "FEATURE_CONTEXT_MISMATCH"}:
        user_message = "人工题目特征尚未满足保存条件，请根据标记内容修改。"
        allowed_actions = ["editProblem", "createFeaturesManually"]
    elif failure.code in {"LLM_UNAVAILABLE", "FEATURE_EXTRACTION_INTERNAL_ERROR"}:
        user_message = "题目解析服务暂时不可用；可以稍后重试或改为人工填写。"
        allowed_actions = ["retry", "createFeaturesManually"]
    else:
        user_message = "AI没有形成可用的题目解析报告；题目草稿没有丢失。"
        allowed_actions = ["retry", "editProblem", "createFeaturesManually"]
    payload: dict[str, Any] = {
        "requestId": request_id,
        "schemaVersion": "1.0.0",
        "createdAt": timestamp(clock()),
        "code": failure.code,
        "message": failure.message,
        "userMessage": user_message,
        "allowedActions": allowed_actions,
        "retryable": failure.retryable,
        "fieldErrors": failure.field_errors,
    }
    if problem_ref:
        payload["problemRef"] = problem_ref
    validate("ProblemFeatureExtractionError", payload)
    return payload

"""Bounded, advisory review of extracted facts against authoritative problem text.

This pre-goal report never changes a feature, resolves a conflict, or replaces a
human decision. The model chooses fixed verdicts and source IDs; Python alone
assigns item identities, verifies the response, and fills in verbatim evidence.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Protocol

from .deepseek_provider import ProviderResponse
from .problem_evidence import build_evidence_catalog
from .problem_identity import problem_content_hash
from .schema_validation import validation_errors


PROMPT_VERSION = "problem-feature-review-io-v5"
MAX_REVIEW_BYTES = 128 * 1024
SOURCE_FIELDS = ("statement", "inputSpec", "outputSpec", "constraints", "examples")


class FeatureReviewProvider(Protocol):
    def review(
        self, problem: dict[str, Any], *, features: list[dict[str, Any]],
        evidence_catalog: list[dict[str, str]],
    ) -> ProviderResponse: ...


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_items(
    features: list[dict[str, Any]], catalog: list[dict[str, str]],
) -> list[dict[str, Any]]:
    by_id = {entry["evidenceId"]: entry for entry in catalog}

    def refs(evidence: list[dict[str, Any]]) -> list[str]:
        selected = []
        for item in evidence:
            evidence_id = item.get("evidenceId")
            if evidence_id is None:
                matches = [entry["evidenceId"] for entry in catalog if all(
                    item.get(key) == entry[key]
                    for key in ("sourceField", "sourcePath", "sourceText")
                )]
                evidence_id = matches[0] if matches else None
            entry = by_id.get(evidence_id)
            if entry is None or any(
                item.get(key) != entry[key]
                for key in ("sourceField", "sourcePath", "sourceText")
            ):
                raise ValueError("Invalid source evidence")
            if evidence_id not in selected:
                selected.append(evidence_id)
        return selected

    items = []
    for index, feature in enumerate(features):
        item = {
            "itemId": f"feature-{index}", "featureKey": feature["featureKey"],
            "category": feature["category"], "status": feature["status"],
            "evidenceRefs": refs(feature["evidence"]),
        }
        if "value" in feature:
            item["value"] = deepcopy(feature["value"])
        if 'presentation' in feature:
            item['presentation'] = deepcopy(feature['presentation'])
        if "candidates" in feature:
            item["candidates"] = [{
                "value": deepcopy(candidate["value"]),
                "evidenceRefs": refs(candidate["evidence"]),
            } for candidate in feature["candidates"]]
        items.append(item)
    return items


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-JSON constant")


def _parse_review(content: str) -> dict[str, Any]:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ValueError("Review response exceeds the boundary")
    candidate = json.loads(
        content, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
    )
    if validation_errors("ProblemFeatureReviewCandidates", candidate):
        raise ValueError("Invalid review contract")
    return candidate


def review_problem_features(
    problem: dict[str, Any], features: list[dict[str, Any]],
    provider: FeatureReviewProvider, *, created_at: str, io_proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make at most one independent review call and return a safe advisory report.

    Callers supply an already validated problem and normalized feature list. An
    unavailable reviewer or invalid response does not discard those features.
    """
    source = deepcopy({key: problem[key] for key in SOURCE_FIELDS})
    catalog = build_evidence_catalog(source)
    content_hash = problem_content_hash(problem)
    report = {
        "status": "unavailable", "promptVersion": PROMPT_VERSION,
        "problemRef": {"problemId": problem["problemId"], "problemVersion": problem["version"]},
        "problemContentHash": content_hash,
        "inputHash": _hash({"promptVersion": PROMPT_VERSION, "problemContentHash": content_hash}),
        "createdAt": created_at, "items": [],
    }
    try:
        items = _review_items(features, catalog)
        if io_proposal:
            items.append({'itemId': 'io-proposal', 'featureKey': 'io.proposal',
                          'category': 'ioConvention', 'status': 'hypothesis',
                          'value': deepcopy(io_proposal),
                          'evidenceRefs': list(io_proposal['evidenceRefs'])})
        if not items:
            raise ValueError("No features to review")
        report["inputHash"] = _hash({
            "promptVersion": PROMPT_VERSION, "problemSource": source,
            "evidenceCatalog": catalog, "features": items,
        })
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return {**report, "failureCode": "REVIEW_INVALID"}

    try:
        review = getattr(provider, "review", None)
        if not callable(review):
            return {**report, "failureCode": "REVIEW_UNAVAILABLE"}
        response = review(deepcopy(source), features=deepcopy(items), evidence_catalog=deepcopy(catalog))
    except Exception:
        return {**report, "failureCode": "REVIEW_UNAVAILABLE"}

    try:
        candidate = _parse_review(response.content)
        expected = {item["itemId"]: item for item in items}
        seen: dict[str, dict[str, Any]] = {}
        by_id = {entry["evidenceId"]: entry for entry in catalog}
        for verdict in candidate["items"]:
            item_id = verdict["itemId"]
            if item_id not in expected or item_id in seen:
                raise ValueError("Unexpected or repeated review item")
            if any(ref not in by_id for ref in verdict["evidenceRefs"]):
                raise ValueError("Unknown review evidence")
            item = expected[item_id]
            if (verdict["verdict"] == "supported" and item["status"] != "unknown"
                    and not verdict["evidenceRefs"]):
                raise ValueError("Known features require supporting evidence")
            seen[item_id] = {
                **verdict, "featureKey": item["featureKey"], "category": item["category"],
                "evidence": [deepcopy(by_id[ref]) for ref in verdict["evidenceRefs"]],
            }
        if set(seen) != set(expected):
            raise ValueError("Missing review item")
        completed = {
            **report, "status": "completed", "model": response.model,
            "providerResponseId": response.response_id,
            "items": [seen[item["itemId"]] for item in items],
        }
        if validation_errors("ProblemFeatureReviewResult", completed):
            raise ValueError("Invalid review report")
        return completed
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return {**report, "failureCode": "REVIEW_INVALID"}

"""Generated duplicates and surplus cases must not discard usable coverage."""

from copy import deepcopy
import json

import pytest

from services.verifier_builder import case_selection
from services.verifier_builder.case_selection import CaseSetError, normalize_cases


def small_cases(count=8):
    return [
        {"caseKey": f"small-{n}", "scale": "small",
         "tags": ["minimumSize" if n == 0 else "randomSmall"], "stdin": f"{n}\n"}
        for n in range(count)
    ]


def raw_cases(items):
    return json.dumps({"cases": items}).encode("utf-8")


def test_repeated_input_does_not_discard_eight_distinct_small_cases():
    items = small_cases()
    items.append({**items[1], "caseKey": "same-input-different-name"})
    selected, audit = normalize_cases(raw_cases(items), False)
    assert len(selected) == 8
    assert audit["duplicateCount"] == 1
    assert audit["rawCaseCount"] == 9
    assert audit["selectedSmallCount"] == 8


def test_surplus_large_cases_are_selected_instead_of_rejecting_all():
    items = small_cases() + [
        {"caseKey": f"large-{n}", "scale": "large",
         "tags": ["maximumSize" if n == 6 else "randomLarge"], "stdin": f"large-{n}\n"}
        for n in range(7)
    ]
    selected, audit = normalize_cases(raw_cases(items), True)
    assert sum(case["scale"] == "large" for case in selected) == 4
    assert any("maximumSize" in case["tags"] for case in selected)
    assert audit["omittedLargeCount"] == 3


def test_selection_keeps_late_boundary_and_tag_diversity_deterministically():
    items = small_cases(30)
    items[0]["tags"] = ["randomSmall"]
    items[26]["tags"] = ["minimumSize"]
    items[27]["tags"] = ["duplicates"]
    items[28]["tags"] = ["zeroValues"]
    items[29]["tags"] = ["reverseSorted"]
    original = deepcopy(items)
    selected, audit = normalize_cases(raw_cases(items), False)
    assert len(selected) == 24
    assert {"small-26", "small-27", "small-28", "small-29"} <= {case["caseKey"] for case in selected}
    assert any("randomSmall" in case["tags"] for case in selected)
    assert audit["omittedSmallCount"] == 6
    assert (selected, audit) == normalize_cases(raw_cases(items), False)
    assert items == original


def test_same_input_prefers_small_without_combining_the_duplicate_tags():
    items = small_cases()
    items.insert(0, {**items[1], "caseKey": "claimed-large", "scale": "large", "tags": ["maximumSize"]})
    items.append({"caseKey": "real-large", "scale": "large", "tags": ["maximumSize"], "stdin": "1000\n"})
    selected, audit = normalize_cases(raw_cases(items), True)
    assert len(selected) == 9
    duplicate = next(case for case in selected if case["stdin"] == "1\n")
    assert duplicate["caseKey"] == "small-1" and duplicate["scale"] == "small"
    assert duplicate["tags"] == ["randomSmall"]
    assert audit["duplicateCount"] == 1


def test_duplicate_cannot_manufacture_random_and_boundary_coverage():
    items = small_cases()
    for item in items:
        item["tags"] = ["sorted"]
    items[0]["tags"] = ["minimumSize"]
    items.append({**items[0], "caseKey": "same-as-boundary", "tags": ["randomSmall"]})
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw_cases(items), False)
    assert caught.value.reason == "insufficientSmallCoverage"


def test_identical_record_is_deduplicated_but_reused_key_with_different_input_fails():
    items = small_cases()
    selected, audit = normalize_cases(raw_cases(items + [deepcopy(items[0])]), False)
    assert len(selected) == 8 and audit["duplicateCount"] == 1
    items.append({**items[0], "stdin": "different input\n"})
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw_cases(items), False)
    assert caught.value.reason == "invalidStructure"


@pytest.mark.parametrize("large_allowed,expected", [(False, "unexpectedLargeCases"), (True, "missingLargeCoverage")])
def test_deduplication_cannot_hide_disallowed_or_required_large_cases(large_allowed, expected):
    items = small_cases()
    items.append({**items[0], "caseKey": "large-duplicate", "scale": "large"})
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw_cases(items), large_allowed)
    assert caught.value.reason == expected


@pytest.mark.parametrize("variant", ["few", "duplicates", "noRandom", "noBoundary"])
def test_missing_distinct_small_coverage_remains_an_error(variant):
    items = small_cases()
    if variant == "few": items.pop()
    if variant == "duplicates": items[-1]["stdin"] = items[0]["stdin"]
    if variant == "noRandom":
        for item in items: item["tags"] = ["minimumSize"]
    if variant == "noBoundary":
        for item in items: item["tags"] = ["randomSmall"]
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw_cases(items), False)
    assert caught.value.reason == "insufficientSmallCoverage"


@pytest.mark.parametrize("raw", [b"not JSON", b'{"cases":[],"cases":[]}', b'{"cases":NaN}', b"\xff", b'{"cases":{},"other":0}', b'{"cases":{}}'])
def test_invalid_json_or_top_level_shape_is_not_salvaged(raw):
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw, False)
    assert caught.value.reason == "invalidStructure"


@pytest.mark.parametrize("variant", ["unknownTag", "duplicateTag", "extraField", "badKey", "badScale", "surrogate", "notString", "nonObject", "tooMany"])
def test_invalid_records_are_checked_before_any_selection(variant):
    items = small_cases(30)
    if variant == "unknownTag": items[-1]["tags"] = ["SECRET-unknown"]
    if variant == "duplicateTag": items[-1]["tags"] = ["randomSmall", "randomSmall"]
    if variant == "extraField": items[-1]["expected"] = "SECRET-answer"
    if variant == "badKey": items[-1]["caseKey"] = "SECRET input key!"
    if variant == "badScale": items[-1]["scale"] = "medium"
    if variant == "surrogate": items[-1]["stdin"] = "\ud800"
    if variant == "notString": items[-1]["stdin"] = 123
    if variant == "nonObject": items[-1] = None
    if variant == "tooMany": items *= 3
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw_cases(items), False)
    assert caught.value.reason == "invalidStructure"
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("limit", ["individual", "aggregate", "raw"])
def test_material_limits_apply_before_deduplication_and_selection(monkeypatch, limit):
    items = small_cases(30)
    if limit == "individual":
        monkeypatch.setattr(case_selection, "MAX_TEXT_BYTES", 8)
        items[-1]["stdin"] = "x" * 9
    elif limit == "aggregate":
        monkeypatch.setattr(case_selection, "MATERIAL_MAX_BYTES", 1024)
        items = small_cases()
        # UTF-8 content and its JSON representation remain bounded as a batch.
        for item in items: item["stdin"] += "中" * 60
    else:
        monkeypatch.setattr(case_selection, "MATERIAL_MAX_BYTES", 8)
    with pytest.raises(CaseSetError) as caught:
        normalize_cases(raw_cases(items), False)
    assert caught.value.reason == "materialLimit"


def test_audit_has_only_counts_and_hashes_never_private_inputs_or_names():
    items = small_cases()
    items[0]["stdin"] = "SECRET-test-input\n"
    items[0]["caseKey"] = "SECRET-case-key"
    _, audit = normalize_cases(raw_cases(items), False)
    assert "SECRET" not in json.dumps(audit)
    for value in audit.values():
        assert type(value) is int or (
            isinstance(value, list) and all(text.startswith("sha256:") and len(text) == 71 for text in value)
        )

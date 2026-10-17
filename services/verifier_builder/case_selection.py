"""Select bounded, distinct inputs without treating extra cases as bad answers.

Tags remain the generator's coverage claims; selection does not establish input
legality or reference agreement. Those checks still belong to the builder.
"""

from __future__ import annotations

import hashlib
import re

from apps.api.deepseek_provider import strict_json_loads
from apps.api.trusted_assets import MATERIAL_MAX_BYTES, MAX_TEXT_BYTES


TAGS = frozenset({"minimumSize", "maximumSize", "randomSmall", "randomLarge", "duplicates",
                  "allEqual", "sorted", "reverseSorted", "negativeValues", "zeroValues",
                  "disconnected", "cycle", "singleElement", "emptyAllowed", "boundary"})
BOUNDARY_TAGS = frozenset({"minimumSize", "maximumSize", "singleElement", "emptyAllowed", "boundary"})
RANDOM_TAGS = frozenset({"randomSmall", "randomLarge"})
SMALL_TARGET = 24
LARGE_TARGET = 4


class CaseSetError(ValueError):
    """A fixed reason, never the rejected private input or generated field text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _boundary(item: dict) -> bool:
    tags = set(item["tags"])
    return bool(tags & BOUNDARY_TAGS) and not tags & RANDOM_TAGS


def _select(items: list[dict], target: int, *, small: bool) -> list[dict]:
    if len(items) <= target:
        return items
    selected: set[int] = set()
    covered: set[str] = set()

    def choose(indices):
        # Cover new boundary categories first, then other new categories; source
        # order breaks equal scores so the same output always selects the same set.
        index = max(indices, key=lambda i: (
            len((set(items[i]["tags"]) - covered) & BOUNDARY_TAGS),
            len(set(items[i]["tags"]) - covered), -i))
        selected.add(index)
        covered.update(items[index]["tags"])

    if small:
        # Required coverage is reserved before greedy selection can fill the cap.
        choose(i for i, item in enumerate(items) if _boundary(item))
        choose(i for i, item in enumerate(items) if "randomSmall" in item["tags"])
    while len(selected) < target:
        choose(i for i in range(len(items)) if i not in selected)
    return [item for i, item in enumerate(items) if i in selected]


def normalize_cases(raw: bytes, large_applicable: bool) -> tuple[list[dict], dict]:
    """Validate every source record, deduplicate exact inputs, then select coverage.

    A duplicate retains one complete record, never a union of its labels. A small
    representative wins over a large one so duplicate declarations cannot bypass
    the slow oracle. Two different inputs cannot share a case key.
    """
    try:
        if not isinstance(raw, bytes):
            raise CaseSetError("invalidStructure")
        if len(raw) > MATERIAL_MAX_BYTES:
            raise CaseSetError("materialLimit")
        document = strict_json_loads(raw.decode("utf-8"))
        if not isinstance(document, dict) or set(document) != {"cases"}:
            raise CaseSetError("invalidStructure")
        items = document["cases"]
        if not isinstance(items, list) or len(items) > 64:
            raise CaseSetError("invalidStructure")
        keys: dict[str, bytes] = {}
        unique: dict[bytes, dict] = {}
        byte_count = 0
        has_large = False
        for item in items:
            if not isinstance(item, dict) or set(item) != {"caseKey", "scale", "tags", "stdin"}:
                raise CaseSetError("invalidStructure")
            key, scale, tags, stdin = item["caseKey"], item["scale"], item["tags"], item["stdin"]
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key)
                or scale not in ("small", "large") or not isinstance(tags, list) or not tags
                or any(not isinstance(tag, str) or tag not in TAGS for tag in tags)
                or len(tags) != len(set(tags)) or not isinstance(stdin, str)):
                raise CaseSetError("invalidStructure")
            encoded = stdin.encode("utf-8")
            byte_count += len(encoded)
            if len(encoded) > MAX_TEXT_BYTES or byte_count > MATERIAL_MAX_BYTES:
                raise CaseSetError("materialLimit")
            if key in keys and keys[key] != encoded:
                raise CaseSetError("invalidStructure")
            keys[key] = encoded
            has_large |= scale == "large"
            previous = unique.get(encoded)
            if previous is None or (scale == "small" and previous["scale"] == "large"):
                unique[encoded] = item
        if has_large and not large_applicable:
            raise CaseSetError("unexpectedLargeCases")
        small = [item for item in unique.values() if item["scale"] == "small"]
        large = [item for item in unique.values() if item["scale"] == "large"]
        if (len(small) < 8 or not any("randomSmall" in item["tags"] for item in small)
            or not any(_boundary(item) for item in small)):
            raise CaseSetError("insufficientSmallCoverage")
        if large_applicable and not large:
            raise CaseSetError("missingLargeCoverage")
        chosen_small = _select(small, SMALL_TARGET, small=True)
        chosen_large = _select(large, LARGE_TARGET, small=False)
        chosen_keys = {item["caseKey"] for item in chosen_small + chosen_large}
        selected = [item for item in unique.values() if item["caseKey"] in chosen_keys]
        audit = {
            "rawCaseCount": len(items), "uniqueCaseCount": len(unique),
            "duplicateCount": len(items) - len(unique),
            "selectedSmallCount": len(chosen_small), "selectedLargeCount": len(chosen_large),
            "omittedSmallCount": len(small) - len(chosen_small),
            "omittedLargeCount": len(large) - len(chosen_large),
            "selectedInputHashes": ["sha256:" + hashlib.sha256(item["stdin"].encode("utf-8")).hexdigest()
                                    for item in selected],
        }
        return selected, audit
    except CaseSetError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CaseSetError("invalidStructure") from None

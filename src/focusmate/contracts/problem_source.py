"""Canonical problem identity and evidence, shared by intake and compilation."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def problem_content_hash(problem: dict[str, Any]) -> str:
    content = {key: problem[key] for key in (
        "statement", "inputSpec", "outputSpec", "constraints", "examples",
        "languagePolicy", "ioMode",
    )}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_evidence_catalog(problem: dict[str, Any]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for field in ("statement", "inputSpec", "outputSpec", "constraints"):
        lines = [line.strip() for line in problem[field].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            catalog.append({"evidenceId": f"{field}:{index}", "sourceField": field,
                            "sourcePath": field, "sourceText": line})
    for index, example in enumerate(problem["examples"]):
        for side in ("input", "output"):
            if example[side]:
                catalog.append({"evidenceId": f"examples:{index}:{side}", "sourceField": "examples",
                                "sourcePath": f"examples.{index}.{side}", "sourceText": example[side]})
    return catalog

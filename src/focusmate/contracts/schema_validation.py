"""Runtime access to the repository's versioned JSON Schema contracts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_ROOT = ROOT / "schemas"


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict[str, dict[str, Any]], Registry]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in SCHEMA_ROOT.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[str(schema["title"])] = schema

    registry = Registry().with_resources(
        (str(schema["$id"]), Resource.from_contents(schema))
        for schema in schemas.values()
    )
    return schemas, registry


def validation_errors(title: str, instance: object) -> list[ValidationError]:
    """Return every contract violation in deterministic field-path order."""

    if isinstance(instance, dict) and instance.get('schemaVersion') == '2.0.0' and title in ('GoalContract', 'GoalState', 'ManagerSession'):
        title += 'V2'
    schemas, registry = _catalog()
    schema = schemas[title]
    validator_class = validator_for(schema)
    validator = validator_class(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )
    return sorted(validator.iter_errors(instance), key=lambda error: list(error.path))


def validate(title: str, instance: object) -> None:
    """Raise the first validation error when an object violates a contract."""

    errors = validation_errors(title, instance)
    if errors:
        raise errors[0]

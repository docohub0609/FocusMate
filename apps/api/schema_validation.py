"""Compatibility imports for API callers of the shared contract validator."""

from src.focusmate.contracts.schema_validation import (
    _catalog,
    validate,
    validation_errors,
)

__all__ = ["_catalog", "validate", "validation_errors"]

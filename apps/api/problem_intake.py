"""Create an in-memory ProblemPackage from a validated intake request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Callable
from uuid import uuid4

from jsonschema.exceptions import ValidationError

from .schema_validation import validate, validation_errors


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


@dataclass(frozen=True)
class IntakeFailure(Exception):
    """A safe, request-level failure that can be returned to the client."""

    code: str
    message: str
    field_errors: list[dict[str, str]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_problem_id() -> str:
    return f"problem-{uuid4()}"


def new_request_id() -> str:
    return f"request-{uuid4()}"


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _field_path(error: ValidationError) -> str:
    parts = [str(part) for part in error.absolute_path]
    if error.validator == "required":
        missing = next(
            (name for name in error.validator_value if name not in error.instance),
            None,
        )
        if missing:
            parts.append(str(missing))
    elif error.validator == "additionalProperties":
        names = re.findall(r"'([^']+)'", error.message)
        if names:
            parts.append(names[0])
    return ".".join(parts) or "body"


def _field_code(error: ValidationError) -> str:
    if error.validator == "required":
        return "required"
    if error.validator == "pattern":
        return "blank"
    if error.validator == "type":
        return "invalidType"
    if error.validator == "additionalProperties":
        return "unknownField"
    if error.validator == "const":
        return "invalidValue"
    return "invalidStructure"


def _field_message(field: str, code: str) -> str:
    messages = {
        "required": f"{field} is required.",
        "blank": f"{field} must contain non-whitespace text.",
        "invalidType": f"{field} has an invalid type.",
        "invalidValue": f"{field} has an unsupported value.",
        "unknownField": f"{field} is not accepted.",
        "invalidStructure": f"{field} has an invalid structure.",
    }
    return messages[code]


def _format_errors(errors: list[ValidationError]) -> list[dict[str, str]]:
    formatted = []
    for error in errors:
        field = _field_path(error)
        code = _field_code(error)
        formatted.append(
            {"field": field, "code": code, "message": _field_message(field, code)}
        )
    return formatted


def validate_intake_request(payload: object) -> dict[str, Any]:
    """Validate client input and return it with a narrowed dictionary type."""

    if isinstance(payload, dict):
        if payload.get("language") not in (None, "python3"):
            raise IntakeFailure(
                "UNSUPPORTED_LANGUAGE",
                "FocusMate V1 only accepts Python 3 problems.",
                [{"field": "language", "code": "invalidValue", "message": "language must be python3."}],
            )
        if payload.get("ioMode") not in (None, "standardInputOutput"):
            raise IntakeFailure(
                "UNSUPPORTED_IO_MODE",
                "FocusMate V1 only accepts standard input and output.",
                [{"field": "ioMode", "code": "invalidValue", "message": "ioMode must be standardInputOutput."}],
            )

    errors = validation_errors("ProblemIntakeRequest", payload)
    if errors:
        raise IntakeFailure(
            "INVALID_PROBLEM_INPUT",
            "The problem input contains invalid fields.",
            _format_errors(errors),
        )
    return payload  # type: ignore[return-value]


def create_problem_package(
    payload: object,
    *,
    clock: Clock = utc_now,
    id_factory: IdFactory = new_problem_id,
) -> dict[str, Any]:
    """Build, but do not persist, the first version of a ProblemPackage."""

    request = validate_intake_request(payload)
    package = {
        "problemId": id_factory(),
        "schemaVersion": "1.0.0",
        "version": 1,
        "createdAt": timestamp(clock()),
        "source": "userProvided",
        "statement": request["statement"],
        "inputSpec": request["inputSpec"],
        "outputSpec": request["outputSpec"],
        "constraints": request["constraints"],
        "examples": request["examples"],
        "languagePolicy": {"allowed": ["python3"]},
        "ioMode": "standardInputOutput",
        "verifierAssetStatus": "missing",
    }
    if "title" in request:
        package["title"] = request["title"]
    validate("ProblemPackage", package)
    return package


def build_error_payload(
    request_id: str,
    code: str,
    message: str,
    field_errors: list[dict[str, str]],
    *,
    clock: Clock = utc_now,
) -> dict[str, Any]:
    """Create and validate a safe pre-Goal API error response."""

    payload = {
        "requestId": request_id,
        "schemaVersion": "1.0.0",
        "createdAt": timestamp(clock()),
        "code": code,
        "message": message,
        "fieldErrors": field_errors,
    }
    validate("ProblemIntakeError", payload)
    return payload

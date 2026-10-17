"""Build the generic draft GoalContract for an algorithm problem."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from jsonschema.exceptions import ValidationError

from focusmate.contracts.schema_validation import validate


class GoalTemplateError(ValueError):
    """Raised when a generic GoalContract cannot be built safely."""


def build_generic_algorithm_goal_contract(
    goal_record: Mapping[str, object], *, created_at: str
) -> dict[str, object]:
    """Return a schema-valid, generic draft contract for a valid GoalRecord."""

    if not isinstance(created_at, str):
        raise GoalTemplateError("Cannot build generic GoalContract from invalid input.")

    try:
        validate("GoalRecord", goal_record)
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        contract: dict[str, object] = {
            "goalId": goal_record["goalId"],
            "schemaVersion": "1.0.0",
            "version": 1,
            "createdAt": created_at,
            "problemRef": {
                "problemId": goal_record["problemRef"]["problemId"],  # type: ignore[index]
                "problemVersion": goal_record["problemRef"]["problemVersion"],  # type: ignore[index]
            },
            "objective": "Produce a Python solution accepted by the trusted OJ.",
            "subgoals": [
                {
                    "subgoalId": "understand-problem",
                    "stage": "problemUnderstanding",
                    "objective": "Understand the problem requirements.",
                    "dependsOn": [],
                    "acceptableEvidence": [
                        "confirmedProblemFeature",
                        "evaluatedUserExplanation",
                    ],
                },
                {
                    "subgoalId": "choose-feasible-strategy",
                    "stage": "feasibleStrategy",
                    "objective": "Choose a strategy that fits the constraints.",
                    "dependsOn": [],
                    "acceptableEvidence": [
                        "complexityCheck",
                        "evaluatedUserExplanation",
                        "codeStructureCheck",
                    ],
                },
                {
                    "subgoalId": "define-state-and-correctness",
                    "stage": "coreStateAndCorrectness",
                    "objective": "Explain the maintained state and correctness.",
                    "dependsOn": [],
                    "acceptableEvidence": [
                        "evaluatedUserExplanation",
                        "codeStructureCheck",
                        "targetedRunResult",
                    ],
                },
                {
                    "subgoalId": "implement-core-logic",
                    "stage": "coreImplementation",
                    "objective": "Implement the core strategy.",
                    "dependsOn": [],
                    "acceptableEvidence": [
                        "codeStructureCheck",
                        "compileResult",
                        "runResult",
                    ],
                },
                {
                    "subgoalId": "handle-boundaries-and-tests",
                    "stage": "boundaryAndTesting",
                    "objective": "Test boundaries and representative inputs.",
                    "dependsOn": [],
                    "acceptableEvidence": [
                        "publicSampleRunResult",
                        "targetedRunResult",
                    ],
                },
            ],
            "navigationPolicy": {
                "mode": "adaptive",
                "stageOrder": "defaultOnly",
                "allowSkip": True,
                "allowRevisit": True,
                "offRouteHandling": "temporaryBranch",
                "stalledRouteFallback": "latestVerifiedSubgoal",
                "processStagesRequiredForCompletion": False,
            },
            "finalCriterion": {"verifierType": "finalOj", "requiredVerdict": "AC"},
            "coachPolicy": {"completeSolutionAllowed": False},
            "contractStatus": "draft",
        }
        validate("GoalContract", contract)
    except (KeyError, TypeError, ValidationError, ValueError) as error:
        raise GoalTemplateError("Cannot build generic GoalContract from invalid input.") from error
    return contract

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from focusmate.contracts.schema_validation import validate
from focusmate.core.goal_template import (
    GoalTemplateError,
    build_generic_algorithm_goal_contract,
)


VALID_GOAL_RECORD = {
    "goalId": "goal-001",
    "schemaVersion": "1.0.0",
    "createdAt": "2026-09-11T10:00:00Z",
    "problemRef": {"problemId": "problem-001", "problemVersion": 1},
    "status": "active",
}

EXPECTED_SUBGOALS = [
    {
        "subgoalId": "understand-problem",
        "stage": "problemUnderstanding",
        "objective": "Understand the problem requirements.",
        "dependsOn": [],
        "acceptableEvidence": ["confirmedProblemFeature", "evaluatedUserExplanation"],
    },
    {
        "subgoalId": "choose-feasible-strategy",
        "stage": "feasibleStrategy",
        "objective": "Choose a strategy that fits the constraints.",
        "dependsOn": [],
        "acceptableEvidence": ["complexityCheck", "evaluatedUserExplanation", "codeStructureCheck"],
    },
    {
        "subgoalId": "define-state-and-correctness",
        "stage": "coreStateAndCorrectness",
        "objective": "Explain the maintained state and correctness.",
        "dependsOn": [],
        "acceptableEvidence": ["evaluatedUserExplanation", "codeStructureCheck", "targetedRunResult"],
    },
    {
        "subgoalId": "implement-core-logic",
        "stage": "coreImplementation",
        "objective": "Implement the core strategy.",
        "dependsOn": [],
        "acceptableEvidence": ["codeStructureCheck", "compileResult", "runResult"],
    },
    {
        "subgoalId": "handle-boundaries-and-tests",
        "stage": "boundaryAndTesting",
        "objective": "Test boundaries and representative inputs.",
        "dependsOn": [],
        "acceptableEvidence": ["publicSampleRunResult", "targetedRunResult"],
    },
]

EXPECTED_NAVIGATION_POLICY = {
    "mode": "adaptive",
    "stageOrder": "defaultOnly",
    "allowSkip": True,
    "allowRevisit": True,
    "offRouteHandling": "temporaryBranch",
    "stalledRouteFallback": "latestVerifiedSubgoal",
    "processStagesRequiredForCompletion": False,
}


def build_contract(goal_record: dict[str, object] | None = None) -> dict[str, object]:
    return build_generic_algorithm_goal_contract(
        goal_record or VALID_GOAL_RECORD,
        created_at="2026-09-11T10:30:00Z",
    )


def test_valid_goal_record_produces_schema_valid_draft_contract() -> None:
    contract = build_contract()

    validate("GoalContract", contract)
    assert contract["schemaVersion"] == "1.0.0"
    assert contract["version"] == 1
    assert contract["contractStatus"] == "draft"
    assert contract["subgoals"] == EXPECTED_SUBGOALS
    assert contract["navigationPolicy"] == EXPECTED_NAVIGATION_POLICY
    assert contract["coachPolicy"] == {"completeSolutionAllowed": False}


def test_contract_has_fixed_final_criterion_and_no_dependencies() -> None:
    contract = build_contract()

    assert contract["finalCriterion"] == {"verifierType": "finalOj", "requiredVerdict": "AC"}
    assert all(subgoal["dependsOn"] == [] for subgoal in contract["subgoals"])


def test_different_goal_contexts_only_change_copied_identity() -> None:
    other_goal = {
        **VALID_GOAL_RECORD,
        "goalId": "goal-002",
        "problemRef": {"problemId": "problem-002", "problemVersion": 2},
    }
    first = build_contract()
    second = build_contract(other_goal)

    for field in ("goalId", "problemRef"):
        first.pop(field)
        second.pop(field)
    assert first == second


def test_mutating_a_return_value_does_not_affect_a_later_call() -> None:
    first = build_contract()
    first["problemRef"]["problemId"] = "mutated"  # type: ignore[index]
    first["subgoals"][0]["acceptableEvidence"].append("runResult")  # type: ignore[index]
    first["navigationPolicy"]["allowSkip"] = False  # type: ignore[index]

    assert build_contract()["problemRef"] == {"problemId": "problem-001", "problemVersion": 1}
    assert build_contract()["subgoals"] == EXPECTED_SUBGOALS
    assert build_contract()["navigationPolicy"] == EXPECTED_NAVIGATION_POLICY


@pytest.mark.parametrize(
    "goal_record, created_at",
    [
        ({key: value for key, value in VALID_GOAL_RECORD.items() if key != "goalId"}, "2026-09-11T10:30:00Z"),
        ({**VALID_GOAL_RECORD, "problemRef": {"problemId": "problem-001", "problemVersion": 0}}, "2026-09-11T10:30:00Z"),
        ({**VALID_GOAL_RECORD, "unexpected": True}, "2026-09-11T10:30:00Z"),
        (VALID_GOAL_RECORD, "not-a-timestamp"),
        (VALID_GOAL_RECORD, None),
    ],
)
def test_invalid_input_or_created_at_raises_goal_template_error(
    goal_record: dict[str, object], created_at: object
) -> None:
    with pytest.raises(GoalTemplateError):
        build_generic_algorithm_goal_contract(goal_record, created_at=created_at)

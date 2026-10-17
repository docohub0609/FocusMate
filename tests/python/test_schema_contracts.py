from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "schemas"


def load_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    for path in SCHEMA_ROOT.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[str(schema["$id"])] = schema
    return schemas


SCHEMAS = load_schemas()
REGISTRY = Registry().with_resources(
    (schema_id, Resource.from_contents(schema))
    for schema_id, schema in SCHEMAS.items()
)
OBJECT_SCHEMAS = {
    str(schema["title"]): schema
    for schema in SCHEMAS.values()
    if schema.get("examples") and schema.get("title") != "GoalContext"
}


GENERIC_SUBGOAL_LITERALS = [
    {
        "subgoalId": "understand-problem",
        "stage": "problemUnderstanding",
        "objective": "Understand the problem requirements.",
        "dependsOn": [],
        "acceptableEvidence": ["confirmedProblemFeature", "evaluatedUserExplanation"],
    },
    {
        "subgoalId": "choose-strategy",
        "stage": "feasibleStrategy",
        "objective": "Choose a strategy that fits the constraints.",
        "dependsOn": [],
        "acceptableEvidence": ["complexityCheck", "evaluatedUserExplanation", "codeStructureCheck"],
    },
    {
        "subgoalId": "justify-state",
        "stage": "coreStateAndCorrectness",
        "objective": "Explain the maintained state and correctness.",
        "dependsOn": [],
        "acceptableEvidence": ["evaluatedUserExplanation", "codeStructureCheck", "targetedRunResult"],
    },
    {
        "subgoalId": "implement-core",
        "stage": "coreImplementation",
        "objective": "Implement the core strategy.",
        "dependsOn": [],
        "acceptableEvidence": ["codeStructureCheck", "compileResult", "runResult"],
    },
    {
        "subgoalId": "test-boundaries",
        "stage": "boundaryAndTesting",
        "objective": "Test boundaries and representative inputs.",
        "dependsOn": [],
        "acceptableEvidence": ["publicSampleRunResult", "targetedRunResult"],
    },
]

ADAPTIVE_POLICY_LITERAL = {
    "mode": "adaptive",
    "stageOrder": "defaultOnly",
    "allowSkip": True,
    "allowRevisit": True,
    "offRouteHandling": "temporaryBranch",
    "stalledRouteFallback": "latestVerifiedSubgoal",
    "processStagesRequiredForCompletion": False,
}


def validate(title: str, instance: object) -> None:
    schema = OBJECT_SCHEMAS[title]
    validator_class = validator_for(schema)
    validator_class(
        schema,
        registry=REGISTRY,
        format_checker=FormatChecker(),
    ).validate(instance)


def valid_example(title: str) -> dict[str, object]:
    return deepcopy(OBJECT_SCHEMAS[title]["examples"][0])  # type: ignore[index]


def test_goal_contract_accepts_the_adaptive_generic_map() -> None:
    validate("GoalContract", adaptive_goal_contract())


def adaptive_goal_contract() -> dict[str, object]:
    return {
        "goalId": "goal-001",
        "schemaVersion": "1.0.0",
        "version": 1,
        "createdAt": "2026-09-11T10:00:00+08:00",
        "problemRef": {"problemId": "problem-001", "problemVersion": 1},
        "objective": "Produce a Python solution accepted by the trusted OJ.",
        "subgoals": deepcopy(GENERIC_SUBGOAL_LITERALS),
        "navigationPolicy": deepcopy(ADAPTIVE_POLICY_LITERAL),
        "finalCriterion": {"verifierType": "finalOj", "requiredVerdict": "AC"},
        "coachPolicy": {"completeSolutionAllowed": False},
        "contractStatus": "draft",
    }


def test_goal_contract_rejects_missing_or_unknown_stage() -> None:
    for mutation in ("missing", "unknown"):
        instance = adaptive_goal_contract()
        subgoals = instance["subgoals"]
        assert isinstance(subgoals, list)
        assert isinstance(subgoals[0], dict)
        if mutation == "missing":
            del subgoals[0]["stage"]
        else:
            subgoals[0]["stage"] = "magicAlgorithm"
        with pytest.raises(ValidationError):
            validate("GoalContract", instance)


def test_goal_contract_rejects_empty_unknown_or_duplicate_evidence() -> None:
    for evidence in ([], ["modelGuess"], ["runResult", "runResult"]):
        instance = adaptive_goal_contract()
        subgoals = instance["subgoals"]
        assert isinstance(subgoals, list)
        assert isinstance(subgoals[0], dict)
        subgoals[0]["acceptableEvidence"] = evidence
        with pytest.raises(ValidationError):
            validate("GoalContract", instance)


def test_goal_contract_requires_the_fixed_navigation_policy() -> None:
    missing = adaptive_goal_contract()
    del missing["navigationPolicy"]
    with pytest.raises(ValidationError):
        validate("GoalContract", missing)

    blocking = adaptive_goal_contract()
    navigation_policy = blocking["navigationPolicy"]
    assert isinstance(navigation_policy, dict)
    navigation_policy["processStagesRequiredForCompletion"] = True
    with pytest.raises(ValidationError):
        validate("GoalContract", blocking)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("mode", "sequential"),
        ("stageOrder", "required"),
        ("allowSkip", False),
        ("allowRevisit", False),
        ("offRouteHandling", "reject"),
        ("stalledRouteFallback", "restartGoal"),
    ],
)
def test_goal_contract_rejects_each_non_fixed_navigation_policy_value(
    field: str, invalid_value: object
) -> None:
    instance = adaptive_goal_contract()
    navigation_policy = instance["navigationPolicy"]
    assert isinstance(navigation_policy, dict)
    navigation_policy[field] = invalid_value
    with pytest.raises(ValidationError):
        validate("GoalContract", instance)


@pytest.mark.parametrize(
    "field",
    [
        "mode",
        "stageOrder",
        "allowSkip",
        "allowRevisit",
        "offRouteHandling",
        "stalledRouteFallback",
        "processStagesRequiredForCompletion",
    ],
)
def test_goal_contract_rejects_each_missing_navigation_policy_field(field: str) -> None:
    instance = adaptive_goal_contract()
    navigation_policy = instance["navigationPolicy"]
    assert isinstance(navigation_policy, dict)
    del navigation_policy[field]
    with pytest.raises(ValidationError):
        validate("GoalContract", instance)


def test_goal_contract_rejects_missing_acceptable_evidence() -> None:
    instance = adaptive_goal_contract()
    subgoals = instance["subgoals"]
    assert isinstance(subgoals, list)
    assert isinstance(subgoals[0], dict)
    del subgoals[0]["acceptableEvidence"]
    with pytest.raises(ValidationError):
        validate("GoalContract", instance)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("verifierType", "process"), ("requiredVerdict", "WA")],
)
def test_goal_contract_rejects_non_final_oj_ac_criterion(field: str, invalid_value: str) -> None:
    instance = adaptive_goal_contract()
    final_criterion = instance["finalCriterion"]
    assert isinstance(final_criterion, dict)
    final_criterion[field] = invalid_value
    with pytest.raises(ValidationError):
        validate("GoalContract", instance)


@pytest.mark.parametrize("field", ["verifierType", "requiredVerdict"])
def test_goal_contract_rejects_missing_final_criterion_field(field: str) -> None:
    instance = adaptive_goal_contract()
    final_criterion = instance["finalCriterion"]
    assert isinstance(final_criterion, dict)
    del final_criterion[field]
    with pytest.raises(ValidationError):
        validate("GoalContract", instance)


def test_all_schema_documents_are_valid_draft_2020_12() -> None:
    assert len(SCHEMAS) == 98
    for schema in SCHEMAS.values():
        validator_for(schema).check_schema(schema)


@pytest.mark.parametrize("title", sorted(OBJECT_SCHEMAS))
def test_each_core_object_has_a_valid_example(title: str) -> None:
    validate(title, valid_example(title))


@pytest.mark.parametrize(
    "title",
    [
        "RawEvent",
        "CodeSnapshot",
        "Observation",
        "LearningAction",
        "SkillInvocation",
        "Submission",
        "OJResult",
        "VerifierResult",
        "DecisionTrace",
        "SystemError",
    ],
)
def test_process_objects_reject_missing_goal_context(title: str) -> None:
    instance = valid_example(title)
    del instance["goalContext"]
    with pytest.raises(ValidationError):
        validate(title, instance)


def test_goal_context_enforces_scope_and_subgoal_consistency() -> None:
    observation = valid_example("Observation")
    del observation["goalContext"]["subgoalId"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("Observation", observation)

    submission = valid_example("Submission")
    submission["goalContext"]["subgoalId"] = "unexpected"  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("Submission", submission)


@pytest.mark.parametrize("title", sorted(OBJECT_SCHEMAS))
def test_core_objects_reject_unknown_schema_version(title: str) -> None:
    instance = valid_example(title)
    instance["schemaVersion"] = "9.9.9"
    with pytest.raises(ValidationError):
        validate(title, instance)


def test_unknown_top_level_fields_are_rejected() -> None:
    submission = valid_example("Submission")
    submission["forceGoalCompleted"] = True
    with pytest.raises(ValidationError):
        validate("Submission", submission)


def test_problem_features_remain_backward_compatible_and_accept_review_metadata() -> None:
    legacy = valid_example("ProblemFeatures")
    validate("ProblemFeatures", legacy)

    reviewed = deepcopy(legacy)
    reviewed["status"] = "reviewRequired"
    reviewed["provenance"] = {"type": "llmGenerated"}
    reviewed["features"][0]["review"] = {  # type: ignore[index]
        "status": "required",
        "issue": "locallyUnverifiable",
        "message": "The local verifier cannot normalize the cited expression.",
        "allowedActions": ["editFeature", "confirmFeature", "editProblem"],
    }
    reviewed["features"][0]["evidence"][0]["evidenceId"] = "constraints:0"  # type: ignore[index]
    validate("ProblemFeatures", reviewed)


def test_human_created_problem_features_do_not_require_an_extractor() -> None:
    instance = valid_example("ProblemFeatures")
    instance.pop("extractor")
    instance["provenance"] = {
        "type": "humanCreated",
        "confirmedAt": "2026-08-27T10:00:00Z",
    }
    validate("ProblemFeatures", instance)

    instance["extractor"] = valid_example("ProblemFeatures")["extractor"]
    with pytest.raises(ValidationError):
        validate("ProblemFeatures", instance)


def test_human_edited_provenance_requires_its_base_report() -> None:
    instance = valid_example("ProblemFeatures")
    instance["provenance"] = {
        "type": "humanEdited",
        "confirmedAt": "2026-08-27T10:00:00Z",
    }
    with pytest.raises(ValidationError):
        validate("ProblemFeatures", instance)

    instance["provenance"]["basedOnFeatureSetId"] = "features-ai-001"  # type: ignore[index]
    validate("ProblemFeatures", instance)


def test_feature_confirmation_request_supports_ai_based_and_manual_paths() -> None:
    ai_based = valid_example("ProblemFeatureConfirmationRequest")
    validate("ProblemFeatureConfirmationRequest", ai_based)

    manual = deepcopy(ai_based)
    manual.pop("basedOnFeatureSetId")
    validate("ProblemFeatureConfirmationRequest", manual)


@pytest.mark.parametrize("missing_category", [
    "dataScale", "inputStructure", "outputStructure", "resourceConstraint", "domainTag"
])
def test_feature_confirmation_request_requires_all_five_categories(missing_category: str) -> None:
    instance = valid_example("ProblemFeatureConfirmationRequest")
    instance["features"] = [  # type: ignore[index]
        feature for feature in instance["features"]  # type: ignore[index]
        if feature["category"] != missing_category
    ]
    with pytest.raises(ValidationError):
        validate("ProblemFeatureConfirmationRequest", instance)


def test_feature_confirmation_request_rejects_conflict_and_invalid_evidence_rules() -> None:
    instance = valid_example("ProblemFeatureConfirmationRequest")
    instance["features"][0]["status"] = "conflict"  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("ProblemFeatureConfirmationRequest", instance)

    instance = valid_example("ProblemFeatureConfirmationRequest")
    instance["features"][1]["evidenceRefs"] = []  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("ProblemFeatureConfirmationRequest", instance)

    instance = valid_example("ProblemFeatureConfirmationRequest")
    instance["features"][0]["evidenceRefs"] = ["constraints:0"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("ProblemFeatureConfirmationRequest", instance)


@pytest.mark.parametrize("title", ["GoalContract", "LearningAction", "SkillDescriptor"])
def test_coach_mode_cannot_allow_complete_solutions(title: str) -> None:
    instance = valid_example(title)
    instance["coachPolicy"]["completeSolutionAllowed"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate(title, instance)


def test_final_verifier_requires_an_oj_result_reference() -> None:
    result = valid_example("VerifierResult")
    del result["ojResultId"]
    with pytest.raises(ValidationError):
        validate("VerifierResult", result)


def test_verified_and_completed_goal_states_require_verifier_references() -> None:
    state = valid_example("GoalState")
    state["subgoalStates"][0]["status"] = "verified"  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("GoalState", state)

    state = valid_example("GoalState")
    state["status"] = "completed"
    state.pop("currentSubgoalId")
    with pytest.raises(ValidationError):
        validate("GoalState", state)


def test_completed_goal_state_allows_unverified_process_stages_with_final_verifier() -> None:
    state = valid_example("GoalState")
    state["status"] = "completed"
    state.pop("currentSubgoalId")
    state["lastVerifierResultId"] = "verifier-final-001"
    state["subgoalStates"] = [
        {"subgoalId": subgoal_id, "status": "notStarted"}
        for subgoal_id in [
            "understand-problem",
            "choose-strategy",
            "justify-state",
            "implement-core",
            "test-boundaries",
        ]
    ]
    validate("GoalState", state)


def test_skill_result_cannot_request_a_goal_state_update() -> None:
    invocation = valid_example("SkillInvocation")
    invocation["result"]["goalStateUpdate"] = {"status": "completed"}  # type: ignore[index]
    with pytest.raises(ValidationError):
        validate("SkillInvocation", invocation)


def raw_event(event_name: str, *, scope: str = "subgoal") -> dict[str, object]:
    event = valid_example("RawEvent")
    event["eventName"] = event_name
    if scope == "goal":
        event["goalContext"] = {"goalId": "goal-001", "scope": "goal"}
    return event


@pytest.mark.parametrize(
    ("event_name", "scope", "payload", "source"),
    [
        ("codeSnapshotCreated", "subgoal", {"codeVersion": 2, "artifactRef": "code-002", "draftState": "draft"}, "web"),
        ("runRequested", "subgoal", {"codeVersion": 2, "runMode": "publicExample"}, "web"),
        ("submissionRequested", "goal", {"codeVersion": 2, "problemRef": {"problemId": "problem-001", "problemVersion": 1}}, "web"),
        ("ojResultProduced", "goal", {"submissionId": "submission-001", "ojResultId": "oj-result-001", "verdict": "WA"}, "ojWorker"),
        ("helpRequested", "subgoal", {"requestType": "stuck"}, "web"),
        ("actionFeedbackProvided", "subgoal", {"actionId": "action-001", "feedback": "notHelpful"}, "web"),
        ("sessionPaused", "goal", {"reason": "userRequested"}, "web"),
        ("sessionResumed", "goal", {"previousPauseEventId": "event-pause-001"}, "web"),
    ],
)
def test_each_supported_event_has_a_valid_contract(
    event_name: str, scope: str, payload: dict[str, object], source: str
) -> None:
    event = raw_event(event_name, scope=scope)
    event["payload"] = payload
    event["source"] = source
    validate("RawEvent", event)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("unknownName", "madeUpEvent"),
        ("wrongSource", "ojWorker"),
        ("wrongPayload", {}),
        ("wrongPrivacy", "public"),
    ],
)
def test_events_reject_unknown_names_and_wrong_contracts(mutation: str, value: object) -> None:
    event = valid_example("RawEvent")
    if mutation == "unknownName":
        event["eventName"] = value
    elif mutation == "wrongSource":
        event["source"] = value
    elif mutation == "wrongPayload":
        event["payload"] = value
    else:
        event["privacyLevel"] = value
    with pytest.raises(ValidationError):
        validate("RawEvent", event)


def test_custom_run_requires_an_input_reference() -> None:
    event = raw_event("runRequested")
    event["payload"] = {"codeVersion": 2, "runMode": "customInput"}
    with pytest.raises(ValidationError):
        validate("RawEvent", event)


def test_event_scope_must_match_event_semantics() -> None:
    event = raw_event("submissionRequested")
    event["payload"] = {
        "codeVersion": 2,
        "problemRef": {"problemId": "problem-001", "problemVersion": 1},
    }
    with pytest.raises(ValidationError):
        validate("RawEvent", event)


@pytest.mark.parametrize(
    ("category", "component", "code", "retryability"),
    [
        ("validation", "api", "EVENT_CONTEXT_INVALID", "nonRetryable"),
        ("sandbox", "ojWorker", "SANDBOX_START_FAILED", "retryable"),
        ("sandbox", "ojWorker", "SANDBOX_POLICY_VIOLATION", "nonRetryable"),
        ("oj", "api", "OJ_WORKER_UNAVAILABLE", "retryable"),
        ("oj", "ojWorker", "OJ_RESULT_INVALID", "nonRetryable"),
        ("llm", "core", "LLM_OUTPUT_INVALID", "retryable"),
        ("skill", "skillExecutor", "SKILL_OUTPUT_REJECTED", "nonRetryable"),
        ("verifier", "processVerifier", "VERIFIER_EXECUTION_FAILED", "retryable"),
        ("verifier", "finalOjVerifier", "VERIFIER_INPUT_INVALID", "nonRetryable"),
    ],
)
def test_system_error_mapping(
    category: str, component: str, code: str, retryability: str
) -> None:
    error = valid_example("SystemError")
    error.update({
        "category": category,
        "component": component,
        "code": code,
        "retryability": retryability,
    })
    validate("SystemError", error)


def test_system_errors_reject_wrong_retryability_and_verifier_statuses() -> None:
    error = valid_example("SystemError")
    error["retryability"] = "retryable"
    with pytest.raises(ValidationError):
        validate("SystemError", error)

    error = valid_example("SystemError")
    error["retryability"] = "inconclusive"
    with pytest.raises(ValidationError):
        validate("SystemError", error)


@pytest.mark.parametrize("verdict", ["WA", "CE"])
def test_normal_oj_verdicts_remain_results_not_system_errors(verdict: str) -> None:
    result = valid_example("OJResult")
    result["verdict"] = verdict
    validate("OJResult", result)

    error = valid_example("SystemError")
    error["code"] = verdict
    with pytest.raises(ValidationError):
        validate("SystemError", error)


def test_inconclusive_is_a_valid_verifier_result_not_an_error() -> None:
    result = valid_example("VerifierResult")
    result["verifierType"] = "process"
    result["status"] = "inconclusive"
    result.pop("ojResultId")
    validate("VerifierResult", result)

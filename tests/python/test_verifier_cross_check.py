from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from apps.api.schema_validation import validate
from jsonschema.exceptions import ValidationError
from services.oj_worker.docker_runner import PythonRunResult, ResourceSummary
from services.verifier_builder.candidates import CandidateSet, CoverageClaim, ProgramCandidate, MutantCandidate
from services.verifier_builder.cross_check import (
    CrossCheckFailure,
    VerifierCrossChecker,
    _generator_control,
)
from tests.python.test_submission_storage import frozen_problem


SUMMARY = ResourceSummary(1, 256 * 1024 * 1024, 0, 1, 2, 32, 16 * 1024 * 1024)


def result(stdout=b"", reason="exited", exit_code=0):
    return PythonRunResult(stdout, b"SECRET-stderr", exit_code, reason, False, False, False, SUMMARY)


@pytest.fixture
def setup(tmp_path):
    repository, record = frozen_problem(tmp_path / "problem.db")
    features = repository.get_feature_set(record["lifecycle"]["confirmedFeatureSetId"])
    def program(name):
        return ProgramCandidate("# SECRET-" + name, "deepseek", "fake", "response-1", "prompt-v1")
    candidates = CandidateSet(
        record["problemPackage"]["problemId"], 1, record["lifecycle"]["contentHash"],
        features["featureSetId"], tuple(f["featureKey"] for f in features["features"] if f["status"] == "confirmed"),
        program("generator"), (CoverageClaim("randomSmall", ("dataScale.n",)),),
        program("slow"), program("fast"), (MutantCandidate("off-one", "offByOne", "# SECRET-mutant"),),
    )
    return record, features, candidates


def cases(large=False):
    items = [{"caseKey": f"small-{n}", "scale": "small", "tags": ["minimumSize" if n == 100 else "randomSmall"], "stdin": f"{n}\n"} for n in range(100, 108)]
    if large:
        items.append({"caseKey": "large-1", "scale": "large", "tags": ["maximumSize"], "stdin": "99999\n"})
    return items


class FakeRunner:
    def __init__(self, record, generated=None, change=None):
        self.generated = json.dumps({"cases": cases(True)}).encode() if generated is None else generated
        self.public = {x["input"].encode(): x["output"].encode() for x in record["problemPackage"]["examples"]}
        self.calls = []
        self.change = change

    def run(self, request):
        source = (request.workspace / "main.py").read_text(encoding="utf-8")
        role = source.removeprefix("# SECRET-")
        self.calls.append((role, request))
        if self.change:
            changed = self.change(role, request, self)
            if changed is not None:
                return changed
        if request.operation == "compile":
            return result()
        if role == "generator":
            return result(self.generated)
        if request.stdin in self.public:
            return result(self.public[request.stdin])
        return result(b"wrong" if role.startswith("mutant") else request.stdin)


def run(tmp_path, setup, runner=None, **kwargs):
    runner = runner or FakeRunner(setup[0])
    checker = VerifierCrossChecker(runner, tmp_path / "work", monotonic=lambda: 10.0, **kwargs)
    return checker.precheck(*setup, 100), runner


def test_runner_exception_retains_private_diagnostic_without_hidden_text(tmp_path, setup):
    def fail(role, request, runner):
        raise RuntimeError("SECRET-candidate input and host path")
    checker = VerifierCrossChecker(FakeRunner(setup[0], change=fail), tmp_path / "work")
    trace = []
    checker.trace = lambda **entry: trace.append(entry)
    with pytest.raises(CrossCheckFailure) as caught:
        checker.precheck(*setup, checker.monotonic() + 60)
    assert caught.value.code == "VERIFIER_BUILDER_INTERNAL_ERROR"
    assert any(entry.get("event") == "sandboxFailed" and entry.get("stage") == "compile"
               and entry.get("operation") == "compile" and entry.get("errorType") == "RuntimeError"
               for entry in trace)
    location = trace[-1]["locations"][-1]
    assert location["file"] == "test_verifier_cross_check.py" and location["function"] == "fail"
    assert location["line"] > 0 and set(location) == {"file", "function", "line"}
    assert "SECRET" not in json.dumps(trace) + str(caught.value)


def test_returned_infrastructure_failure_retains_only_safe_runner_fields(tmp_path, setup):
    def fail(role, request, runner):
        return replace(result(reason="infrastructureError", exit_code=None), failure={
            "stage": "attach", "errorType": "ReadTimeout", "elapsedMs": 84, "retryCount": 1,
            "message": "SECRET-hidden input", "stdout": "SECRET-output",
        })
    checker = VerifierCrossChecker(FakeRunner(setup[0], change=fail), tmp_path / "work")
    trace = []
    checker.trace = lambda **entry: trace.append(entry)
    with pytest.raises(CrossCheckFailure) as caught:
        checker.precheck(*setup, checker.monotonic() + 60)
    assert caught.value.code == "VERIFIER_BUILDER_INTERNAL_ERROR"
    sandbox = next(entry for entry in trace if entry["event"] == "sandbox")
    assert sandbox["runnerFailure"] == {
        "stage": "attach", "errorType": "ReadTimeout", "elapsedMs": 84, "retryCount": 1,
    }
    assert "SECRET" not in json.dumps(trace) + str(caught.value)


def test_precheck_filesystem_failure_retains_stage_without_path(tmp_path, setup, monkeypatch):
    checker = VerifierCrossChecker(FakeRunner(setup[0]), tmp_path / "work")
    checker.stage = "mutation"  # The same worker may previously have handled another job.
    trace = []
    checker.trace = lambda **entry: trace.append(entry)
    def fail(*args, **kwargs):
        raise PermissionError("SECRET-host path")
    monkeypatch.setattr(type(tmp_path), "mkdir", fail)
    with pytest.raises(CrossCheckFailure) as caught:
        checker.precheck(*setup, checker.monotonic() + 60)
    assert caught.value.code == "VERIFIER_BUILDER_INTERNAL_ERROR"
    assert any(entry.get("event") == "precheckFailed" and entry.get("stage") == "preparing"
               and entry.get("errorType") == "PermissionError" for entry in trace)
    assert trace[-1]["locations"][-1]["function"] == "fail"
    assert "SECRET" not in json.dumps(trace) + str(caught.value)


def test_success_has_immutable_material_correct_counts_isolation_and_cleanup(tmp_path, setup):
    material, runner = run(tmp_path, setup)
    assert material.small_count == material.agreement_count == 8
    assert material.large_count == 1
    assert (material.mutation_attempted, material.mutation_eligible, material.mutation_killed) == (1, 1, 1)
    assert material.cases[0].stdin == material.cases[0].expected_output == b"100\n"
    assert all(req.stdin != b"99999\n" for role, req in runner.calls if role == "slow")
    generator_runs = [req for role, req in runner.calls if role == "generator" and req.operation == "execute"]
    assert len(generator_runs) == 2 and generator_runs[0].stdin == generator_runs[1].stdin
    assert json.loads(generator_runs[0].stdin) == {"schemaVersion": "1.0.0", "seed": 123, "smallTarget": 24, "largeTarget": 4}
    assert len({req.workspace for _, req in runner.calls}) == 4
    assert list((tmp_path / "work").iterdir()) == []
    assert "SECRET" not in repr(material) and "100\\n" not in repr(material)
    with pytest.raises(FrozenInstanceError):
        material.cases[0].stdin = b"changed"


def test_generator_control_disables_large_cases_without_confirmed_large_input_size():
    small_context = {
        "confirmedFeatures": [{
            "featureKey": "dataScale.n",
            "category": "dataScale",
            "status": "confirmed",
            "value": {"variable": "n", "minimum": 1, "maximum": 8},
        }],
    }

    assert _generator_control(small_context, 123) == {
        "schemaVersion": "1.0.0",
        "seed": 123,
        "smallTarget": 24,
        "largeTarget": 0,
    }


@pytest.mark.parametrize("bad", ["json", "duplicateJson", "nan", "utf8", "surrogate", "duplicateKey", "duplicateInput", "few", "noRandom", "noBoundary", "badTag", "extra", "largeMissing", "tooMany", "oversized", "nonDeterministic"])
def test_bad_generator_is_rejected_and_never_reaches_references(tmp_path, setup, bad):
    document = {"cases": cases(True)}
    if bad == "duplicateKey": document["cases"][1]["caseKey"] = "small-100"
    if bad == "duplicateInput": document["cases"][1]["stdin"] = "100\n"
    if bad == "few": document["cases"].pop(2)
    if bad == "noRandom":
        for item in document["cases"]: item["tags"] = ["minimumSize"]
    if bad == "noBoundary":
        for item in document["cases"]: item["tags"] = ["randomSmall"]
    if bad == "badTag": document["cases"][0]["tags"] = ["SECRET-unknown"]
    if bad == "extra": document["cases"][0]["answer"] = "SECRET"
    if bad == "largeMissing": document["cases"].pop()
    if bad == "tooMany": document["cases"] *= 8
    if bad == "surrogate": document["cases"][0]["stdin"] = "\ud800"
    generated = {"json": b"SECRET-invalid", "duplicateJson": b'{"cases":[],"cases":[]}', "nan": b'{"cases":NaN}', "utf8": b"\xff", "oversized": b"x" * (1024 * 1024 + 1)}.get(bad, json.dumps(document).encode())
    def change(role, request, runner):
        if bad == "nonDeterministic" and role == "generator" and request.operation == "execute" and sum(r == "generator" and q.operation == "execute" for r, q in runner.calls) == 2:
            changed = deepcopy(document)
            changed["cases"][0]["stdin"] += " "
            return result(json.dumps(changed).encode())
    runner = FakeRunner(setup[0], generated, change)
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, setup, runner)
    expected = "GENERATOR_COVERAGE_INSUFFICIENT" if bad in {"duplicateInput", "few", "noRandom", "noBoundary", "largeMissing"} else "GENERATOR_NON_DETERMINISTIC" if bad == "nonDeterministic" else "GENERATOR_CASES_INVALID"
    assert caught.value.code == expected
    assert not any(role == "slow" and req.operation == "execute" for role, req in runner.calls)
    assert "SECRET" not in str(caught.value) and "SECRET" not in repr(caught.value)
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize("failure,code", [("compile", "CANDIDATE_COMPILE_FAILED"), ("public", "REFERENCE_PUBLIC_MISMATCH"), ("small", "REFERENCE_DISAGREEMENT"), ("large", "REFERENCE_DISAGREEMENT"), ("mutantSurvives", "MUTATION_EVIDENCE_INSUFFICIENT"), ("mutantCompile", "MUTATION_EVIDENCE_INSUFFICIENT"), ("mutantPublic", "MUTATION_EVIDENCE_INSUFFICIENT")])
def test_reference_and_mutation_gates(tmp_path, setup, failure, code):
    def change(role, req, runner):
        if failure == "compile" and role == "slow" and req.operation == "compile": return result(exit_code=1)
        if failure == "public" and role == "slow" and req.stdin in runner.public: return result(b"SECRET-wrong")
        if failure == "small" and role == "slow" and req.stdin == b"100\n": return result(b"SECRET-wrong")
        if failure == "large" and role == "fast" and req.stdin == b"99999\n" and sum(q.stdin == b"99999\n" for _, q in runner.calls) == 2: return result(b"SECRET-wrong")
        if failure == "mutantSurvives" and role == "mutant" and req.operation == "execute": return result(runner.public.get(req.stdin, req.stdin))
        if failure == "mutantCompile" and role == "mutant" and req.operation == "compile": return result(exit_code=1)
        if failure == "mutantPublic" and role == "mutant" and req.stdin in runner.public: return result(b"SECRET-wrong")
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, setup, FakeRunner(setup[0], change=change))
    assert caught.value.code == code and caught.value.outcome == "failed"
    assert "SECRET" not in repr(caught.value)
    assert list((tmp_path / "work").iterdir()) == []


def test_every_eligible_mutant_must_be_killed(tmp_path, setup):
    record, features, candidates = setup
    candidates = replace(candidates, mutants=candidates.mutants + (MutantCandidate("survivor", "offByOne", "# SECRET-survivor"),))
    with pytest.raises(CrossCheckFailure, match="fault-detection"):
        run(tmp_path, (record, features, candidates))


@pytest.mark.parametrize("failure", ["infrastructure", "raised", "budget"])
def test_infrastructure_and_absolute_budget_are_inconclusive(tmp_path, setup, failure):
    clock = [10.0]
    def change(role, req, runner):
        if failure == "raised": raise RuntimeError("SECRET-host-path")
        if failure == "infrastructure": return result(reason="infrastructureError", exit_code=None)
        clock[0] = 100.0
    runner = FakeRunner(setup[0], change=change)
    checker = VerifierCrossChecker(runner, tmp_path / "work", monotonic=lambda: clock[0])
    with pytest.raises(CrossCheckFailure) as caught:
        checker.precheck(*setup, 100)
    assert caught.value.outcome == "inconclusive"
    assert len(runner.calls) == 1
    assert "SECRET" not in str(caught.value)
    assert list((tmp_path / "work").iterdir()) == []


def test_safe_summary_fields_and_existing_comparator(tmp_path, setup):
    def change(role, req, runner):
        if role in {"slow", "fast"} and req.stdin in runner.public:
            return result(runner.public[req.stdin].rstrip(b"\n") + b"\r\n")
    material, _ = run(tmp_path, setup, FakeRunner(setup[0], change=change))
    summary = material.safe_failure_summaries[0].to_dict()
    assert set(summary) == {"caseHash", "scale", "tags", "inputByteCount", "inputLineCount", "inputTokenCount", "failureKind"}
    assert summary["inputByteCount"] == 4 and summary["inputLineCount"] == summary["inputTokenCount"] == 1


def test_wrong_candidate_context_never_runs(tmp_path, setup):
    record, features, candidates = setup
    runner = FakeRunner(record)
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, (record, features, replace(candidates, problem_version=2)), runner)
    assert caught.value.code == "CANDIDATE_SCHEMA_INVALID" and runner.calls == []


def test_cross_check_schema_disallows_private_failure_fields():
    from pathlib import Path
    document = json.loads(Path("schemas/execution/verifier-cross-check-run.schema.json").read_text())
    sample = deepcopy(document["examples"][0])
    validate("VerifierCrossCheckRun", sample)
    sample["safeFailureSummaries"][0]["stdin"] = "secret"
    with pytest.raises(ValidationError):
        validate("VerifierCrossCheckRun", sample)


def test_budget_is_checked_at_every_external_call(tmp_path, setup):
    _, complete = run(tmp_path / "complete", setup)
    for stop_after in range(1, len(complete.calls) + 1):
        clock = [10.0]
        def change(role, request, runner):
            if len(runner.calls) == stop_after:
                clock[0] = 100.0
        runner = FakeRunner(setup[0], change=change)
        root = tmp_path / str(stop_after)
        checker = VerifierCrossChecker(runner, root, monotonic=lambda: clock[0])
        with pytest.raises(CrossCheckFailure) as caught:
            checker.precheck(*setup, 100)
        assert caught.value.code == "BUILD_BUDGET_EXCEEDED"
        assert len(runner.calls) == stop_after and list(root.iterdir()) == []


def test_request_wall_time_is_clamped_to_remaining_absolute_budget(tmp_path, setup):
    runner = FakeRunner(setup[0])
    VerifierCrossChecker(runner, tmp_path / "work", monotonic=lambda: 98.5).precheck(*setup, 100)
    assert all(req.policy.wall_time_seconds == 1.5 for _, req in runner.calls)


@pytest.mark.parametrize("maximum,large_allowed", [(8, False), (64, False), (65, True)])
def test_only_meaningful_confirmed_numeric_scale_requires_large(tmp_path, setup, maximum, large_allowed):
    record, features, candidates = setup
    features = deepcopy(features)
    features["features"][0]["value"]["maximum"] = maximum
    runner = FakeRunner(record, json.dumps({"cases": cases(large_allowed)}).encode())
    material, _ = run(tmp_path, (record, features, candidates), runner)
    assert material.large_count == int(large_allowed)


@pytest.mark.parametrize("reason", ["wallTimeExceeded", "outputLimitExceeded", "memoryLimitExceeded", "exited"])
def test_reference_execution_failure_is_not_a_pass(tmp_path, setup, reason):
    def change(role, req, runner):
        if role == "fast" and req.stdin == b"100\n":
            return result(reason=reason, exit_code=1)
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, setup, FakeRunner(setup[0], change=change))
    assert caught.value.code == "REFERENCE_EXECUTION_FAILED" and caught.value.outcome == "failed"
    assert caught.value.summaries[0].failure_kind == "candidateExecutionFailed"


def test_output_comparison_does_not_strip_meaningful_whitespace(tmp_path, setup):
    def change(role, req, runner):
        if role == "slow" and req.stdin == b"100\n": return result(b"100 \n")
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, setup, FakeRunner(setup[0], change=change))
    assert caught.value.code == "REFERENCE_DISAGREEMENT"


def test_oracle_sealing_evaluator_shares_absolute_deadline(tmp_path):
    from apps.api.trusted_assets import OracleEvaluationFailure
    from services.verifier_builder.docker_oracle import DockerOracleEvaluator
    clock = [10.0]
    class Runner:
        calls = []
        def run(self, request):
            self.calls.append(request)
            clock[0] = 100.0
            return result()
    runner = Runner()
    with pytest.raises(OracleEvaluationFailure) as caught:
        DockerOracleEvaluator(runner, tmp_path / "work", deadline=100, monotonic=lambda: clock[0]).evaluate("print(1)", (b"SECRET",))
    assert caught.value.code == "ORACLE_BUILD_BUDGET_EXCEEDED"
    assert len(runner.calls) == 1 and list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize("tags", [["randomSmall", "duplicates"], ["randomSmall", "minimumSize"]])
def test_random_cases_cannot_also_claim_nonrandom_boundary_coverage(tmp_path, setup, tags):
    generated = cases(True)
    for item in generated:
        if item["scale"] == "small":
            item["tags"] = tags
    runner = FakeRunner(setup[0], json.dumps({"cases": generated}).encode())
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, setup, runner)
    assert caught.value.code == "GENERATOR_COVERAGE_INSUFFICIENT"


def test_separate_random_and_explicit_nonrandom_boundary_satisfy_coverage(tmp_path, setup):
    generated = cases(True)
    generated[0]["tags"] = ["boundary", "duplicates"]
    material, _ = run(tmp_path, setup, FakeRunner(setup[0], json.dumps({"cases": generated}).encode()))
    assert material.small_count == 8


def test_one_random_minimum_case_cannot_supply_both_required_roles(tmp_path, setup):
    generated = cases(True)
    generated[0]["tags"] = ["randomSmall", "minimumSize"]
    with pytest.raises(CrossCheckFailure) as caught:
        run(tmp_path, setup, FakeRunner(setup[0], json.dumps({"cases": generated}).encode()))
    assert caught.value.code == "GENERATOR_COVERAGE_INSUFFICIENT"


@pytest.mark.parametrize("status,review", [("hypothesis", False), ("unknown", False), ("confirmed", True)])
def test_large_applicability_does_not_use_uncertain_or_review_pending_features(status, review):
    from services.verifier_builder.cross_check import _large_applicable
    feature = {"featureKey": "dataScale.inputSize", "category": "dataScale", "status": status, "value": {"nMax": 100000}}
    if review:
        feature["review"] = {}
    assert _large_applicable({"confirmedFeatures": [feature]}) is False


@pytest.mark.parametrize("feature_key,value,large_allowed", [
    ("dataScale.valueRange", {"maxValue": 1000000}, False),
    ("dataScale.weightRange", {"maximum": 1000000}, False),
    ("dataScale.elementRange", {"variable": "n", "maximum": 1000000}, False),
    ("dataScale.inputSize", {"nMax": 100000}, True),
    ("dataScale.inputSize", {"nMax": 64}, False),
    ("dataScale.inputSize", {"nMax": 65}, True),
    ("dataScale.inputSize", {"length": 100000}, True),
    ("dataScale.inputSize", {"length": 64}, False),
    ("dataScale.inputSize", {"size": 100000}, True),
    ("dataScale.inputSize", {"size": 64}, False),
    ("dataScale.inputSize", {"count": 100000}, True),
    ("dataScale.inputSize", {"count": 64}, False),
    ("dataScale.sequenceLength", {"maximum": 100000}, True),
    ("dataScale.unrecognized", {"maximum": 100000}, False),
])
def test_large_cases_follow_input_cardinality_not_scalar_value_limits(tmp_path, setup, feature_key, value, large_allowed):
    record, features, candidates = setup
    features = deepcopy(features)
    features["features"][0]["featureKey"] = feature_key
    features["features"][0]["value"] = value
    candidates = replace(candidates,
        confirmed_feature_keys=tuple(feature_key if key == "dataScale.n" else key for key in candidates.confirmed_feature_keys),
        coverage_plan=(CoverageClaim("randomSmall", (feature_key,)),))
    material, _ = run(tmp_path, (record, features, candidates), FakeRunner(record, json.dumps({"cases": cases(large_allowed)}).encode()))
    assert material.large_count == int(large_allowed)

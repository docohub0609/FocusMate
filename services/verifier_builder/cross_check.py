"""Run untrusted verifier candidates; only T030 may make an asset ready."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import time
from traceback import walk_tb
from typing import Callable, Protocol

from apps.api.deepseek_provider import strict_json_loads
from apps.api.trusted_assets import MATERIAL_MAX_BYTES, MAX_TEXT_BYTES
from services.oj_worker.docker_runner import PythonRunRequest, PythonRunResult, ResourcePolicy
from services.oj_worker.judge import outputs_equal
from .build_jobs import ISSUES
from .case_selection import normalize_cases, CaseSetError, TAGS, BOUNDARY_TAGS
from .candidates import CandidateFailure, CandidateSet, MAX_SOURCE_BYTES, _context


CHECK_POLICY_VERSION = "verifier-cross-check-v3"
REPAIR_CHECK_POLICY_VERSION = "verifier-cross-check-v4"


def _exception_locations(error: Exception) -> list[dict]:
    """Keep code locations, excluding messages, source lines and local values."""
    return [{"file": Path(frame.f_code.co_filename).name,
             "function": frame.f_code.co_name, "line": line}
            for frame, line in walk_tb(error.__traceback__)][-8:]


def content_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def same_generated_cases(first: bytes,second: bytes) -> bool:
    """JSON layout is transport; strings and array order remain exact test data."""
    def canonical(raw):
        return json.dumps(strict_json_loads(raw.decode('utf-8')),ensure_ascii=False,
                          sort_keys=True,separators=(',',':'),allow_nan=False)
    try:return canonical(first)==canonical(second)
    except (ValueError,UnicodeError,RecursionError):
        raise CrossCheckFailure('GENERATOR_CASES_INVALID') from None


class PythonRunner(Protocol):
    def run(self, request: PythonRunRequest) -> PythonRunResult: ...


@dataclass(frozen=True)
class SafeFailureSummary:
    case_hash: str
    scale: str
    tags: tuple[str, ...]
    input_byte_count: int
    input_line_count: int
    input_token_count: int
    failure_kind: str

    def to_dict(self) -> dict:
        return {"caseHash": self.case_hash, "scale": self.scale, "tags": list(self.tags),
                "inputByteCount": self.input_byte_count, "inputLineCount": self.input_line_count,
                "inputTokenCount": self.input_token_count, "failureKind": self.failure_kind}


@dataclass(frozen=True)
class CheckedCase:
    case_key: str = field(repr=False)
    scale: str
    tags: tuple[str, ...]
    stdin: bytes = field(repr=False)
    expected_output: bytes = field(default=b"", repr=False)

    def summary(self, failure_kind: str) -> SafeFailureSummary:
        return SafeFailureSummary(content_hash(self.stdin), self.scale, self.tags, len(self.stdin),
                                  len(self.stdin.splitlines()), len(self.stdin.split()), failure_kind)


@dataclass(frozen=True)
class PrecheckedMaterial:
    problem_id: str
    problem_version: int
    problem_content_hash: str
    feature_set_id: str
    cases: tuple[CheckedCase, ...] = field(repr=False)
    fast_reference_source: str = field(repr=False)
    seed: int
    generator_hash: str
    slow_oracle_hash: str
    fast_reference_hash: str
    small_count: int
    large_count: int
    agreement_count: int
    mutation_attempted: int
    mutation_eligible: int
    mutation_killed: int
    elapsed_ms: int
    safe_failure_summaries: tuple[SafeFailureSummary, ...]
    assurance: str = "full"
    warning_codes: tuple[str, ...] = ()
    mutation_repair_count: int = 0


class CrossCheckFailure(Exception):
    """Only fixed messages and safe metadata cross this boundary."""

    def __init__(self, code: str, *, outcome: str = "failed", summaries: tuple[SafeFailureSummary, ...] = (), repair_feedback: dict | None = None):
        self.code = code
        self.message, _, self.retryable = ISSUES[code]
        self.outcome = outcome
        self.summaries = summaries
        self.repair_feedback = repair_feedback
        super().__init__(self.message)


def _large_applicable(context: dict) -> bool:
    # Finite recognized vocabulary, not a semantic parser: first identify input
    # cardinality, then read flat upper bounds or one named dimension's bounds.
    cardinalities = {"n", "m", "inputSize", "size", "length", "cardinality", "count",
                     "sequenceLength", "arrayLength", "stringLength", "vertexCount",
                     "nodeCount", "edgeCount", "elementCount"}
    bounds = {"max", "maximum", "upper", "upperBound", "maxN", "nMax", "maxM", "mMax",
              "maxLength", "lengthMax", "maxSize", "sizeMax", "maxCount", "countMax",
              "length", "size", "count"}
    dimensions = {"n", "m", "length", "size", "count", "vertices", "nodes", "edges"}
    scalar_keys = {"value", "valueRange", "element", "elementRange", "elementValue",
                   "weight", "weightRange", "range"}

    def finite_large(value):
        return (type(value) is int and value > 64) or (type(value) is float and math.isfinite(value) and value > 64)

    for item in context["confirmedFeatures"]:
        if item.get("category") != "dataScale" or item.get("status") != "confirmed" or "review" in item:
            continue
        key_parts = item["featureKey"].split(".")[1:]
        value = item.get("value")
        if not isinstance(value, dict) or any(part in scalar_keys for part in key_parts):
            continue
        describes_cardinality = any(part in cardinalities for part in key_parts) or any(
            isinstance(value.get(key), str) and value[key] in cardinalities
            for key in ("variable", "quantity", "kind", "type"))
        if not describes_cardinality:
            continue
        if any(key in bounds and finite_large(bound) for key, bound in value.items()):
            return True
        if any(key in dimensions and isinstance(nested, dict)
               and any(bound_key in bounds and finite_large(bound) for bound_key, bound in nested.items())
               for key, nested in value.items()):
            return True
    return False


def _generator_control(context: dict, seed: int) -> dict:
    """Tell the generator explicitly whether this problem permits large cases."""

    return {
        "schemaVersion": "1.0.0",
        "seed": seed,
        "smallTarget": 24,
        "largeTarget": 4 if _large_applicable(context) else 0,
    }

class VerifierCrossChecker:
    def __init__(self, runner: PythonRunner, workspace_root: str | Path, *, seed: int = 123,
                 monotonic: Callable[[], float] = time.monotonic, mutation_provider=None) -> None:
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError("The seed must be an unsigned 32-bit integer.")
        self.mutation_provider = mutation_provider
        self.latest_mutants = ()
        self.mutation_audit = []
        self.mutation_repair_count = 0
        self.case_review = None
        self.allow_mutation_repair = True
        self.case_audit = {}
        self.stage = "preparing"
        self.trace = lambda **entry: None
        self.runner, self.workspace_root, self.seed, self.monotonic = runner, Path(workspace_root), seed, monotonic

    def _budget(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise CrossCheckFailure("BUILD_BUDGET_EXCEEDED", outcome="inconclusive")
        return remaining

    def _call(self, workspace: Path, stdin: bytes, deadline: float, *, operation="execute") -> PythonRunResult:
        remaining = self._budget(deadline)
        started = self.monotonic()
        try:
            response = self.runner.run(PythonRunRequest(workspace, stdin,
                ResourcePolicy(wall_time_seconds=min(3.0, remaining)), operation, deadline))
        except Exception as error:
            # Private diagnostics retain the failing boundary, never exception
            # messages: those can contain candidate input, output or host paths.
            self.trace(event="sandboxFailed", stage=self.stage, operation=operation,
                       elapsedMs=max(0, int((self.monotonic()-started)*1000)),
                       errorType=type(error).__name__, locations=_exception_locations(error))
            raise CrossCheckFailure("VERIFIER_BUILDER_INTERNAL_ERROR", outcome="inconclusive") from None
        failure = getattr(response, "failure", None)
        safe_failure = ({key: failure[key] for key in ("stage", "errorType", "elapsedMs", "retryCount")
                         if key in failure} if isinstance(failure, dict) else None)
        self.trace(event="sandbox", stage=self.stage, operation=operation,
                   elapsedMs=max(0, int((self.monotonic()-started)*1000)),
                   terminationReason=response.termination_reason, exitCode=response.exit_code,
                   **({"runnerFailure": safe_failure} if safe_failure else {}),
                   **getattr(self.runner, "last_timing", {}))
        self._budget(deadline)
        if response.termination_reason == "startupTimeExceeded":
            raise CrossCheckFailure("SANDBOX_STARTUP_TIMEOUT", outcome="inconclusive")
        if response.termination_reason == "infrastructureError":
            raise CrossCheckFailure("VERIFIER_BUILDER_INTERNAL_ERROR", outcome="inconclusive")
        return response

    @staticmethod
    def _successful(result: PythonRunResult) -> bool:
        return (result.termination_reason == "exited" and result.exit_code == 0
                and not result.stdout_truncated and not result.stderr_truncated and not result.oom_killed
                and len(result.stdout) <= MAX_TEXT_BYTES and len(result.stderr) <= MAX_TEXT_BYTES)

    def _output(self, workspace: Path, stdin: bytes, deadline: float, code: str, case=None, *, small_oracle=False) -> bytes:
        result = self._call(workspace, stdin, deadline)
        if small_oracle and case is not None and case.scale == "small" and result.termination_reason == "wallTimeExceeded":
            summary = case.summary("candidateExecutionFailed")
            raise CrossCheckFailure("SMALL_REFERENCE_TIMEOUT", summaries=(summary,), repair_feedback={
                "reason":"smallReferenceTimeout", "failureSummary":[summary.to_dict()]})
        try:
            if not self._successful(result):
                raise ValueError("candidate execution")
            result.stdout.decode("utf-8")
        except (ValueError, UnicodeError):
            feedback = None
            if case is not None and case.scale == 'small':
                feedback = {'reason': 'smallReferenceExecutionFailed',
                    'failureSummary': [case.summary('candidateExecutionFailed').to_dict()],
                    'execution': {'exitCode': result.exit_code,
                        'terminationReason': result.termination_reason, 'oomKilled': result.oom_killed,
                        'stderr': result.stderr[:8192].decode('utf-8', errors='replace')},
                    'input': case.stdin.decode('utf-8')}
            elif code == 'GENERATOR_CASES_INVALID':
                feedback = {'reason': 'generatorExecutionFailed', 'execution': {
                    'exitCode': result.exit_code, 'terminationReason': result.termination_reason,
                    'oomKilled': result.oom_killed,
                    'stderr': result.stderr[:8192].decode('utf-8', errors='replace')}}
            raise CrossCheckFailure(code, summaries=(() if case is None else (case.summary("candidateExecutionFailed"),)),
                repair_feedback=feedback) from None
        return result.stdout

    def _cases(self, raw: bytes, large_applicable: bool) -> tuple[CheckedCase, ...]:
        try:
            items, self.case_audit = normalize_cases(raw, large_applicable)
        except CaseSetError as error:
            repairable = error.reason in {"insufficientSmallCoverage", "missingLargeCoverage"}
            raise CrossCheckFailure("GENERATOR_COVERAGE_INSUFFICIENT" if repairable else "GENERATOR_CASES_INVALID",
                repair_feedback={"reason":error.reason}) from None
        self.trace(event="caseSelection", **self.case_audit)
        return tuple(CheckedCase(item["caseKey"],item["scale"],tuple(item["tags"]),item["stdin"].encode("utf-8")) for item in items)

    def precheck(self, problem_record: dict, feature_set: dict, candidates: CandidateSet, deadline: float) -> PrecheckedMaterial:
        started = self.monotonic()
        self.stage = "preparing"
        self.latest_mutants = candidates.mutants
        self.mutation_audit = []
        self.mutation_repair_count = 0
        self._budget(deadline)
        try:
            return self._precheck(problem_record, feature_set, candidates, deadline, started)
        except CrossCheckFailure:
            raise
        except CandidateFailure as error:
            code = "MUTATION_RESPONSE_INVALID" if error.code == "CANDIDATE_SCHEMA_INVALID" and self.stage.startswith("mutation") else error.code
            raise CrossCheckFailure(code) from None
        except Exception as error:
            # File/runner failures must not leak hidden text or host paths.
            self.trace(event="precheckFailed", stage=self.stage, errorType=type(error).__name__,
                       locations=_exception_locations(error))
            raise CrossCheckFailure("VERIFIER_BUILDER_INTERNAL_ERROR", outcome="inconclusive") from None

    def _precheck(self, problem_record, feature_set, candidates, deadline, started):
        context = _context(problem_record, feature_set)
        expected = (context["problemRef"]["problemId"], context["problemRef"]["problemVersion"], context["problemContentHash"], context["featureSetId"])
        if (candidates.problem_id, candidates.problem_version, candidates.problem_content_hash, candidates.feature_set_id) != expected:
            raise CrossCheckFailure("CANDIDATE_SCHEMA_INVALID")
        sources = [candidates.generator.source, candidates.slow_oracle.source, candidates.fast_reference.source] + [m.source for m in candidates.mutants]
        try:
            if (len(candidates.mutants) > 3 or len({m.mutant_key for m in candidates.mutants}) != len(candidates.mutants)
                or any(not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > MAX_SOURCE_BYTES for source in sources)):
                raise ValueError("source")
            allowed = {item["featureKey"] for item in context["confirmedFeatures"]}
            if set(candidates.confirmed_feature_keys) != allowed or not candidates.coverage_plan or any(claim.tag not in TAGS or not claim.feature_keys or not set(claim.feature_keys) <= allowed for claim in candidates.coverage_plan):
                raise ValueError("coverage")
        except (ValueError, UnicodeError):
            raise CrossCheckFailure("CANDIDATE_SCHEMA_INVALID") from None
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        with ExitStack() as cleanup:
            workspaces = []
            for source in sources:
                workspace = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="focusmate-cross-check-", dir=self.workspace_root)))
                (workspace / "main.py").write_bytes(source.encode("utf-8"))
                workspaces.append(workspace)
            generator, slow, fast, *mutants = workspaces
            self.stage = "compile"
            for workspace in (generator, slow, fast):
                compilation = self._call(workspace, b"", deadline, operation="compile")
                if not self._successful(compilation):
                    raise CrossCheckFailure("CANDIDATE_COMPILE_FAILED",
                        repair_feedback={'reason':'generatorCompileFailed','execution':{
                            'exitCode':compilation.exit_code,'terminationReason':compilation.termination_reason,
                            'oomKilled':compilation.oom_killed,
                            'stderr':compilation.stderr[:8192].decode('utf-8',errors='replace')}}
                        if workspace == generator else None)
            compiled_mutants = [] if self.mutation_provider else [workspace for workspace in mutants if self._successful(self._call(workspace, b"", deadline, operation="compile"))]
            large_applicable = _large_applicable(context)
            control = json.dumps(
                _generator_control(context, self.seed), separators=(",", ":"),
            ).encode("utf-8")
            self.stage = "generatorDeterminism"
            raw = self._output(generator, control, deadline, "GENERATOR_CASES_INVALID")
            repeated = self._output(generator, control, deadline, "GENERATOR_CASES_INVALID")
            try:
                equal=same_generated_cases(raw,repeated)
            except CrossCheckFailure as error:
                error.repair_feedback={'reason':'generatorOutputInvalid'}
                raise
            self.trace(event='generatorRepeatComparison',sameCases=equal,
                byteIdentical=raw==repeated,firstHash=content_hash(raw),secondHash=content_hash(repeated),
                firstBytes=len(raw),secondBytes=len(repeated))
            if not equal:
                raise CrossCheckFailure("GENERATOR_NON_DETERMINISTIC",
                    repair_feedback={'reason':'generatorNonDeterministic'})
            cases = self._cases(raw, large_applicable)
            if self.case_review is not None:
                self.stage = "repairedInputLegality"
                self.case_review(cases)
                self._budget(deadline)
            public = tuple((item["input"].encode("utf-8"), item["output"].encode("utf-8")) for item in context["problemPackage"]["examples"])
            if any(len(stdin) > MAX_TEXT_BYTES or len(output) > MAX_TEXT_BYTES for stdin, output in public):
                raise CrossCheckFailure("CANDIDATE_SCHEMA_INVALID")
            self.stage = "publicExamples"
            for workspace in (slow, fast):
                for stdin, expected_output in public:
                    actual = self._output(workspace, stdin, deadline, "REFERENCE_EXECUTION_FAILED")
                    if not outputs_equal(actual, expected_output):
                        raise CrossCheckFailure("REFERENCE_PUBLIC_MISMATCH")
            checked = []
            total_bytes = sum(len(source.encode("utf-8")) for source in sources) + sum(len(a) + len(b) for a, b in public)
            self.stage = "referenceComparison"
            for case in cases:
                actual = self._output(fast, case.stdin, deadline, "REFERENCE_EXECUTION_FAILED", case)
                other = self._output(slow if case.scale == "small" else fast, case.stdin, deadline, "REFERENCE_EXECUTION_FAILED", case, small_oracle=case.scale == "small")
                agree = outputs_equal(actual, other) if case.scale == "small" else actual == other
                if not agree:
                    raise CrossCheckFailure("REFERENCE_DISAGREEMENT", summaries=(case.summary("referenceDisagreement"),))
                total_bytes += len(case.stdin) + len(actual)
                if total_bytes > MATERIAL_MAX_BYTES:
                    raise CrossCheckFailure("GENERATOR_CASES_INVALID")
                checked.append(replace(case, expected_output=actual))
            repair_count = 0
            if self.mutation_provider:
                from .mutation_check import qualify_mutations
                compiled_mutants, repair_count = qualify_mutations(
                    self, cleanup, problem_record, feature_set, public, slow, fast, deadline)
            eligible, summaries = 0, []
            self.stage = "mutation"
            for workspace in compiled_mutants:
                public_passed = True
                for stdin, expected_output in public:
                    result = self._call(workspace, stdin, deadline)
                    if not self._successful(result) or not outputs_equal(result.stdout, expected_output):
                        public_passed = False
                        break
                if not public_passed:
                    continue
                eligible += 1
                for case in checked:
                    result = self._call(workspace, case.stdin, deadline)
                    if not self._successful(result) or not outputs_equal(result.stdout, case.expected_output):
                        summaries.append(case.summary("mutantDetected"))
                        break
                else:
                    raise CrossCheckFailure("MUTATION_TEST_GAP" if self.mutation_provider else "MUTATION_EVIDENCE_INSUFFICIENT")
            if not eligible and not self.mutation_provider:
                raise CrossCheckFailure("MUTATION_EVIDENCE_INSUFFICIENT")
            self._budget(deadline)
            small_count = sum(case.scale == "small" for case in checked)
            return PrecheckedMaterial(*expected, tuple(checked), candidates.fast_reference.source, self.seed,
                *(content_hash(source.encode("utf-8")) for source in sources[:3]),
                small_count, len(checked) - small_count, small_count, len(self.latest_mutants), eligible, len(summaries),
                max(0, int((self.monotonic() - started) * 1000)), tuple(summaries),
                "full" if eligible else "limited", () if eligible else ("MUTATION_CHALLENGE_UNAVAILABLE",), repair_count)

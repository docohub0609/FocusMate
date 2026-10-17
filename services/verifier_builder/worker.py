"""Build trusted material in an independent process, with fenced durable writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import json
import math
import logging
import threading
import time
from typing import Callable
from uuid import uuid4

from apps.api.problem_intake import timestamp
from apps.api.problem_storage import ProblemRepository, StorageFailure
from apps.api.trusted_assets import TrustedAssetBuilder, OracleEvaluationFailure
from apps.api.verifier_assets import VerifierAssetRepository
from services.oj_worker.judge import JudgeFailure
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from .build_jobs import BuildClaim, ISSUES, VerifierBuildJobRepository
from .candidates import CandidateFailure, CandidateSet, ProgramCandidate, MutantCandidate, CoverageClaim, _context
from .cross_check import (CHECK_POLICY_VERSION, REPAIR_CHECK_POLICY_VERSION, CrossCheckFailure,
                          PrecheckedMaterial, VerifierCrossChecker, content_hash)


CHECKS = ["compile", "generatorDeterminism", "publicExamples", "smallDifferential",
          "largeDeterminism", "mutation", "sealedMaterialIntegrity"]


@dataclass(frozen=True)
class BuildOutcome:
    job_id: str
    status: str
    issue_code: str | None = None


class _FreshPrecheckedOracleEvaluator:
    """Consume one fresh precheck snapshot only when the seal request matches it exactly."""

    def __init__(self, material: PrecheckedMaterial) -> None:
        self._source_bytes = material.fast_reference_source.encode("utf-8")
        self._source_hash = material.fast_reference_hash
        self._inputs = tuple(case.stdin for case in material.cases)
        self._outputs = tuple(case.expected_output for case in material.cases)
        self._used = False
        self._lock = threading.Lock()

    def evaluate(self, source_code: str, inputs: tuple[bytes, ...]) -> tuple[bytes, ...]:
        with self._lock:
            if self._used:
                raise OracleEvaluationFailure(
                    "ORACLE_PRECHECK_REUSE_ALREADY_USED",
                    "Fresh prechecked Oracle outputs can only be consumed once.",
                )
            self._used = True
            try:
                source_bytes = source_code.encode("utf-8")
            except (AttributeError, UnicodeEncodeError):
                source_bytes = None
            matches = (
                source_bytes == self._source_bytes
                and source_bytes is not None
                and content_hash(source_bytes) == self._source_hash
                and type(inputs) is tuple
                and all(type(value) is bytes for value in inputs)
                and len(inputs) == len(self._inputs)
                and all(actual == expected for actual, expected in zip(inputs, self._inputs, strict=True))
            )
            if not matches:
                raise OracleEvaluationFailure(
                    "ORACLE_PRECHECK_REUSE_MISMATCH",
                    "Fresh prechecked Oracle evidence does not match the sealing request.",
                )
            return self._outputs


def _restore_candidates(value: dict) -> CandidateSet:
    value = dict(value)
    for name in ("generator", "slow_oracle", "fast_reference"):
        value[name] = ProgramCandidate(**value[name])
    value["mutants"] = tuple(MutantCandidate(**item) for item in value["mutants"])
    value["coverage_plan"] = tuple(CoverageClaim(item["tag"], tuple(item["feature_keys"])) for item in value["coverage_plan"])
    value["confirmed_feature_keys"] = tuple(value["confirmed_feature_keys"])
    return CandidateSet(**value)


def _checkpoint_material(material: PrecheckedMaterial) -> dict:
    value = asdict(material)
    for case in value["cases"]:
        case["stdin"] = case["stdin"].decode("utf-8")
        case["expected_output"] = case["expected_output"].decode("utf-8")
    return value


class VerifierBuilderWorker:
    def __init__(self, jobs: VerifierBuildJobRepository, provider, runner, worker_id: str,
                 workspace_root: str | Path, *, lease_seconds: int = 60,
                 monotonic: Callable[[], float] = time.monotonic, checker=None, builder_factory=None,
                 build_seconds: float = 600.0, mutation_provider=None, repair_agent_provider=None):
        if not math.isfinite(build_seconds) or not 0 < build_seconds <= 1800:
            raise ValueError("build_seconds must be finite and in (0, 1800]")
        self.build_seconds = build_seconds
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.jobs, self.provider, self.runner, self.worker_id = jobs, provider, runner, worker_id
        self.workspace_root, self.lease_seconds, self.monotonic = Path(workspace_root), lease_seconds, monotonic
        self.checker = checker or VerifierCrossChecker(runner, self.workspace_root, monotonic=monotonic,
                                                      mutation_provider=mutation_provider)
        from .input_repair import InputRepairProvider
        self.input_repair_provider = InputRepairProvider(provider) if hasattr(provider, "transport") else None
        self.repair_agent_provider = repair_agent_provider
        self.builder_factory = builder_factory or (lambda evaluator, guard: TrustedAssetBuilder(
            jobs.path, evaluator=evaluator, clock=jobs.clock, transaction_guard=guard))

    def run_once(self) -> BuildOutcome | None:
        claim = self.jobs.discover_and_claim(self.worker_id, self.lease_seconds)
        if claim is None:
            return None
        started = self.monotonic()
        deadline = started + self.build_seconds
        diagnostics = self.workspace_root / "diagnostics"
        trace_path = diagnostics / f"{claim.job_id}-{uuid4().hex}.jsonl"
        try:
            diagnostics.mkdir(parents=True, exist_ok=True)
            trace_path.touch(exist_ok=False)
        except OSError:
            trace_path = None
        def trace(**entry):
            try:
                if trace_path is None:
                    raise OSError("diagnostics unavailable")
                with trace_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                logging.getLogger(__name__).warning("Builder timing record could not be saved.")
        trace(jobId=claim.job_id, problemId=claim.problem_id, problemVersion=claim.problem_version,
              buildSeconds=self.build_seconds, event="started")
        stage = "preparing"
        stage_started = started
        def phase(name):
            nonlocal stage, stage_started
            trace(stage=stage, elapsedMs=max(0, int((self.monotonic()-stage_started)*1000)))
            stage, stage_started = name, self.monotonic()
            self.jobs.set_phase(claim, name)
        if isinstance(self.checker, VerifierCrossChecker):
            self.checker.trace = trace
        stopped, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not stopped.wait(min(10.0, self.lease_seconds / 3)):
                try:
                    self.jobs.renew(claim, self.lease_seconds)
                except Exception:
                    lost.set()
                    return

        def guard(db):
            self._budget(deadline)
            if lost.is_set():
                raise StorageFailure("BUILD_JOB_CLAIM_LOST", "Build claim was lost.", 409, [])
            self.jobs.guard(claim, db)

        def active():
            self._budget(deadline)
            if lost.is_set():
                raise StorageFailure("BUILD_JOB_CLAIM_LOST", "Build claim was lost.", 409, [])
            self.jobs.require_owned(claim)

        thread = threading.Thread(target=heartbeat, name="verifier-build-heartbeat", daemon=True)
        thread.start()
        candidates = None
        checked = None
        try:
            active()
            assets = VerifierAssetRepository(self.jobs.path, clock=self.jobs.clock, transaction_guard=guard)
            try:
                asset = assets.get(claim.problem_id, claim.problem_version)
            except StorageFailure as exc:
                if exc.code != "ASSET_NOT_FOUND": raise
                asset = assets.create_missing(claim.problem_id, claim.problem_version)
            if asset["status"] in {"ready", "rejected"}:
                # T030 committed before the old worker stopped. Validate the
                # existing terminal record and finish only the job bookkeeping.
                return self._finalize(claim, None, None, assets, started, active)
            if asset["status"] == "missing": assets.start_build(claim.problem_id, claim.problem_version)
            problems = ProblemRepository(self.jobs.path)
            record = problems.get(claim.problem_id, claim.problem_version)
            features = problems.get_feature_set(record["lifecycle"]["confirmedFeatureSetId"])
            _context(record, features)
            if isinstance(self.checker, VerifierCrossChecker):
                self.checker.allow_mutation_repair = not bool(claim.repair_policy)
            def automatic_repair(original, error, make_candidates=None):
                from . import automatic_repair as repair
                if not repair.eligible(error) or not isinstance(self.checker, VerifierCrossChecker):
                    raise error
                agent_provider = self.repair_agent_provider
                if agent_provider is None:
                    config = getattr(getattr(self.provider, 'transport', None), 'config', None)
                    if config is None:
                        raise error
                    from focusmate.adapters.repair_agent import DeepSeekRepairProvider
                    agent_provider = DeepSeekRepairProvider(config)
                return repair.run(self.jobs, claim, record, features, original, error,
                    candidate_provider=self.provider, agent_provider=agent_provider, checker=self.checker,
                    deadline=deadline, active=active, make_candidates=make_candidates,
                    review_provider=getattr(self, 'automatic_review_provider', None))
            checkpoint = self.jobs.checkpoint(claim)
            if checkpoint is not None:
                candidates = _restore_candidates(checkpoint)
            else:
                phase("generating")
                from .candidates import CandidateProvider
                if isinstance(self.provider,CandidateProvider):
                    def save_role(role,value):
                        active()
                        self.jobs.save_role(claim,role,value)
                    try:
                        candidates=self.provider.generate(record,features,deadline,
                            checkpoints=self.jobs.role_checkpoints(claim),save_role=save_role,
                            format_correction=not bool(claim.repair_policy))
                    except CandidateFailure as failure:
                        if not claim.repair_policy or not failure.repair_payload:
                            raise
                        cached = self.jobs.role_checkpoints(claim)
                        if not {'slowOracle','fastReference'} <= cached.keys():
                            raise
                        def make_candidates(generator, document):
                            # Sibling roles were already parsed and saved by this attempt.
                            oracle = ProgramCandidate(**cached['slowOracle']['program'])
                            reference = ProgramCandidate(**cached['fastReference']['program'])
                            context = _context(record, features)
                            return CandidateSet(claim.problem_id,claim.problem_version,context['problemContentHash'],
                                context['featureSetId'],tuple(item['featureKey'] for item in context['confirmedFeatures']),
                                generator,tuple(CoverageClaim(item['tag'],tuple(item['featureKeys'])) for item in document['coveragePlan']),
                                oracle,reference,tuple(MutantCandidate(item['mutantKey'],item['defectTag'],item['source'])
                                    for item in cached['fastReference']['document']['mutants']))
                        candidates, checked = automatic_repair(None, failure, make_candidates)
                else:candidates = self.provider.generate(record, features, deadline)
                active()
                self.jobs.save_checkpoint(claim, asdict(candidates))
            # Persisted outputs are recovery data, never evidence for this attempt.
            # Recheck every gate, including mutants, before reusing fresh outputs.
            phase("prechecking")
            original_candidates = candidates
            if isinstance(self.checker, VerifierCrossChecker) and checked is None:
                self.checker.latest_mutants = candidates.mutants
                self.checker.mutation_audit = []
                self.checker.mutation_repair_count = 0
                self.checker.case_review = None
                if hasattr(self.checker.mutation_provider, "last_requests"):
                    self.checker.mutation_provider.last_requests = []
            from . import repair_storage
            restored_review = (repair_storage.restore_review(self.jobs, claim, candidates)
                               if not claim.repair_policy else False)

            def review_inputs(cases):
                active()
                if self.input_repair_provider is None:
                    raise CrossCheckFailure("GENERATOR_INPUT_LEGALITY_UNCONFIRMED")
                review = self.input_repair_provider.review_cases(record, features, cases, deadline)
                active()
                repair_storage.save_review(self.jobs, claim, review, cases)

            if restored_review:
                if not isinstance(self.checker, VerifierCrossChecker):
                    raise CrossCheckFailure("GENERATOR_INPUT_LEGALITY_UNCONFIRMED")
                self.checker.case_review = review_inputs
            elif claim.repair_policy and checked is None and isinstance(self.checker, VerifierCrossChecker):
                from .automatic_repair import restored_review as restore_verified_review
                self.checker.case_review = restore_verified_review(self.jobs, claim, candidates)
            try:
                try:
                    if checked is None:
                        checked = self.checker.precheck(record, features, candidates, deadline)
                except CrossCheckFailure as failure:
                    if claim.repair_policy:
                        candidates, checked = automatic_repair(candidates, failure)
                    else:
                        if (restored_review or not failure.repair_feedback or self.input_repair_provider is None
                                or not isinstance(self.checker, VerifierCrossChecker)
                                or failure.repair_feedback.get('reason') not in {'insufficientSmallCoverage','missingLargeCoverage','smallReferenceTimeout'}):
                            raise
                        active()
                        if not repair_storage.reserve(self.jobs, claim, failure.repair_feedback, candidates):
                            raise
                        trace(event="inputRepairRequested", reason=failure.repair_feedback["reason"])
                        candidates = self.input_repair_provider.repair(record, features, candidates, failure.repair_feedback, deadline)
                        active()
                        repair_storage.save_generator(self.jobs, claim, candidates)
                        self.checker.case_review = review_inputs
                        checked = self.checker.precheck(record, features, candidates, deadline)
            finally:
                if isinstance(self.checker, VerifierCrossChecker):
                    self.checker.case_review = None
                latest = getattr(self.checker, "latest_mutants", None)
                if latest is not None:
                    candidates = replace(candidates, mutants=tuple(latest))
                # This local private record retains the original and repaired programs even
                # if checking failed. It is never copied into the public job/run payload.
                audit_dir = self.workspace_root / "mutation-audit"
                audit_dir.mkdir(parents=True, exist_ok=True)
                audit_path = audit_dir / f"{claim.job_id}-{uuid4().hex}.json"
                with audit_path.open("x", encoding="utf-8") as stream:
                    json.dump({"jobId": claim.job_id,
                               "originalCandidates": asdict(original_candidates),
                               "finalCandidates": asdict(candidates),
                               "mutationAudit": getattr(self.checker, "mutation_audit", []),
                               "mutationRequests": getattr(getattr(self.checker, "mutation_provider", None),
                                                           "last_requests", [])}, stream,
                              ensure_ascii=True, allow_nan=False)
                self.jobs.save_checkpoint(claim, asdict(candidates))
            active()
            self._validate_checked(claim, candidates, checked)
            self.jobs.save_checkpoint(claim, asdict(candidates), _checkpoint_material(checked))
            active()
            phase("sealing")
            evaluator = _FreshPrecheckedOracleEvaluator(checked)
            builder = self.builder_factory(evaluator, guard)
            manifest = self._manifest(claim, checked)
            material = builder.build(manifest)
            active()
            phase("crossChecking")
            return self._finalize(claim, checked, material, assets, started, active)
        except Exception as error:
            code = self._issue(error)
            trace(event="failed", stage=stage, issueCode=code)
            if code != "BUILDER_LEASE_EXPIRED":
                try:
                    if candidates is not None:
                        run = self._run_record(claim, candidates, checked, started, "inconclusive")
                        if isinstance(error, CrossCheckFailure) and error.summaries:
                            run["safeFailureSummaries"] = [item.to_dict() for item in error.summaries]
                        self.jobs.save_run(claim, run)
                except Exception:
                    pass
                try:
                    # Failed diagnostic persistence must not leave a live job running.
                    self.jobs.pause(claim, code)
                except Exception:
                    # A lost/expired owner may not overwrite the replacement's state.
                    pass
            return BuildOutcome(claim.job_id, "paused", code)
        finally:
            trace(stage=stage, elapsedMs=max(0, int((self.monotonic()-stage_started)*1000)))
            trace(event="ended", elapsedMs=max(0, int((self.monotonic()-started)*1000)))
            stopped.set()
            thread.join(timeout=1)

    def _budget(self, deadline: float):
        if self.monotonic() >= deadline:
            raise CrossCheckFailure("BUILD_BUDGET_EXCEEDED", outcome="inconclusive")

    @staticmethod
    def _issue(error: Exception) -> str:
        if isinstance(error, StorageFailure) and error.code == "BUILD_JOB_CLAIM_LOST":
            return "BUILDER_LEASE_EXPIRED"
        if isinstance(error, (CandidateFailure, CrossCheckFailure)) and error.code in ISSUES:
            return error.code
        if isinstance(error, OracleEvaluationFailure):
            if error.code in {"ORACLE_BUILD_BUDGET_EXCEEDED", "ORACLE_TIMEOUT"}: return "BUILD_BUDGET_EXCEEDED"
            if error.code == "ORACLE_COMPILE_ERROR": return "CANDIDATE_COMPILE_FAILED"
        return "VERIFIER_BUILDER_INTERNAL_ERROR"

    @staticmethod
    def _validate_checked(claim, candidates, checked):
        small = sum(case.scale == "small" for case in checked.cases)
        large = sum(case.scale == "large" for case in checked.cases)
        assurance = getattr(checked, "assurance", "full")
        warnings = tuple(getattr(checked, "warning_codes", ()))
        repairs = getattr(checked, "mutation_repair_count", 0)
        mutation_valid = (
            type(repairs) is int and repairs in {0, 1}
            and 0 <= checked.mutation_eligible <= checked.mutation_attempted <= 3
            and checked.mutation_killed == checked.mutation_eligible
            and ((assurance == "full" and not warnings and checked.mutation_eligible >= 1)
                 or (assurance == "limited" and warnings == ("MUTATION_CHALLENGE_UNAVAILABLE",)
                     and repairs == (0 if claim.repair_policy else 1)
                     and checked.mutation_eligible == checked.mutation_killed == 0))
        )
        if (checked.problem_id != claim.problem_id or checked.problem_version != claim.problem_version
                or checked.problem_content_hash != candidates.problem_content_hash
                or checked.feature_set_id != candidates.feature_set_id
                or small != checked.small_count or large != checked.large_count
                or small < 8 or large > 4 or small + large > 64
                or checked.agreement_count != small
                or not mutation_valid
                or checked.generator_hash != content_hash(candidates.generator.source.encode("utf-8"))
                or checked.slow_oracle_hash != content_hash(candidates.slow_oracle.source.encode("utf-8"))
                or checked.fast_reference_hash != content_hash(candidates.fast_reference.source.encode("utf-8"))
                or checked.fast_reference_source != candidates.fast_reference.source):
            raise CrossCheckFailure("VERIFIER_BUILDER_INTERNAL_ERROR", outcome="inconclusive")

    @staticmethod
    def _manifest(claim: BuildClaim, checked: PrecheckedMaterial) -> dict:
        return {"manifestId": "auto-" + claim.job_id, "schemaVersion": "1.0.0",
                "problemRef": {"problemId": claim.problem_id, "problemVersion": claim.problem_version},
                "privateTests": [{"caseKey": case.case_key, "stdin": case.stdin.decode("utf-8"),
                                  "answerSource": "referenceOracle"} for case in checked.cases],
                "referenceOracle": {"language": "python3", "sourceCode": checked.fast_reference_source,
                                    "origin": "generatedCandidate", "resourcePolicyRef": "python-default-v1"}}

    def _run_record(self, claim, candidates, checked, started, outcome):
        value = {"runId": f"cross-check-{uuid4()}", "schemaVersion": "1.0.0",
                 "problemRef": {"problemId": claim.problem_id, "problemVersion": claim.problem_version},
                 "featureSetId": candidates.feature_set_id if candidates else checked.feature_set_id,
                 "outcome": outcome, "seed": getattr(self.checker, "seed", 123) if checked is None else checked.seed,
                 "generatorHash": content_hash(candidates.generator.source.encode()) if checked is None else checked.generator_hash,
                 "slowOracleHash": content_hash(candidates.slow_oracle.source.encode()) if checked is None else checked.slow_oracle_hash,
                 "fastReferenceHash": content_hash(candidates.fast_reference.source.encode()) if checked is None else checked.fast_reference_hash,
                 "smallCount": 0 if checked is None else checked.small_count,
                 "largeCount": 0 if checked is None else checked.large_count,
                 "agreementCount": 0 if checked is None else checked.agreement_count,
                 "mutationAttempted": 0 if checked is None else checked.mutation_attempted,
                 "mutationEligible": 0 if checked is None else checked.mutation_eligible,
                 "mutationKilled": 0 if checked is None else checked.mutation_killed,
                 "elapsedMs": max(0, int((self.monotonic() - started) * 1000)),
                 "checkPolicyVersion": REPAIR_CHECK_POLICY_VERSION if claim.repair_policy else CHECK_POLICY_VERSION,
                 "buildBudgetMs": max(1, int(self.build_seconds * 1000)),
                 "assurance": "full" if checked is None else checked.assurance,
                 "warningCodes": [] if checked is None else list(checked.warning_codes),
                 "mutationRepairCount": getattr(self.checker, "mutation_repair_count", 0) if checked is None else checked.mutation_repair_count,
                 "safeFailureSummaries": [] if checked is None else [item.to_dict() for item in checked.safe_failure_summaries],
                 "createdAt": timestamp(self.jobs.clock())}
        return value

    def _finalize(self, claim, checked, material, assets, started, active):
        active()
        asset = assets.get(claim.problem_id, claim.problem_version)
        if asset["status"] == "ready":
            SQLiteTrustedAssetProvider(self.jobs.path).get(claim.problem_id, claim.problem_version)
            self.jobs.complete(claim)
            return BuildOutcome(claim.job_id, "completed")
        if asset["status"] == "rejected":
            self.jobs.fail(claim)
            return BuildOutcome(claim.job_id, "failed", "SEALED_MATERIAL_MISMATCH")
        mismatch = not self._sealed_matches(claim, checked, material)
        active()
        run = self._run_record(claim, None, checked, started, "failed" if mismatch else "passed")
        run["checkedMaterialHash"] = material["materialHash"]
        self.jobs.save_run(claim, run)
        report = {key: value for key, value in run.items() if key not in {"runId", "schemaVersion", "problemRef", "featureSetId"}}
        report.update(reportId=run["runId"], checksRun=CHECKS)
        # T030 compares the exact seal returned by T032, and alone releases submissions.
        assets.complete_cross_check(claim.problem_id, claim.problem_version, report)
        if mismatch:
            self.jobs.fail(claim)
            return BuildOutcome(claim.job_id, "failed", "SEALED_MATERIAL_MISMATCH")
        self.jobs.complete(claim)
        return BuildOutcome(claim.job_id, "completed")

    def _sealed_matches(self, claim, checked, material):
        provider = SQLiteTrustedAssetProvider(self.jobs.path)
        rows = provider._load(claim.problem_id, claim.problem_version)
        try:
            asset, public, private, oracles = (rows[name] for name in ("asset", "public", "private", "oracles"))
            provider._verify_case_hashes(claim.problem_id, claim.problem_version, asset, public, private, oracles)
            return (rows["material"]["material_hash"] == material["materialHash"]
                    == provider._material_hash(asset, public, private, oracles)
                    and len(private) == len(checked.cases) == checked.small_count + checked.large_count
                    and len(oracles) == 1 and oracles[0]["content_hash"] == checked.fast_reference_hash
                    and checked.fast_reference_hash == content_hash(checked.fast_reference_source.encode("utf-8"))
                    and all(row["case_key"] == case.case_key and row["stdin_text"].encode("utf-8") == case.stdin
                            and row["expected_output_text"].encode("utf-8") == case.expected_output
                            for row, case in zip(private, checked.cases, strict=True)))
        except (JudgeFailure, KeyError, TypeError, ValueError):
            return False

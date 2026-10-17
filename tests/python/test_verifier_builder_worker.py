from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time
from threading import Event

import pytest

from apps.api.problem_storage import StorageFailure
from apps.api.problem_storage import ProblemRepository
from apps.api.problem_features import extract_problem_features
from apps.api.submission_storage import SubmissionRepository
from apps.api.trusted_assets import OracleEvaluationFailure, TrustedAssetBuilder
from apps.api.verifier_assets import VerifierAssetRepository
from services.oj_worker.judge import JudgeFailure
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.verifier_builder.build_jobs import VerifierBuildJobRepository
from services.verifier_builder.candidates import CandidateFailure
from services.verifier_builder.cross_check import VerifierCrossChecker, CrossCheckFailure
from services.verifier_builder.worker import VerifierBuilderWorker
from tests.python.test_verifier_cross_check import setup as checker_setup, FakeRunner, result
from tests.python.test_submission_storage import repository, submission_request
from tests.python.test_problem_features import FakeProvider as FeatureProvider, base_features


class FakeProvider:
    def __init__(self, candidates, callback=None):
        self.candidates, self.callback, self.calls = candidates, callback, 0

    def generate(self, record, features, deadline):
        self.calls += 1
        if self.callback:
            self.callback()
        return self.candidates


@pytest.fixture
def environment(tmp_path):
    record, features, candidates = checker_setup.__wrapped__(tmp_path)
    path = tmp_path / "problem.db"
    jobs = VerifierBuildJobRepository(path)
    runner = FakeRunner(record)
    provider = FakeProvider(candidates)
    worker = VerifierBuilderWorker(jobs, provider, runner, "builder-test", tmp_path / "work")
    return path, jobs, runner, provider, worker


def asset_status(path):
    return VerifierAssetRepository(path).get("problem-submit", 1)["status"]


def test_worker_seals_real_records_releases_submission_and_persists_safe_evidence(environment):
    path, jobs, runner, provider, worker = environment
    submissions = repository(path)
    goal = submissions.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    submissions.create_code_snapshot(goal["goalId"], "raise RuntimeError('USER-MUST-NOT-RUN')", "python3")
    submission, _ = submissions.create_submission(submission_request())
    outcome = worker.run_once()
    assert outcome.status == "completed"
    assert asset_status(path) == "ready"
    assert submissions.get_submission(submission["submissionId"])["status"] == "queued"
    assert submissions.get_goal(goal["goalId"]) == goal
    assert len(SQLiteTrustedAssetProvider(path).get("problem-submit", 1).cases) == 10
    assert provider.calls == 1 and worker.run_once() is None
    assert all("USER-MUST-NOT-RUN" not in role for role, _ in runner.calls)
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0])
        material_hash = db.execute("SELECT material_hash FROM trusted_test_materials").fetchone()[0]
        assert report["checkedMaterialHash"] == material_hash
        assert report["smallCount"] == report["agreementCount"] == 8
        assert report["mutationKilled"] == report["mutationEligible"] == 1
        assert db.execute("SELECT COUNT(*) FROM verifier_candidate_sets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM verifier_cross_check_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='oj_results'").fetchone()[0] == 0
    safe = VerifierBuildJobRepository(path).get("problem-submit", 1)
    assert safe["status"] == "completed" and "SECRET" not in json.dumps(safe)
    assert "SECRET" not in repr(outcome)


def test_fresh_precheck_sealing_does_not_run_the_reference_again(environment):
    path, jobs, runner, provider, worker = environment
    runner_calls_during_build = []

    def factory(evaluator, guard):
        class RecordingBuilder(TrustedAssetBuilder):
            def build(self, manifest):
                before = len(runner.calls)
                material = super().build(manifest)
                runner_calls_during_build.append(len(runner.calls) - before)
                return material

        return RecordingBuilder(path, evaluator=evaluator, transaction_guard=guard)

    worker.builder_factory = factory

    outcome = worker.run_once()

    assert outcome.status == "completed"
    assert runner_calls_during_build == [0]


@pytest.mark.parametrize("damage", ["source", "input", "order", "count"])
def test_fresh_precheck_request_mismatch_never_seals_material(environment, damage):
    path, jobs, runner, provider, worker = environment
    original_manifest = worker._manifest

    def damaged_manifest(claim, checked):
        manifest = original_manifest(claim, checked)
        if damage == "source":
            manifest["referenceOracle"]["sourceCode"] += "# SECRET-changed\n"
        elif damage == "input":
            manifest["privateTests"][0]["stdin"] = "109\n"
        elif damage == "order":
            manifest["privateTests"] = list(reversed(manifest["privateTests"]))
        else:
            manifest["privateTests"].pop()
        return manifest

    worker._manifest = damaged_manifest

    outcome = worker.run_once()

    assert outcome.status == "paused"
    assert outcome.issue_code == "VERIFIER_BUILDER_INTERNAL_ERROR"
    assert asset_status(path) == "building"
    assert "SECRET" not in repr(outcome)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM reference_oracles").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM private_test_cases").fetchone()[0] == 0


@pytest.mark.parametrize("code", ["LLM_TEMPORARILY_UNAVAILABLE", "PROBLEM_FACTS_INSUFFICIENT", "CANDIDATE_SCHEMA_INVALID", "LLM_CONFIGURATION_REQUIRED"])
def test_provider_failures_pause_without_rejecting(environment, code):
    path, jobs, runner, provider, worker = environment
    def fail():
        raise CandidateFailure(code, "SECRET raw provider output", True)
    provider.callback = fail
    outcome = worker.run_once()
    assert outcome.status == "paused" and outcome.issue_code == code
    assert asset_status(path) == "building" and runner.calls == []
    safe = jobs.get("problem-submit", 1)
    assert "SECRET" not in json.dumps(safe)
    if code == "PROBLEM_FACTS_INSUFFICIENT": assert safe["allowedAction"] == "reviseProblem"


@pytest.mark.parametrize("code", ["REFERENCE_DISAGREEMENT", "REFERENCE_EXECUTION_FAILED", "MUTATION_EVIDENCE_INSUFFICIENT", "BUILD_BUDGET_EXCEEDED"])
def test_precheck_failures_never_reject_assets(environment, code):
    path, jobs, runner, provider, worker = environment
    class Checker:
        def precheck(self, *args): raise CrossCheckFailure(code)
    worker.checker = Checker()
    assert worker.run_once().status == "paused"
    assert asset_status(path) == "building"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM verifier_cross_check_runs").fetchone()[0] == 1


def test_absolute_deadline_stops_after_provider(environment):
    path, jobs, runner, provider, worker = environment
    now = [10.0]
    worker.monotonic = lambda: now[0]
    provider.callback = lambda: now.__setitem__(0, 131.0)
    result = worker.run_once()
    assert result.issue_code == "BUILD_BUDGET_EXCEEDED"
    assert runner.calls == [] and asset_status(path) == "building"


def test_lost_claim_prevents_sealing_and_can_be_recovered(environment):
    path, jobs, runner, provider, worker = environment
    now = [datetime.now(timezone.utc)]
    jobs.clock = lambda: now[0]
    def expire(): now[0] += timedelta(seconds=61)
    provider.callback = expire
    assert worker.run_once().issue_code == "BUILDER_LEASE_EXPIRED"
    assert asset_status(path) == "building" and not runner.calls
    assert jobs.discover_and_claim("recover", 60) is None
    assert jobs.get("problem-submit", 1)["status"] == "paused"


def test_configuration_recovery_requires_operator_preflight(environment):
    path, jobs, runner, provider, worker = environment
    claim = jobs.discover_and_claim("operator", 60)
    jobs.pause(claim, "LLM_CONFIGURATION_REQUIRED")
    with pytest.raises(StorageFailure): jobs.retry("problem-submit", 1)
    assert worker.run_once() is None
    assert jobs.requeue_configuration_paused() == 1
    assert worker.run_once().status == "completed"


def test_sealed_output_mismatch_rejects_without_user_failure_verdict(environment):
    path, jobs, runner, provider, worker = environment
    def altered_builder(evaluator, guard):
        class Builder(TrustedAssetBuilder):
            def build(self, manifest):
                material = super().build(manifest)
                with sqlite3.connect(path) as db:
                    db.execute("UPDATE private_test_cases SET expected_output_text='tampered'")
                return material
        return Builder(path, evaluator=evaluator, transaction_guard=guard)
    worker.builder_factory = altered_builder
    result = worker.run_once()
    assert result.status == "failed" and result.issue_code == "SEALED_MATERIAL_MISMATCH"
    assert asset_status(path) == "rejected"


def test_retry_resumes_identical_sealed_material_without_regenerating(environment):
    path, jobs, runner, provider, worker = environment
    original = worker._finalize
    worker._finalize = lambda *a: (_ for _ in ()).throw(RuntimeError("SECRET crash after seal"))
    assert worker.run_once().status == "paused"
    assert asset_status(path) == "crossChecking"
    jobs.retry("problem-submit", 1)
    worker._finalize = original
    assert worker.run_once().status == "completed"
    assert provider.calls == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 1


@pytest.mark.parametrize("corruption", ["staleOutput", "missingCases", "invalidJson"])
def test_retry_recomputes_and_overwrites_unsealed_precheck_checkpoint(environment, corruption):
    path, jobs, runner, provider, worker = environment
    original_factory = worker.builder_factory
    factory_calls = 0

    def fail_before_first_build(evaluator, guard):
        nonlocal factory_calls
        factory_calls += 1
        builder = original_factory(evaluator, guard)
        if factory_calls == 1:
            raise RuntimeError("SECRET crash before seal")
        return builder

    worker.builder_factory = fail_before_first_build

    first = worker.run_once()
    calls_after_precheck = len(runner.calls)

    assert first.status == "paused"
    assert asset_status(path) == "building"
    with sqlite3.connect(path) as db:
        prechecked = json.loads(db.execute(
            "SELECT prechecked_json FROM verifier_candidate_sets",
        ).fetchone()[0])
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 0
        expected_inputs = [case["stdin"].encode("utf-8") for case in prechecked["cases"]]
        if corruption == "staleOutput":
            prechecked["cases"][0]["expected_output"] = "SECRET-tampered-checkpoint-output\n"
        else:
            del prechecked["cases"]
        db.execute(
            "UPDATE verifier_candidate_sets SET prechecked_json=?",
            ("{broken-precheck-json" if corruption == "invalidJson" else json.dumps(prechecked),),
        )

    jobs.retry("problem-submit", 1)
    second = worker.run_once()
    resumed_calls = runner.calls[calls_after_precheck:]
    assert second.status == "completed"
    assert second.issue_code is None
    assert asset_status(path) == "ready"
    assert provider.calls == 1
    assert sum(request.operation == "compile" for role, request in resumed_calls if role == "fast") == 1
    assert {role for role, request in resumed_calls if request.operation == "compile"} == {
        "generator", "slow", "fast", "mutant",
    }
    assert {request.stdin for role, request in resumed_calls
            if role == "fast" and request.operation == "execute"}.issuperset(expected_inputs)
    with sqlite3.connect(path) as db:
        sealed_output = db.execute(
            "SELECT expected_output_text FROM private_test_cases WHERE ordinal=0",
        ).fetchone()[0]
        refreshed = json.loads(db.execute(
            "SELECT prechecked_json FROM verifier_candidate_sets",
        ).fetchone()[0])
    assert sealed_output.encode("utf-8") == expected_inputs[0]
    assert "SECRET-tampered-checkpoint-output" not in sealed_output
    assert refreshed["cases"][0]["expected_output"] == "100\n"
    assert len(SQLiteTrustedAssetProvider(path).get("problem-submit", 1).cases) == 10


def test_retry_rechecks_mutant_changed_to_equivalent_reference(environment):
    path, jobs, runner, provider, worker = environment
    original_factory = worker.builder_factory
    def interrupt_before_seal(*args):
        original_factory(*args)
        raise RuntimeError("interrupt before seal")
    worker.builder_factory = interrupt_before_seal
    assert worker.run_once().status == "paused"
    with sqlite3.connect(path) as db:
        candidate = json.loads(db.execute("SELECT candidate_json FROM verifier_candidate_sets").fetchone()[0])
        candidate["mutants"][0]["source"] = candidate["fast_reference"]["source"]
        db.execute("UPDATE verifier_candidate_sets SET candidate_json=?", (json.dumps(candidate),))

    jobs.retry("problem-submit", 1)
    worker.builder_factory = original_factory
    outcome = worker.run_once()

    assert outcome.status == "paused"
    assert outcome.issue_code == "MUTATION_EVIDENCE_INSUFFICIENT"
    assert jobs.get("problem-submit", 1)["status"] == "paused"
    assert asset_status(path) == "building"
    assert provider.calls == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 0


def test_content_failure_retry_generates_a_new_candidate_and_preserves_run_history(environment):
    path, jobs, runner, provider, worker = environment
    valid = provider.candidates
    provider.candidates = replace(valid, mutants=(replace(valid.mutants[0], source=valid.fast_reference.source),))
    assert worker.run_once().issue_code == "MUTATION_EVIDENCE_INSUFFICIENT"
    with sqlite3.connect(path) as db:
        history = db.execute("SELECT run_id,record_json FROM verifier_cross_check_runs").fetchall()
    assert len(history) == 1

    provider.candidates = valid
    jobs.retry("problem-submit", 1)
    assert worker.run_once().status == "completed"
    assert provider.calls == 2
    assert asset_status(path) == "ready"
    assert len(SQLiteTrustedAssetProvider(path).get("problem-submit", 1).cases) == 10
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT run_id,record_json FROM verifier_cross_check_runs WHERE run_id=?", (history[0][0],)).fetchall() == history
        assert db.execute("SELECT COUNT(*) FROM verifier_cross_check_runs").fetchone()[0] == 2


@pytest.mark.parametrize("committed_status", ["building", "crossChecking", "ready", "rejected"])
def test_content_retry_never_discards_candidates_for_already_sealed_material(environment, monkeypatch, committed_status):
    path, jobs, runner, provider, worker = environment
    def interrupted(*args):
        raise RuntimeError("interrupt after durable write")

    if committed_status == "building":
        monkeypatch.setattr(TrustedAssetBuilder, "_advance_asset_if_needed", interrupted)
    elif committed_status == "crossChecking":
        monkeypatch.setattr(worker, "_finalize", interrupted)
    else:
        original_finalize = VerifierAssetRepository.complete_cross_check
        def commit_then_interrupt(repository, problem_id, version, report):
            if committed_status == "rejected":
                report = {**report, "outcome": "failed"}
            original_finalize(repository, problem_id, version, report)
            interrupted()
        monkeypatch.setattr(VerifierAssetRepository, "complete_cross_check", commit_then_interrupt)
    assert worker.run_once().status == "paused"
    assert asset_status(path) == committed_status
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] == 1
        checkpoint = db.execute("SELECT candidate_json,prechecked_json FROM verifier_candidate_sets").fetchone()
        # Exercise retry's issue policy with an existing immutable seal, including
        # the inter-transaction state where the asset still says building.
        db.execute("UPDATE verifier_build_jobs SET issue_code='MUTATION_EVIDENCE_INSUFFICIENT'")

    jobs.retry("problem-submit", 1)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT candidate_json,prechecked_json FROM verifier_candidate_sets").fetchone() == checkpoint
    monkeypatch.undo()
    assert worker.run_once().status == ("failed" if committed_status == "rejected" else "completed")
    assert provider.calls == 1


@pytest.mark.parametrize("boundary", ["material", "summary"])
@pytest.mark.parametrize("expiration", ["deadline", "lease"])
def test_t032_transactions_recheck_before_commit_and_roll_back(environment, monkeypatch, boundary, expiration):
    path, jobs, runner, provider, worker = environment
    ticks = [10.0]
    now = [datetime.now(timezone.utc)]
    worker.monotonic = worker.checker.monotonic = lambda: ticks[0]
    jobs.clock = lambda: now[0]
    invalidated = []

    def expire(db):
        assert db.in_transaction
        if not invalidated:
            invalidated.append(True)
            if expiration == "deadline":
                ticks[0] = 10.0 + worker.build_seconds
            else:
                now[0] += timedelta(seconds=61)

    if boundary == "material":
        original_insert = TrustedAssetBuilder._insert_private
        def insert_then_expire(db, item):
            original_insert(db, item)
            expire(db)
        monkeypatch.setattr(TrustedAssetBuilder, "_insert_private", staticmethod(insert_then_expire))
    else:
        original_log = VerifierAssetRepository._log
        def log_then_expire(db, row, target, reason, timestamp):
            original_log(db, row, target, reason, timestamp)
            if target == "crossChecking":
                expire(db)
        monkeypatch.setattr(VerifierAssetRepository, "_log", staticmethod(log_then_expire))

    outcome = worker.run_once()
    assert invalidated == [True]
    assert outcome.status == "paused"
    assert outcome.issue_code == ("BUILD_BUDGET_EXCEEDED" if expiration == "deadline" else "BUILDER_LEASE_EXPIRED")
    with sqlite3.connect(path) as db:
        material_count = db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0]
        assert material_count == (0 if boundary == "material" else 1)
        assert db.execute("SELECT COUNT(*) FROM private_test_cases").fetchone()[0] == (0 if boundary == "material" else 9)
        assert db.execute("SELECT COUNT(*) FROM reference_oracles").fetchone()[0] == (0 if boundary == "material" else 1)
        assert db.execute("SELECT status,material_summary_json FROM verifier_assets").fetchone() == ("building", None)
        assert db.execute("SELECT COUNT(*) FROM verifier_asset_transitions WHERE to_status='crossChecking'").fetchone()[0] == 0


@pytest.mark.parametrize("expiration", ["deadline", "lease"])
def test_final_report_transaction_rechecks_before_commit(environment, monkeypatch, expiration):
    path, jobs, runner, provider, worker = environment
    submissions = repository(path)
    goal = submissions.create_goal({"problemId": "problem-submit", "problemVersion": 1})
    submissions.create_code_snapshot(goal["goalId"], "print('user code')", "python3")
    submission, _ = submissions.create_submission(submission_request())
    ticks = [10.0]
    now = [datetime.now(timezone.utc)]
    worker.monotonic = worker.checker.monotonic = lambda: ticks[0]
    jobs.clock = lambda: now[0]
    original_log = VerifierAssetRepository._log
    reached_ready = []

    def expire_before_commit(db, row, target, reason, timestamp):
        original_log(db, row, target, reason, timestamp)
        if target == "ready":
            assert db.in_transaction
            assert db.execute("SELECT status FROM submissions").fetchone()[0] == "queued"
            reached_ready.append(True)
            if expiration == "deadline":
                ticks[0] = 10.0 + worker.build_seconds
            else:
                now[0] += timedelta(seconds=61)

    monkeypatch.setattr(VerifierAssetRepository, "_log", staticmethod(expire_before_commit))
    outcome = worker.run_once()

    assert reached_ready == [True]
    assert outcome.status == "paused"
    assert outcome.issue_code == ("BUILD_BUDGET_EXCEEDED" if expiration == "deadline" else "BUILDER_LEASE_EXPIRED")
    assert asset_status(path) == "crossChecking"
    assert submissions.get_submission(submission["submissionId"])["status"] == "awaitingAssets"
    if expiration == "deadline":
        assert jobs.get("problem-submit", 1)["status"] == "paused"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM verifier_asset_transitions WHERE to_status='ready'").fetchone()[0] == 0


@pytest.mark.parametrize("final_status", ["ready", "rejected"])
def test_restart_after_asset_finalization_finishes_job_without_rebuilding(environment, monkeypatch, final_status):
    path, jobs, runner, provider, worker = environment
    original_finalizer = VerifierAssetRepository.complete_cross_check
    def finalize_then_crash(repository, problem_id, version, report):
        if final_status == "rejected": report = {**report, "outcome": "failed"}
        original_finalizer(repository, problem_id, version, report)
        raise RuntimeError("SECRET crash after asset commit")
    monkeypatch.setattr(VerifierAssetRepository, "complete_cross_check", finalize_then_crash)
    assert worker.run_once().status == "paused"
    assert asset_status(path) == final_status
    calls_after_finalization = len(runner.calls)
    jobs.retry("problem-submit", 1)
    monkeypatch.setattr(VerifierAssetRepository, "complete_cross_check", original_finalizer)
    assert worker.run_once().status == ("completed" if final_status == "ready" else "failed")
    assert provider.calls == 1
    assert len(runner.calls) == calls_after_finalization


def test_retry_reuses_candidate_only_checkpoint_after_interrupted_precheck(environment):
    path, jobs, runner, provider, worker = environment
    original_precheck = worker.checker.precheck
    worker.checker.precheck = lambda *args: (_ for _ in ()).throw(RuntimeError("interrupt before precheck"))
    assert worker.run_once().status == "paused"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT prechecked_json FROM verifier_candidate_sets").fetchone()[0] is None
    assert not runner.calls

    jobs.retry("problem-submit", 1)
    worker.checker.precheck = original_precheck
    assert worker.run_once().status == "completed"
    assert provider.calls == 1
    assert asset_status(path) == "ready"
    assert len(SQLiteTrustedAssetProvider(path).get("problem-submit", 1).cases) == 10


@pytest.mark.parametrize("field,value", [("agreementCount", 7), ("mutationKilled", 0), ("smallCount", 7), ("fastReferenceHash", "sha256:" + "0" * 64)])
def test_judge_provider_rejects_damaged_t033_report(environment, field, value):
    path, jobs, runner, provider, worker = environment
    assert worker.run_once().status == "completed"
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0])
        report[field] = value
        db.execute("UPDATE verifier_assets SET cross_check_report_json=?", (json.dumps(report),))
    with pytest.raises(JudgeFailure): SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


@pytest.mark.parametrize("failure", ["configuration", "docker", "image"])
def test_cli_preflight_failure_does_not_claim_or_show_internals(tmp_path, monkeypatch, capsys, failure):
    from services.verifier_builder import __main__ as cli
    monkeypatch.setenv("FOCUSMATE_DB_PATH", str(tmp_path / "must-not-create.db"))
    class Provider:
        @staticmethod
        def from_environment():
            if failure == "configuration": raise CandidateFailure("LLM_CONFIGURATION_REQUIRED", "SECRET key", False)
            return object()
    monkeypatch.setattr(cli, "CandidateProvider", Provider)
    commands = []
    def command(args, **kwargs):
        commands.append(args)
        from subprocess import CompletedProcess
        return CompletedProcess(args, 1 if failure == "docker" or "image" in args else 0)
    monkeypatch.setattr(cli.subprocess, "run", command)
    assert cli.main(["--once"]) == 1
    assert "SECRET" not in capsys.readouterr().out
    assert not (tmp_path / "must-not-create.db").exists()


def test_cli_health_is_read_only(monkeypatch, capsys):
    from services.verifier_builder import __main__ as cli
    monkeypatch.setattr(cli, "preflight", lambda: (_ for _ in ()).throw(AssertionError("not needed")))
    assert cli.main(["--health"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_heartbeat_renews_while_provider_waits(environment):
    path, jobs, runner, provider, worker = environment
    now = [datetime.now(timezone.utc)]
    jobs.clock = lambda: now[0]
    worker.lease_seconds = 3
    renewed = Event()
    original = jobs.renew
    def renew(claim, seconds):
        result = original(claim, seconds)
        renewed.set()
        return result
    jobs.renew = renew
    def wait_for_heartbeat():
        now[0] += timedelta(seconds=2)
        assert renewed.wait(5), "heartbeat did not run while provider was blocked"
        now[0] += timedelta(seconds=2)
    provider.callback = wait_for_heartbeat
    assert worker.run_once().status == "completed"


@pytest.mark.parametrize("boundary", ["material", "summary", "report"])
def test_lost_owner_is_rejected_inside_write_transaction(environment, boundary):
    path, jobs, runner, provider, worker = environment
    now = [datetime.now(timezone.utc)]
    jobs.clock = lambda: now[0]
    original_guard = jobs.guard
    def guard(claim, db):
        # Invalidate between Worker active() and the downstream write guard.
        if db.in_transaction:
            asset = db.execute("SELECT status FROM verifier_assets").fetchone()
            sealed = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trusted_test_materials'").fetchone()[0]
            material_count = db.execute("SELECT COUNT(*) FROM trusted_test_materials").fetchone()[0] if sealed else 0
            phase = db.execute("SELECT phase FROM verifier_build_jobs").fetchone()[0]
            target = ((boundary == "material" and phase == "sealing" and sealed and not material_count)
                      or (boundary == "summary" and asset and asset[0] == "building" and material_count)
                      or (boundary == "report" and phase == "crossChecking" and asset and asset[0] == "crossChecking"))
            if target: now[0] += timedelta(seconds=61)
        original_guard(claim, db)
    jobs.guard = guard
    assert worker.run_once().issue_code == "BUILDER_LEASE_EXPIRED"
    assert asset_status(path) != "ready"


def test_oracle_budget_is_inconclusive_and_does_not_seal(environment):
    path, jobs, runner, provider, worker = environment
    def factory(evaluator, guard):
        class FailingEvaluator:
            def evaluate(self, source, inputs):
                raise OracleEvaluationFailure("ORACLE_BUILD_BUDGET_EXCEEDED", "SECRET")
        return TrustedAssetBuilder(path, evaluator=FailingEvaluator(), transaction_guard=guard)
    worker.builder_factory = factory
    outcome = worker.run_once()
    assert outcome.issue_code == "BUILD_BUDGET_EXCEEDED" and asset_status(path) == "building"


def test_automatic_report_cannot_be_downgraded_to_legacy(environment):
    path, jobs, runner, provider, worker = environment
    assert worker.run_once().status == "completed"
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0])
        report = {key: report[key] for key in ("reportId", "outcome", "checkedMaterialHash", "checksRun", "createdAt")}
        db.execute("UPDATE verifier_assets SET cross_check_report_json=?", (json.dumps(report),))
    with pytest.raises(JudgeFailure): SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


@pytest.mark.parametrize("field", ["generatorHash", "slowOracleHash"])
def test_report_artifact_hashes_match_persisted_candidates(environment, field):
    path, jobs, runner, provider, worker = environment
    assert worker.run_once().status == "completed"
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0])
        report[field] = "sha256:" + "0" * 64
        db.execute("UPDATE verifier_assets SET cross_check_report_json=?", (json.dumps(report),))
    with pytest.raises(JudgeFailure): SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


@pytest.mark.parametrize("agreement", [0, -1])
def test_incomplete_prechecked_counts_never_release_submission(environment, agreement):
    path, jobs, runner, provider, worker = environment
    checker = worker.checker
    class BadChecker:
        def precheck(self, *args): return replace(checker.precheck(*args), agreement_count=agreement)
    worker.checker = BadChecker()
    assert worker.run_once().status != "completed"
    assert asset_status(path) != "ready"
    assert jobs.get("problem-submit", 1)["status"] == "paused"


def test_only_exact_problem_version_submissions_are_released(environment):
    path, jobs, runner, provider, worker = environment
    problems = ProblemRepository(path)
    revised = problems.create_revision("problem-submit", 1)
    features = extract_problem_features({"problemPackage": revised["problemPackage"]},
                                       FeatureProvider([{"features": base_features()}]))
    problems.save_feature_set(features)
    problems.freeze("problem-submit", 2, features["featureSetId"], 1)
    submissions = SubmissionRepository(path)
    records = []
    for version in (1, 2):
        goal = submissions.create_goal({"problemId": "problem-submit", "problemVersion": version})
        submissions.create_code_snapshot(goal["goalId"], "print('user code')", "python3")
        submitted, _ = submissions.create_submission({
            "goalContext": {"goalId": goal["goalId"], "scope": "goal"},
            "problemRef": {"problemId": "problem-submit", "problemVersion": version},
            "codeVersion": 1, "language": "python3", "idempotencyKey": f"click-{version}"})
        records.append(submitted)
    assert worker.run_once().status == "completed"
    assert [submissions.get_submission(item["submissionId"])["status"] for item in records] == ["queued", "awaitingAssets"]


def test_failed_reference_keeps_only_safe_case_summary(environment):
    path, jobs, runner, provider, worker = environment
    runner.change = lambda role, request, _: result(b"SECRET WRONG") if role == "slow" and request.stdin == b"100\n" else None
    assert worker.run_once().issue_code == "REFERENCE_DISAGREEMENT"
    with sqlite3.connect(path) as db:
        run = json.loads(db.execute("SELECT record_json FROM verifier_cross_check_runs").fetchone()[0])
    assert len(run["safeFailureSummaries"]) == 1
    assert run["safeFailureSummaries"][0]["failureKind"] == "referenceDisagreement"
    assert "SECRET" not in json.dumps(run) and "100\\n" not in json.dumps(run)


def test_oracle_provenance_cannot_downgrade_automatic_material(environment):
    path, jobs, runner, provider, worker = environment
    assert worker.run_once().status == "completed"
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0])
        report = {key: report[key] for key in ("reportId", "outcome", "checkedMaterialHash", "checksRun", "createdAt")}
        db.execute("UPDATE reference_oracles SET origin='internalProvided'")
        db.execute("UPDATE verifier_assets SET cross_check_report_json=?", (json.dumps(report),))
        db.execute("DELETE FROM verifier_candidate_sets")
        db.execute("DELETE FROM verifier_cross_check_runs")
    with pytest.raises(JudgeFailure) as error:
        SQLiteTrustedAssetProvider(path).get("problem-submit", 1)
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("change", ["removed", "extra", "duplicate"])
def test_persisted_mutant_set_must_match_report_count_and_have_unique_keys(environment, change):
    path, jobs, runner, provider, worker = environment
    assert worker.run_once().status == "completed"
    with sqlite3.connect(path) as db:
        candidate = json.loads(db.execute("SELECT candidate_json FROM verifier_candidate_sets").fetchone()[0])
        if change == "removed": candidate["mutants"] = []
        elif change == "extra": candidate["mutants"].append({**candidate["mutants"][0], "mutant_key": "extra"})
        else:
            candidate["mutants"].append(dict(candidate["mutants"][0]))
            # Preserve numerical consistency, so the duplicate-key rule itself is tested.
            for table, column in (("verifier_assets", "cross_check_report_json"), ("verifier_cross_check_runs", "record_json")):
                report = json.loads(db.execute(f"SELECT {column} FROM {table}").fetchone()[0])
                report["mutationAttempted"] = 2
                db.execute(f"UPDATE {table} SET {column}=?", (json.dumps(report),))
        db.execute("UPDATE verifier_candidate_sets SET candidate_json=?", (json.dumps(candidate),))
    with pytest.raises(JudgeFailure): SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


@pytest.mark.parametrize("change", ["candidateHash", "checkpointHash", "featureSet"])
def test_persisted_automatic_evidence_matches_frozen_problem_context(environment, change):
    path, jobs, runner, provider, worker = environment
    assert worker.run_once().status == "completed"
    with sqlite3.connect(path) as db:
        candidate_json, checked_json = db.execute("SELECT candidate_json,prechecked_json FROM verifier_candidate_sets").fetchone()
        candidate, checked = json.loads(candidate_json), json.loads(checked_json)
        if change == "candidateHash": candidate["problem_content_hash"] = "sha256:" + "0" * 64
        elif change == "checkpointHash": checked["problem_content_hash"] = "sha256:" + "0" * 64
        else:
            candidate["feature_set_id"] = checked["feature_set_id"] = "features-other"
            run = json.loads(db.execute("SELECT record_json FROM verifier_cross_check_runs").fetchone()[0])
            run["featureSetId"] = "features-other"
            db.execute("UPDATE verifier_cross_check_runs SET record_json=?", (json.dumps(run),))
        db.execute("UPDATE verifier_candidate_sets SET candidate_json=?,prechecked_json=?",
                   (json.dumps(candidate), json.dumps(checked)))
    with pytest.raises(JudgeFailure): SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


def test_legacy_hybrid_manifest_reconstruction_preserves_text_and_answer_shape(environment):
    from tests.python.test_trusted_assets import FakeOracleEvaluator, oracle_manifest, mark_ready
    path, jobs, runner, provider, worker = environment
    manifest = oracle_manifest()
    manifest["privateTests"].append({"caseKey": "unicode-explicit", "stdin": " \r\n秘密\n",
        "answerSource": "expectedOutput", "expectedOutput": " 答案\r\n"})
    material = TrustedAssetBuilder(path, evaluator=FakeOracleEvaluator((b"1 1\n", b"1 3\n"))).build(manifest)
    mark_ready(path, material)
    bundle = SQLiteTrustedAssetProvider(path).get("problem-submit", 1)
    assert bundle.cases[-1].stdin == " \r\n秘密\n".encode("utf-8")
    assert bundle.cases[-1].expected_output == " 答案\r\n".encode("utf-8")


def test_legacy_report_cannot_include_only_one_automatic_field(environment):
    from tests.python.test_trusted_assets import expected_output_manifest, mark_ready
    path, jobs, runner, provider, worker = environment
    material = TrustedAssetBuilder(path).build(expected_output_manifest())
    mark_ready(path, material)
    assert SQLiteTrustedAssetProvider(path).get("problem-submit", 1).cases
    with sqlite3.connect(path) as db:
        report = json.loads(db.execute("SELECT cross_check_report_json FROM verifier_assets").fetchone()[0])
        report["seed"] = 123
        db.execute("UPDATE verifier_assets SET cross_check_report_json=?", (json.dumps(report),))
    with pytest.raises(JudgeFailure): SQLiteTrustedAssetProvider(path).get("problem-submit", 1)


def test_configured_budget_allows_slow_preparation_without_weakening_runs(environment):
    path, jobs, runner, provider, old = environment
    clock = [0.0]
    provider.callback = lambda: clock.__setitem__(0, 130.0)
    worker = VerifierBuilderWorker(jobs, provider, runner, "budget-test", old.workspace_root,
        monotonic=lambda: clock[0], build_seconds=300)
    assert worker.run_once().status == "completed"
    assert all(req.policy.wall_time_seconds <= 3 for _, req in runner.calls)
    traces = list((old.workspace_root / "diagnostics").glob("*.jsonl"))
    assert len(traces) == 1
    rows = [json.loads(line) for line in traces[0].read_text().splitlines()]
    assert rows[0]["buildSeconds"] == 300
    assert any(row.get("stage") == "generating" and row.get("elapsedMs") == 130000 for row in rows)


def test_configured_budget_still_stops_before_checks(environment):
    _, jobs, runner, provider, old = environment
    clock = [0.0]
    provider.callback = lambda: clock.__setitem__(0, 11.0)
    worker = VerifierBuilderWorker(jobs, provider, runner, "budget-test", old.workspace_root,
        monotonic=lambda: clock[0], build_seconds=10)
    assert worker.run_once().issue_code == "BUILD_BUDGET_EXCEEDED"
    assert not runner.calls

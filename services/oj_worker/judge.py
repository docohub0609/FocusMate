"""Trusted test scheduling, output comparison, and OJResult persistence."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable, Protocol
from uuid import uuid4

from .docker_runner import PythonRunRequest, PythonRunResult, ResourceSummary
from .queue import Claim, WorkItem, WorkerQueue, timestamp
from src.focusmate.adapters.raw_events import SQLiteEventStore, event_from_record


@dataclass(frozen=True)
class TrustedTestCase:
    test_case_id: str
    stdin: bytes
    expected_output: bytes


@dataclass(frozen=True)
class TrustedTestBundle:
    asset_id: str
    asset_version: int
    problem_id: str
    problem_version: int
    status: str
    cases: tuple[TrustedTestCase, ...]
    assurance: str = "full"


class TrustedAssetProvider(Protocol):
    def get(self, problem_id: str, problem_version: int) -> TrustedTestBundle: ...


class PythonRunner(Protocol):
    def run(self, request: PythonRunRequest) -> PythonRunResult: ...


class JudgeFailure(Exception):
    """Infrastructure or trusted-input failure; never convert this to a Verdict."""
    def __init__(self, message, *, failure=None):
        super().__init__(message)
        self.failure = failure


class InMemoryTrustedAssetProvider:
    """T023 adapter used by tests; durable trusted assets belong to T030."""

    def __init__(self, bundles: list[TrustedTestBundle]) -> None:
        self._bundles = {
            (bundle.problem_id, bundle.problem_version): bundle for bundle in bundles
        }

    def get(self, problem_id: str, problem_version: int) -> TrustedTestBundle:
        try:
            return self._bundles[(problem_id, problem_version)]
        except KeyError as exc:
            raise JudgeFailure("Trusted tests are unavailable for this problem version.") from exc


class OJResultRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[], str] = lambda: f"oj-result-{uuid4()}",
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.id_factory = id_factory
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            SQLiteEventStore.initialize(db)
            db.execute("""CREATE TABLE IF NOT EXISTS oj_results (
                oj_result_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                submission_id TEXT NOT NULL UNIQUE,
                goal_id TEXT NOT NULL,
                problem_id TEXT NOT NULL,
                problem_version INTEGER NOT NULL,
                code_version INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                asset_version INTEGER NOT NULL,
                verdict TEXT NOT NULL CHECK(verdict IN ('AC','WA','TLE','RE','CE')),
                test_summary_json TEXT NOT NULL,
                resource_summary_json TEXT NOT NULL,
                stderr_category TEXT,
                comparison_policy_json TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source='trustedOjWorker'),
                FOREIGN KEY(submission_id) REFERENCES submissions(submission_id),
                FOREIGN KEY(goal_id) REFERENCES goal_records(goal_id)
            )""")

            columns = {row[1] for row in db.execute("PRAGMA table_info(oj_results)")}
            if "assurance_json" not in columns:
                db.execute("ALTER TABLE oj_results ADD COLUMN assurance_json TEXT")

    def get_for_submission(self, submission_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as db:
            return self._get_for_submission(db, submission_id)

    def save(
        self,
        item: WorkItem,
        bundle: TrustedTestBundle,
        outcome: "JudgeOutcome",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        """Save directly, or participate in the Worker's owning transaction."""
        if connection is not None:
            return self._save_on_connection(connection, item, bundle, outcome)
        with closing(self._connect()) as db, db:
            return self._save_on_connection(db, item, bundle, outcome)

    def _save_on_connection(
        self,
        db: sqlite3.Connection,
        item: WorkItem,
        bundle: TrustedTestBundle,
        outcome: "JudgeOutcome",
    ) -> dict[str, object]:
        existing = self._get_for_submission(db, item.submission_id)
        if existing is not None:
            return existing
        result: dict[str, object] = {
            "ojResultId": self.id_factory(), "schemaVersion": "1.0.0",
            "createdAt": timestamp(self.clock()),
            "goalContext": {"goalId": item.goal_id, "scope": "goal"},
            "submissionId": item.submission_id,
            "problemRef": {
                "problemId": item.problem_id, "problemVersion": item.problem_version,
            },
            "codeVersion": item.code_version,
            "verifierAssetRef": {
                "assetId": bundle.asset_id, "assetVersion": bundle.asset_version,
                "status": "ready",
            },
            "verdict": outcome.verdict,
            "testSummary": {
                "passed": outcome.passed, "total": len(bundle.cases),
                "firstFailedCase": outcome.first_failed_case,
            },
            "resourceSummary": {
                "totalWallTimeMs": outcome.total_wall_time_ms,
                "totalExecutionTimeMs": outcome.total_execution_time_ms,
                "peakMemoryBytes": outcome.peak_memory_bytes,
            },
            "stderrCategory": outcome.stderr_category,
            "comparisonPolicy": {
                "normalizeLineEndings": True, "ignoreOneFinalNewline": True,
                "caseSensitive": True, "otherWhitespaceSensitive": True,
            },
            "source": "trustedOjWorker",
        }
        if bundle.assurance == "limited":
            result.update(assurance="limited", warningCodes=["MUTATION_CHALLENGE_UNAVAILABLE"])
        values = (
            result["ojResultId"], result["schemaVersion"], result["createdAt"],
            item.submission_id, item.goal_id, item.problem_id, item.problem_version,
            item.code_version, bundle.asset_id, bundle.asset_version, outcome.verdict,
            json.dumps(result["testSummary"], separators=(",", ":")),
            json.dumps(result["resourceSummary"], separators=(",", ":")),
            outcome.stderr_category,
            json.dumps(result["comparisonPolicy"], separators=(",", ":")),
            "trustedOjWorker",
            json.dumps({"assurance": result["assurance"], "warningCodes": result["warningCodes"]})
            if bundle.assurance == "limited" else None,
        )
        try:
            db.execute("INSERT INTO oj_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        except sqlite3.IntegrityError:
            concurrent = self._get_for_submission(db, item.submission_id)
            if concurrent is None:
                raise
            return concurrent
        store = SQLiteEventStore(self.path, initialize=False, clock=lambda: timestamp(self.clock()))
        store.append_on_connection(db, event_from_record('ojResultProduced', result), trusted_source='ojWorker')
        return result

    def _get_for_submission(
        self, db: sqlite3.Connection, submission_id: str,
    ) -> dict[str, object] | None:
        row = db.execute(
            "SELECT * FROM oj_results WHERE submission_id=?", (submission_id,),
        ).fetchone()
        return None if row is None else self._result(row)

    @staticmethod
    def _result(row: sqlite3.Row) -> dict[str, object]:
        result = {
            "ojResultId": row["oj_result_id"], "schemaVersion": row["schema_version"],
            "createdAt": row["created_at"],
            "goalContext": {"goalId": row["goal_id"], "scope": "goal"},
            "submissionId": row["submission_id"],
            "problemRef": {
                "problemId": row["problem_id"], "problemVersion": row["problem_version"],
            },
            "codeVersion": row["code_version"],
            "verifierAssetRef": {
                "assetId": row["asset_id"], "assetVersion": row["asset_version"],
                "status": "ready",
            },
            "verdict": row["verdict"],
            "testSummary": json.loads(row["test_summary_json"]),
            "resourceSummary": json.loads(row["resource_summary_json"]),
            "stderrCategory": row["stderr_category"],
            "comparisonPolicy": json.loads(row["comparison_policy_json"]),
            "source": row["source"],
        }
        if "assurance_json" in row.keys() and row["assurance_json"] is not None:
            assurance = json.loads(row["assurance_json"])
            if assurance != {"assurance": "limited", "warningCodes": ["MUTATION_CHALLENGE_UNAVAILABLE"]}:
                raise JudgeFailure("The saved judge assurance is invalid.")
            result.update(assurance)
        return result


@dataclass(frozen=True)
class JudgeOutcome:
    verdict: str
    passed: int
    first_failed_case: int | None
    total_wall_time_ms: int
    peak_memory_bytes: int | None
    stderr_category: str | None
    total_execution_time_ms: int | None = None


@dataclass(frozen=True)
class PreparedJudgeResult:
    bundle: TrustedTestBundle | None
    outcome: JudgeOutcome | None
    existing_result: dict[str, object] | None = None


class JudgeSubmissionProcessor:
    def __init__(
        self, runner: PythonRunner, assets: TrustedAssetProvider, results: OJResultRepository,
    ) -> None:
        self.runner = runner
        self.assets = assets
        self.results = results

    def process(self, item: WorkItem, workspace: Path) -> None:
        prepared = self.prepare_owned(item, workspace)
        if prepared.existing_result is not None:
            return
        if prepared.bundle is None or prepared.outcome is None:
            raise JudgeFailure("The Judge produced no result to save.")
        self.results.save(item, prepared.bundle, prepared.outcome)

    def prepare_owned(self, item: WorkItem, workspace: Path) -> PreparedJudgeResult:
        """Compute a Verdict without writing it before ownership is rechecked."""
        existing = self.results.get_for_submission(item.submission_id)
        if existing is not None:
            return PreparedJudgeResult(None, None, existing)
        bundle = self.assets.get(item.problem_id, item.problem_version)
        self._validate_bundle(item, bundle)
        compile_result = self.runner.run(PythonRunRequest(workspace, b"", operation="compile"))
        accumulator = _Resources()
        accumulator.add(compile_result.resource_summary, execution=False)
        if compile_result.termination_reason in {"infrastructureError", "startupTimeExceeded"}:
            raise JudgeFailure("The sandbox infrastructure could not compile the submission.", failure=compile_result.failure)
        if compile_result.termination_reason != "exited":
            outcome = accumulator.outcome("CE", 0, None, "syntaxError")
        elif compile_result.exit_code != 0:
            outcome = accumulator.outcome("CE", 0, None, "syntaxError")
        else:
            outcome = self._run_cases(workspace, bundle, accumulator)
        return PreparedJudgeResult(bundle, outcome)

    def finalize_owned(
        self,
        item: WorkItem,
        claim: Claim,
        prepared: object,
        queue: WorkerQueue,
    ) -> dict[str, object]:
        """Commit result + finished + claim deletion as one SQLite transaction."""
        if not isinstance(prepared, PreparedJudgeResult):
            raise JudgeFailure("The Judge finalization input is invalid.")
        if self.results.path.resolve() != queue.path.resolve():
            raise JudgeFailure("Judge results and the Worker queue must share one database.")

        def write_result(db: sqlite3.Connection) -> dict[str, object]:
            if prepared.existing_result is not None:
                existing = self.results._get_for_submission(db, item.submission_id)
                if existing is None:
                    raise JudgeFailure("The previously saved Judge result disappeared.")
                return existing
            if prepared.bundle is None or prepared.outcome is None:
                raise JudgeFailure("The Judge produced no result to finalize.")
            return self.results.save(
                item, prepared.bundle, prepared.outcome, connection=db,
            )

        return queue.finalize_owned(claim, write_result)

    def _run_cases(
        self, workspace: Path, bundle: TrustedTestBundle, accumulator: "_Resources",
    ) -> JudgeOutcome:
        passed = 0
        for ordinal, case in enumerate(bundle.cases, start=1):
            result = self.runner.run(PythonRunRequest(workspace, case.stdin))
            accumulator.add(result.resource_summary)
            reason = result.termination_reason
            if reason in {"infrastructureError", "startupTimeExceeded"}:
                raise JudgeFailure("The sandbox infrastructure failed during judging.", failure=result.failure)
            if reason == "wallTimeExceeded":
                return accumulator.outcome("TLE", passed, ordinal, "resourceLimit")
            if reason in {"memoryLimitExceeded", "outputLimitExceeded"}:
                category = "outputLimit" if reason == "outputLimitExceeded" else "resourceLimit"
                return accumulator.outcome("RE", passed, ordinal, category)
            if reason != "exited" or result.exit_code != 0:
                if reason == "exited" and result.exit_code in {-9, 137} and not result.oom_killed:
                    return accumulator.outcome("TLE", passed, ordinal, "resourceLimit")
                return accumulator.outcome("RE", passed, ordinal, "runtimeError")
            if not outputs_equal(result.stdout, case.expected_output):
                return accumulator.outcome("WA", passed, ordinal, None)
            passed += 1
        return accumulator.outcome("AC", passed, None, None)

    @staticmethod
    def _validate_bundle(item: WorkItem, bundle: TrustedTestBundle) -> None:
        if bundle.status != "ready":
            raise JudgeFailure("Trusted tests are not ready.")
        if (bundle.problem_id, bundle.problem_version) != (item.problem_id, item.problem_version):
            raise JudgeFailure("Trusted tests belong to another problem version.")
        if bundle.asset_version < 1 or not bundle.asset_id or not bundle.cases:
            raise JudgeFailure("Trusted tests are incomplete.")
        if any(not case.test_case_id for case in bundle.cases):
            raise JudgeFailure("A trusted test has no identity.")


class _Resources:
    def __init__(self) -> None:
        self.total_wall_time_ms = 0
        self.peak_memory_bytes: int | None = None
        self.execution_total = 0
        self.execution_count = 0
        self.execution_measured = True

    def add(self, summary: ResourceSummary, *, execution: bool = True) -> None:
        self.total_wall_time_ms += summary.wall_time_ms
        if not execution:
            return
        self.execution_count += 1
        if summary.execution_time_ms is None:
            self.execution_measured = False
        else:
            self.execution_total += summary.execution_time_ms
        if summary.peak_memory_bytes is not None:
            self.peak_memory_bytes = max(self.peak_memory_bytes or 0, summary.peak_memory_bytes)

    def outcome(
        self, verdict: str, passed: int, failed: int | None, category: str | None,
    ) -> JudgeOutcome:
        return JudgeOutcome(
            verdict, passed, failed, self.total_wall_time_ms,
            self.peak_memory_bytes, category,
            self.execution_total if self.execution_measured and self.execution_count else None,
        )


def outputs_equal(actual: bytes, expected: bytes) -> bool:
    """Normalize line endings and ignore at most one final LF; preserve all else."""
    return _normalize_output(actual) == _normalize_output(expected)


def _normalize_output(value: bytes) -> bytes:
    normalized = value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized[:-1] if normalized.endswith(b"\n") else normalized

"""Real Docker/OJ/T074 extension for the isolated Coach comparison.

The problem, two reference sources and expected outputs are human-supplied.
Only completed real reference checks authorize the legacy internal asset seal.
This exercises Runner/Judge/final verification, not AI Builder generation,
mutation qualification, hidden-test quality, or algorithm proof.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from apps.api.trusted_assets import TrustedAssetBuilder
from apps.api.verifier_assets import VerifierAssetRepository
from services.oj_worker.docker_runner import DockerPythonRunner, PythonRunRequest
from services.oj_worker.editor_runs import RunRepository, RunWorker
from services.oj_worker.judge import JudgeSubmissionProcessor, OJResultRepository, outputs_equal
from services.oj_worker.queue import WorkerQueue
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.oj_worker.worker import Worker


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def exercise(service, goal_id, session, case, root):
    root = Path(root) / "docker-acceptance"
    root.mkdir(exist_ok=False)
    log = {"scope": __doc__, "status": "running", "referenceChecks": []}
    save(root / "result.json", log)
    if not case.get("reference") or not case.get("privateExamples"):
        raise ValueError("Independent human reference and private expected outputs required")
    if case["reference"] == case["fixed"]:
        raise ValueError("References must not be the same source")
    started = time.monotonic()
    runner = DockerPythonRunner()
    inputs = case["examples"] + case["privateExamples"]
    for index, source in enumerate((case["fixed"], case["reference"])):
        workspace = root / ("reference-" + str(index + 1))
        workspace.mkdir()
        (workspace / "main.py").write_text(source, encoding="utf-8")
        for ordinal, item in enumerate(inputs):
            checked = runner.run(PythonRunRequest(workspace, item["input"].encode("utf-8")))
            row = {"reference": index + 1, "sourceHash": hashlib.sha256(source.encode()).hexdigest(),
                   "caseIndex": ordinal, "input": item["input"], "expected": item["output"],
                   "stdout": checked.stdout.decode("utf-8", errors="replace"),
                   "stderr": checked.stderr.decode("utf-8", errors="replace"),
                   "exitCode": checked.exit_code, "terminationReason": checked.termination_reason,
                   "resourceSummary": asdict(checked.resource_summary), "failure": checked.failure}
            row["passed"] = (checked.exit_code == 0 and checked.termination_reason == "exited"
                             and not checked.stdout_truncated and not checked.stderr_truncated
                             and not checked.oom_killed
                             and outputs_equal(checked.stdout, item["output"].encode()))
            log["referenceChecks"].append(row)
            save(root / "result.json", log)
            if not row["passed"]:
                log.update(status="referenceCheckFailed", wallSeconds=round(time.monotonic() - started, 3))
                save(root / "result.json", log)
                return log
    problem_ref = service.editor.submissions.get_goal(goal_id)["problemRef"]
    material = TrustedAssetBuilder(service.path).build({
        "manifestId": "workflow-human-reference-material", "schemaVersion": "1.0.0", "problemRef": problem_ref,
        "privateTests": [{"caseKey": "human-" + str(i), "stdin": x["input"],
                          "answerSource": "expectedOutput", "expectedOutput": x["output"]}
                         for i, x in enumerate(case["privateExamples"], 1)],
    })
    reference_report = {"reportId": "workflow-real-reference-check", "outcome": "passed",
                        "checkedMaterialHash": material["materialHash"],
                        "checksRun": ["referenceAgreement", "privateCoveragePresent"],
                        "createdAt": datetime.now(timezone.utc).isoformat()}
    asset = VerifierAssetRepository(service.path).complete_cross_check(
        problem_ref["problemId"], problem_ref["problemVersion"], reference_report)
    log.update(material=material, asset=asset, referenceReport=reference_report)
    code_version = service.synchronize(session)["codeVersion"]
    run_repo = RunRepository(service.path)
    run = run_repo.enqueue({"goalContext": {"goalId": goal_id, "scope": "goal"}, "problemRef": problem_ref,
                            "codeVersion": code_version, "runMode": "publicExample", "operationId": "comparison-public-run"})
    run_start = time.monotonic()
    RunWorker(run_repo, root / "public-run", runner=runner).process_once()
    log["publicRun"] = run_repo.get(run["runId"])
    log["publicRunSeconds"] = round(time.monotonic() - run_start, 3)
    public_cases = (log["publicRun"].get("result") or {}).get("cases", [])
    if (log["publicRun"]["status"] != "finished" or not public_cases
            or len(public_cases) != len(case["examples"])
            or any(item.get("outcome") != "passed" for item in public_cases)):
        log.update(status="publicRunFailed", wallSeconds=round(time.monotonic() - started, 3), modelCalls=0)
        save(root / "result.json", log)
        return log
    # Paused Coach cannot block OJ/T074. No model is called by this extension.
    service.command(session, {"operationId": "pause-for-direct-submit", "kind": "pause",
                              "codeVersion": code_version, "payload": {}})
    submission, _ = service.editor.submissions.create_submission({
        "goalContext": {"goalId": goal_id, "scope": "goal"}, "problemRef": problem_ref,
        "codeVersion": code_version, "language": "python3", "idempotencyKey": "comparison-submit"})
    queue = WorkerQueue(service.path)
    results = OJResultRepository(service.path)
    judge = JudgeSubmissionProcessor(runner, SQLiteTrustedAssetProvider(service.path), results)
    submit_start = time.monotonic()
    Worker(queue, judge, "comparison-real-oj", root / "oj-workspaces").process_once()
    log.update(submission=service.editor.submissions.get_submission(submission["submissionId"]),
               ojResult=results.get_for_submission(submission["submissionId"]),
               submissionSeconds=round(time.monotonic() - submit_start, 3))
    log["events"] = service.process_events()
    log["completion"] = service.completion(goal_id)
    log["goalState"] = service.final.goal_state(goal_id)
    saved_submission = log["submission"]
    oj = log["ojResult"] or {}
    completion = log["completion"]
    state = log["goalState"] or {}
    snapshot = service.editor.submissions.get_code_snapshot(goal_id, code_version)
    final_report = completion.get("report") or {}
    refs = final_report.get("inputVersionRefs", {})
    expected_refs = {"codeVersion": code_version, "codeHash": snapshot["contentHash"],
                     "submissionId": submission["submissionId"], "ojResultId": oj.get("ojResultId"),
                     "problemId": problem_ref["problemId"], "problemVersion": problem_ref["problemVersion"],
                     "assetId": asset["assetId"], "assetVersion": asset["assetVersion"],
                     "materialHash": material["materialHash"]}
    log["finalChecks"] = {
        "submissionFinished": saved_submission["status"] == "finished" and saved_submission["codeVersion"] == code_version,
        "ojAccepted": oj.get("verdict") == "AC" and oj.get("codeVersion") == code_version
            and oj.get("submissionId") == submission["submissionId"] and oj.get("problemRef") == problem_ref,
        "currentCodeCompleted": completion.get("status") == "completed"
            and completion.get("currentCodeVersion") == code_version and completion.get("codeVersion") == code_version
            and completion.get("codeHash") == snapshot["contentHash"],
        "finalReportMatches": final_report.get("status") == "verified" and final_report.get("reason") == "TRUSTED_AC"
            and all(refs.get(key) == value for key, value in expected_refs.items()),
        "goalStateMatches": state.get("status") == "completed" and bool(completion.get("resultId"))
            and state.get("lastVerifierResultId") == completion.get("resultId"),
    }
    log.update(status="passed" if all(log["finalChecks"].values()) else "finalVerificationIncomplete",
               wallSeconds=round(time.monotonic() - started, 3), modelCalls=0)
    save(root / "result.json", log)
    return log

"""Bounded, isolated old/new Coach comparison through the production service/worker.

Real calls require --run. Default mode validates and freezes the cases only.
Published output is a candidate for human review, not automatically useful/correct.
Failures are retained, and the script never presses the user retry button.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from apps.api.deepseek_provider import DeepSeekConfig, load_local_env
from focusmate.adapters.coach_chat import DeepSeekCoachProvider
from services.coach_worker.worker import CoachWorker
from tools.experiments.t082_backend_probe import seed

ARTIFACT_ROOT = ROOT / ".artifacts"
CASES_PATH = Path(__file__).with_name("t082_heldout_20261014.json")


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_destination(path):
    target = Path(path).resolve()
    if not target.is_relative_to(ARTIFACT_ROOT.resolve()) or target == ARTIFACT_ROOT.resolve():
        raise ValueError("Comparison artifacts must use a fresh child of the isolated artifact directory")
    if target.exists():
        raise ValueError("Refusing to overwrite an earlier comparison")
    return target


def read_cases(path):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if len(cases) != 3 or len({case["name"] for case in cases}) != 3:
        raise ValueError("The bounded comparison requires three distinct frozen cases")
    required = {"name", "journey", "statement", "inputSpec", "outputSpec", "constraints",
                "examples", "code", "fixed", "ask", "check"}
    for case in cases:
        if not required <= case.keys() or not case["examples"]:
            raise ValueError("Incomplete held-out case")
        for key in ("code", "fixed"):
            compile(case[key], "<held-out-fixture>", "exec")
        if case["code"] == case["fixed"]:
            raise ValueError("The fixed source must be a distinct snapshot")
    return cases


class RecordedProvider:
    """Keep isolated input/output evidence, token usage and every failed call."""
    def __init__(self, provider, log):
        self.provider = provider
        self.public_config = deepcopy(provider.public_config)
        self.log = log
        self.calls = []
        self.turn = None
        self.fatal_failure = None

    def complete(self, **kwargs):
        start = time.monotonic()
        entry = {"index": len(self.calls) + 1, "turn": self.turn,
                 "startedAt": datetime.now(timezone.utc).isoformat(),
                 "messages": deepcopy(kwargs["messages"]), "tools": deepcopy(kwargs["tools"])}
        self.calls.append(entry)
        write_json(self.log, self.calls)
        try:
            response = self.provider.complete(**kwargs)
            entry.update(response=deepcopy(response), usage=deepcopy(response.get("usage", {})))
            return response
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            if code in ("PROVIDER_PAYMENT_REQUIRED", "PROVIDER_CONFIGURATION_INVALID"):
                self.fatal_failure = code
            entry.update(failure=code,
                         responseDiagnostic=deepcopy(getattr(exc, "response_diagnostic", None)),
                         responseAudit=deepcopy(getattr(exc, "response_audit", None)))
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - start, 3)
            write_json(self.log, self.calls)


def visible_candidate(view, round_id):
    """Count only persisted user-visible evidence; placeholders never count."""
    for item in view.get("items", []):
        if item.get("roundId") != round_id:
            continue
        if item.get("kind") == "action" and item.get("content", "").strip():
            return {"kind": "publishedHelp", "id": item.get("actionId")}
        if item.get("kind") in ("check", "report", "checkResult"):
            return {"kind": "formalCheck", "id": item.get("reportId", item.get("batchId"))}
    round_view = view.get("round") or {}
    if round_view.get("roundId") == round_id:
        summary = round_view.get("checkSummary") or {}
        if summary.get("savedReportCount", 0) or summary.get("reportIds"):
            return {"kind": "formalCheck", "id": round_view.get("batchId")}
    return None


def turn(service, worker, provider, session, folder, label, kind, payload):
    started = time.monotonic()
    before_calls = len(provider.calls)
    provider.turn = label
    current = service.synchronize(session)
    command = {"operationId": label, "kind": kind, "codeVersion": current["codeVersion"], "payload": payload}
    result = {"label": label, "command": command, "startedAt": datetime.now(timezone.utc).isoformat(),
              "firstVisibleCandidateSeconds": None, "candidate": None, "humanReview": None,
              "steps": [], "userRetryCount": 0}
    service.command(session, command)
    round_id = service.manager.read(session)["currentRoundId"]
    result["roundId"] = round_id
    for _ in range(32):
        tick = time.monotonic()
        step_id = worker.process_once()
        view = service.read(session)
        state = service.manager.read(session)
        if step_id:
            step = state["steps"].get(step_id, {})
            result["steps"].append({"stepId": step_id, "kind": step.get("kind"),
                                    "seconds": round(time.monotonic() - tick, 3),
                                    "status": step.get("status"),
                                    "repairReason": step.get("payload", {}).get("repairReason")})
        candidate = visible_candidate(view, round_id)
        if candidate and result["firstVisibleCandidateSeconds"] is None:
            result.update(firstVisibleCandidateSeconds=round(time.monotonic() - started, 3), candidate=candidate)
        result.update(view=view, state=state, workerFailures=deepcopy(worker.failures),
                      wallSeconds=round(time.monotonic() - started, 3))
        write_json(folder / (label + ".json"), result)
        round_state = state["rounds"][round_id]
        if not step_id or round_state["status"] not in ("queued", "processing"):
            break
    calls = provider.calls[before_calls:]
    r = result["state"]["rounds"][round_id]
    result.update(status=r["status"], failure=r.get("failure"), counts=r["counts"],
                  workflowVersion=r.get("workflowVersion", 1), modelCalls=len(calls),
                  modelSeconds=round(sum(x["seconds"] for x in calls), 3),
                  tokens={key: sum(x.get("usage", {}).get(key, 0) for x in calls)
                          for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                  formatRepairCount=sum(bool(s["repairReason"]) for s in result["steps"]),
                  queueSeconds=round(max(0, (r.get("startedAt") or r["createdAt"]) - r["createdAt"]), 3))
    write_json(folder / (label + ".json"), result)
    return result


def compact_turn(result):
    return {key: result[key] for key in ("label", "status", "failure", "workflowVersion", "counts",
        "firstVisibleCandidateSeconds", "candidate", "wallSeconds", "modelCalls", "modelSeconds",
        "tokens", "formatRepairCount", "queueSeconds", "humanReview", "userRetryCount")}


def run_case(root, case, repetition, version, config, docker=False):
    folder = root / (case["name"] + "-" + str(repetition) + "-v" + str(version))
    service, goal, session = seed(folder, case)
    service.manager.workflow_version = version
    provider = RecordedProvider(DeepSeekCoachProvider(config), folder / "model-calls.json")
    worker = CoachWorker(service, provider)
    first = turn(service, worker, provider, session, folder, "ask", "ask", {"text": case["ask"]})
    targets = [g["subgoalId"] for g in first["view"]["workGoals"] if g["status"] == "active"]
    turns = [first]
    if not provider.fatal_failure:
        service.editor.save_draft(goal, {"sourceCode": case["fixed"], "expectedRevision": 1, "operationId": "fix"})
        service.synchronize(session)
        # A real "check this step" button sends selected targets, not an extra
        # natural-language request that could change their meaning.
        payload = {"subgoalIds": targets} if targets else {"text": case["check"], "subgoalIds": []}
        turns.append(turn(service, worker, provider, session, folder, "check", "check", payload))
    if case.get("nextQuestion") and not provider.fatal_failure:
        turns.append(turn(service, worker, provider, session, folder, "new-question", "message",
                          {"text": case["nextQuestion"]}))
    summary = {"case": case["name"], "journey": case["journey"], "repetition": repetition,
               "workflowVersion": version, "directory": str(folder), "model": provider.public_config,
               "targetsCreatedByFirstTurn": targets, "turns": [compact_turn(x) for x in turns],
               "fatalFailure": provider.fatal_failure}
    if docker and not provider.fatal_failure:
        from tools.experiments.t082_workflow_docker import exercise
        try:
            summary["docker"] = exercise(service, goal, session, case, folder)
        except Exception as exc:
            summary["docker"] = {"status": "failed", "errorType": type(exc).__name__,
                                  "failure": getattr(exc, "code", str(exc))[:1000]}
    write_json(folder / "summary.json", summary)
    return summary


def rollup(results):
    summaries = {}
    for version in (1, 2):
        turns = [turn for case in results if case["workflowVersion"] == version for turn in case["turns"]]
        visible = [t["firstVisibleCandidateSeconds"] for t in turns if t["firstVisibleCandidateSeconds"] is not None]
        summaries[str(version)] = {
            "turnCount": len(turns), "visibleCandidateCount": len(visible),
            "humanAcceptedCount": sum(t["humanReview"] is True for t in turns),
            "visibleCandidateMedianSeconds": statistics.median(visible) if visible else None,
            "allTurnWaitSeconds": [t["wallSeconds"] for t in turns],
            "failures": [{k: t[k] for k in ("label", "status", "failure", "wallSeconds")}
                         for t in turns if t["status"] != "finished"],
            "modelCalls": sum(t["modelCalls"] for t in turns),
            "totalTokens": sum(t["tokens"]["total_tokens"] for t in turns),
        }
    return {"scope": "Published output still requires human review; not a success rate or learning benefit claim.",
            "versions": summaries, "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--workflow", choices=("1", "2", "both"), default="both")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--docker", action="store_true", help="Real run, human-reference material check, OJ and T074 on case 1 repetition 1")
    args = parser.parse_args()
    root = validate_destination(args.root)
    cases = read_cases(args.cases)
    root.mkdir(parents=True)
    write_json(root / "cases.json", cases)
    manifest = {"createdAt": datetime.now(timezone.utc).isoformat(), "realCallsEnabled": args.run,
                "baselineCommit": "17e3913", "caseHash": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                "gitHead": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "implementationHash": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD", "--", "src", "apps", "services"], cwd=ROOT)).hexdigest(),
                "workflow": args.workflow, "dockerRequested": args.docker,
                "scope": "Isolated developer problems, source edits and manual expected outputs. Same provider config and no Coach execution permissions in both arms. No user database and no user-retry. Existing v1 compatibility path is the baseline arm; code hash is pinned separately. Human review is required for useful/correct output."}
    write_json(root / "manifest.json", manifest)
    if not args.run:
        print(json.dumps({"status": "validatedOnly", "cases": len(cases), "manifest": str(root / "manifest.json")}))
        return
    if not args.env_file:
        raise ValueError("Real calls require an explicitly selected env file")
    from focusmate.core.dynamic_coach import DynamicCoachManager
    if "workflow_version" not in inspect.signature(DynamicCoachManager).parameters:
        raise ValueError("The versioned Manager is not ready; no real calls started")
    load_local_env(args.env_file)
    config = replace(DeepSeekConfig.from_environment(), max_tokens=8192)
    results = []
    for index, case in enumerate(cases):
        for repetition in (1, 2):
            # Alternate arm order to reduce warm-cache/time-of-day bias.
            versions = ([1, 2] if repetition == 1 else [2, 1]) if args.workflow == "both" else [int(args.workflow)]
            for version in versions:
                result = run_case(root, case, repetition, version, config,
                                  docker=args.docker and index == 0 and repetition == 1)
                results.append(result)
                write_json(root / "summary.json", rollup(results))
                print(json.dumps({k: result[k] for k in ("case", "repetition", "workflowVersion", "turns")}, ensure_ascii=False), flush=True)
                if result["fatalFailure"]:
                    print(json.dumps({"stopped": result["fatalFailure"], "remainingCasesSkipped": True}), flush=True)
                    return


if __name__ == "__main__":
    main()

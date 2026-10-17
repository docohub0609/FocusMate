"""Frozen T062 stage/local-run evaluation; semantic judgments name the actual reviewer."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "t062" / "stage-local-request-cases.json"
STAGES = {"draft", "strategy_hypothesis", "implementation_in_progress", "test_ready", "unknown"}
SAFE_CONFIG = {"provider", "model", "maxTokens", "thinkingEnabled", "reasoningEffort",
               "timeoutSeconds", "networkAttempts", "promptVersion"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def _expand(spec):
    value = deepcopy(spec)
    if value['group'] in ('development', 'holdout'):
        value['input'].setdefault('mode', 'combined')  # explicit legacy replay only
    raw = value["input"].pop("materials")
    goal = value["input"]["goalContext"]
    case_id = value["id"]
    value["input"]["problemRef"] = {"problemId": "problem-" + case_id, "problemVersion": 1}
    records = []
    kinds = (("g", "goal", raw["goal"]), ("p", "problemFact", raw["problem"]),
             ("c", "code", {"snapshotRef": "c01", "language": "python3",
                              "text": raw["code"]}))
    for prefix, kind, content in kinds:
        records.append({"ref": f"{prefix}01", "goalContext": goal, "kind": kind,
                        "version": value["input"]["codeVersion"] if kind == "code" else 1,
                        "content": content})
    for index, observation in enumerate(raw.get("observations", []), 1):
        records.append({"ref": f"o{index:02d}", "goalContext": goal,
                        "kind": "observation", "version": index, "content": observation})
    if raw.get("userStatement"):
        records.append({"ref": "u01", "goalContext": goal, "kind": "dialogue",
                        "version": 1, "sequence": 1, "turnId": f"turn-{case_id}",
                        "content": {"ref": "u01", "role": "user",
                                    "text": raw["userStatement"]}})
    for index, run in enumerate(raw.get("runs", []), 1):
        records.append({"ref": f"r{index:02d}", "goalContext": goal,
                        "kind": "runEvidence", "version": index, "content": run})
    for previous in raw.get('historicalCodes', []):
        records.append({'ref': previous['ref'], 'goalContext': goal, 'kind': 'code',
                        'version': previous['version'], 'content': {
                            'snapshotRef': previous['ref'], 'language': 'python3', 'text': previous['text']}})
    for record in records:
        record["contentHash"] = "sha256:" + _digest(record["content"])
    ref_map = {f"g-{case_id}": "g01", f"p-{case_id}": "p01", f"c-{case_id}": "c01",
               f"u-{case_id}": "u01"}
    ref_map.update({f"o-{case_id}-{index}": f"o{index:02d}"
                    for index in range(1, len(raw.get("observations", [])) + 1)})
    ref_map.update({f"r-{case_id}-{index}": f"r{index:02d}"
                    for index in range(1, len(raw.get("runs", [])) + 1)})
    value["expected"]["factRefs"] = [ref_map[ref] for ref in value["expected"]["factRefs"]]
    value["input"]["records"] = records
    refs = {record["kind"]: [] for record in records}
    for record in records:
        refs[record["kind"]].append(record["ref"])
    value["input"]["stageRequest"] = {
        "schemaVersion": "1.0.0", "goalContext": goal,
        "goalRef": refs["goal"][0], "problemRequirementRef": refs["problemFact"][0],
        "codeSnapshotRef": refs["code"][0], "observationRefs": refs.get("observation", []),
        "historicalCodeRefs": refs['code'][1:],
        "userStatementRefs": refs.get("dialogue", []), "runRecordRefs": refs.get("runEvidence", []),
        "dialogueRefs": refs.get("dialogue", []),
        "currentVersions": {record["ref"]: record["version"] for record in records},
        "budget": {"contextWindowTokens": 16000, "reservedOutputTokens": 4096,
                   "safetyMarginTokens": 1000}}
    return value


def load_cases(path=None):
    raw = json.loads(Path(path or FIXTURE).read_text(encoding="utf-8"))
    return [_expand(case) for case in raw]


def _validate(cases):
    if not cases or len({case.get('id') for case in cases}) != len(cases):
        raise ValueError('unique nonempty cases required')
    if any(case.get('group') not in ('development', 'holdout', 'regression', 'new') for case in cases):
        raise ValueError('invalid case group')
    seen_inputs = set()
    for case in cases:
        case_input, expected = case.get("input"), case.get("expected")
        if not isinstance(case_input, dict) or not isinstance(expected, dict):
            raise ValueError("input and expected are required")
        serialized = json.dumps(case_input, ensure_ascii=False, sort_keys=True)
        if serialized in seen_inputs:
            raise ValueError("case inputs must be distinct")
        seen_inputs.add(serialized)
        if any(word in serialized for word in ("shouldRunLocal", "mustExplain", "forbiddenConclusions", "stageLabel")):
            raise ValueError("expectation leaked into model input")
        refs = [record.get("ref", "") for record in case_input.get("records", [])]
        if len(refs) != len(set(refs)) or not all(re.fullmatch(r"[gpcour][0-9]{2}", ref) for ref in refs):
            raise ValueError("references must be unique and neutral")
        mode = case_input.get('mode')
        if mode not in ('stage_only', 'assist_only', 'combined'):
            raise ValueError('invalid task mode')
        if (mode != 'assist_only' and expected.get('stage') not in STAGES or
                mode == 'assist_only' and expected.get('stage') is not None or
                mode == 'stage_only' and expected.get('shouldRunLocal') is not None or
                mode != 'stage_only' and type(expected.get('shouldRunLocal')) is not bool):
            raise ValueError('invalid applicable judgment')
        if not set(expected.get("factRefs", [])) <= set(refs):
            raise ValueError("expected facts must cite supplied records")
        if not expected.get("reasoning") or not expected.get("mustExplain") or not expected.get("forbiddenConclusions"):
            raise ValueError("manual expectations require grounded reasoning and boundaries")


def freeze_experiment(cases, config, group="all"):
    cases = deepcopy(cases)
    _validate(cases)
    if group not in ("development", "holdout", "regression", "new", "all"):
        raise ValueError("invalid group")
    try:
        from focusmate.core.stage_recognition import STAGE_PROMPT, STAGE_PROMPT_VERSION
        from focusmate.core.tool_dialogue import TOOL_PROMPT
        from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
        from tools.stage_local_demo import LOCAL_GUIDANCE
    except ImportError as error:
        raise RuntimeError("T062 production entrypoints are unavailable") from error
    selected = [case for case in cases if group == "all" or case["group"] == group]
    if not selected: raise ValueError("no cases selected")
    schedule = []
    for case in selected:
        repeats = (1,) if case["group"] == "development" else (1, 2)
        schedule.extend({"caseId": case["id"], "repeat": repeat,
                         "recordId": f"{case['id']}-{repeat}"} for repeat in repeats)
    patterns = ("src/focusmate/core/stage_recognition.py", "src/focusmate/core/code_materials.py",
                "schemas/stages/stage-recognition-report-v2.schema.json", "src/focusmate/core/tool_dialogue.py",
                "src/focusmate/core/tool_gateway.py", "src/focusmate/contracts/tool_calls.py",
                "src/focusmate/adapters/stage_recognition.py", "src/focusmate/adapters/tool_storage.py",
                "services/oj_worker/local_runs.py", "tools/stage_local_demo.py",
                "tools/stage_local_evaluation.py")
    sources = {name: (ROOT / name).read_text(encoding="utf-8")
               for name in patterns if (ROOT / name).is_file()}
    hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()}
    manifest = {"version": "t062-request-evaluation-2", "group": group,
                "stagePromptVersion": STAGE_PROMPT_VERSION, "stagePrompt": STAGE_PROMPT,
                "toolPrompt": TOOL_PROMPT, "guidance": LOCAL_GUIDANCE, "toolDefinitions": deepcopy(TOOL_DEFINITIONS),
                "modelConfig": ({name: {key: deepcopy(item) for key, item in value.items()
                                         if key in SAFE_CONFIG}
                                 for name, value in config.items() if isinstance(value, dict)}
                                if any(isinstance(value, dict) for value in config.values())
                                else {key: deepcopy(value) for key, value in config.items() if key in SAFE_CONFIG}),
                "cases": cases, "schedule": schedule, "implementationHashes": hashes,
                "implementationSnapshots": sources,
                "rubric": ["stageJudgment", "reasonSupport", "toolChoice", "answerCorrectness",
                           "evidenceVersion", "coachingScope"], "semanticReviewStatus": "pending", "cost": None}
    manifest["contentHash"] = _digest(manifest)
    return manifest


def run_evaluation(cases, runner, output, config=None, group="all"):
    manifest = freeze_experiment(cases, config or {}, group)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "records").mkdir()
    _save(output / "manifest.json", manifest)
    indexed = {case["id"]: case for case in manifest["cases"]}
    counts = Counter()
    for entry in manifest["schedule"]:
        directory = output / "records" / entry["recordId"]
        directory.mkdir()
        started = time.monotonic()
        try:
            report = runner(deepcopy(indexed[entry["caseId"]]["input"]), directory)
            if not isinstance(report, dict) or not isinstance(report.get("status"), str):
                raise ValueError("invalid runner report")
        except Exception as error:
            report = {"status": "runner_failed", "errorType": type(error).__name__}
        counts[report["status"]] += 1
        _save(output / "records" / f"{entry['recordId']}.json",
              {**entry, "durationSeconds": round(time.monotonic() - started, 6),
               "report": report, "semanticReviewStatus": "pending", "accepted": None})
        print(f"{entry['recordId']}: {report['status']}", flush=True)
    summary = {"counts": dict(counts), "scheduledCount": len(manifest["schedule"]),
               "semanticReviewStatus": "pending", "contentAccepted": None, "cost": None,
               "note": "运行完成不代表阶段判断或解释在语义上通过，需对照冻结预期复核并注明实际复核者。"}
    _save(output / "summary.json", summary)
    return summary

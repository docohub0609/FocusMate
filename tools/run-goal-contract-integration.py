"""Real T041 generation and T042 lifecycle in isolated temporary databases."""
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
for location in (ROOT, ROOT / "src"):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))


def _update_owned_file(target, value):
    """Update only a file handle this run has exclusively created."""
    rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    target.seek(0)
    target.write(rendered)
    target.truncate()
    target.flush()


def _goal_rows(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute("SELECT * FROM goal_records ORDER BY goal_id").fetchall()


def run_examples(output_dir: Path, compiler, reviewer) -> int:
    names = ("two-sum", "graph-connectivity", "merge-intervals")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any((output_dir/name).exists() for name in ["summary.json", *(n+".json" for n in names),
                                                 *(n+".jsonl" for n in names)]):
        raise FileExistsError("Existing results")
    summary = []
    with (output_dir/"summary.json").open("x", encoding="utf-8") as summary_file:
        _update_owned_file(summary_file, summary)
        for name in names:
            with (output_dir/(name+".json")).open("x", encoding="utf-8") as case_file, \
                    (output_dir/(name+".jsonl")).open("x", encoding="utf-8") as checkpoint_log:
                summary.append({})

                def checkpoint(item):
                    # Append before updating snapshots. A process interruption while
                    # writing either JSON file leaves earlier complete log entries.
                    checkpoint_log.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                    checkpoint_log.flush()
                    os.fsync(checkpoint_log.fileno())
                    _update_owned_file(case_file, item)
                    summary[-1] = {key: value for key, value in item.items()
                                   if key not in {"steps", "generation"}}
                    _update_owned_file(summary_file, summary)

                item = _run_example(name, compiler, reviewer, checkpoint)
                print(f"{name}: {item['status']} ({item.get('reason', item.get('errorCode','checked'))})", flush=True)
    return 0 if all(item["completed"] and item["status"] == "passed" for item in summary) else 2


def _run_example(name, compiler, reviewer, checkpoint):
    from tools.goal_compilation_examples import seed_example
    from focusmate.core.goal_compilation import compile_goal_contract
    from apps.api.goal_contract_storage import GoalContractRepository

    started = time.monotonic()
    item = {"example": name, "status": "failed", "steps": {}, "completed": False}

    def save(phase):
        item["phase"] = phase
        item["elapsedSeconds"] = round(time.monotonic()-started, 2)
        checkpoint(item)

    save("initializing")
    with TemporaryDirectory(prefix="focusmate-t042-") as folder:
        db_path = Path(folder)/"example.db"
        before = None
        try:
            goal, problem, features = seed_example(db_path, name)
            before = _goal_rows(db_path)
            save("generation")
            compilation = compile_goal_contract(goal, problem, features, compiler,
                created_at=datetime.now(timezone.utc).isoformat())
            item["generation"] = compilation
            save("generationCompleted")
            repo = GoalContractRepository(db_path)
            draft = repo.import_draft(goal["goalId"], compilation)
            save("initialReview")
            frozen = repo.review_and_freeze(goal["goalId"], draft["version"], reviewer,
                                           expected_draft_revision=draft["draftRevision"])
            item["steps"]["initialReview"] = frozen
            save("initialReviewCompleted")
            if frozen["status"] != "frozen":
                item["reason"] = "initialDraftNeedsReview"
            elif name == "two-sum":
                original = repo.export_editable(goal["goalId"], frozen["version"])
                newer = repo.create_revision(goal["goalId"], frozen["version"], "验证错误要求被拦截及修改后重审")
                bad = deepcopy(original)
                bad["subgoals"].append({
                    "key": "new:incorrect-values", "stage": "problemUnderstanding",
                    "objective": "返回的两个位置上的数值必须不同。",
                    "userGoal": "检查返回的两个位置上的数值是否不同。",
                    "acceptableEvidence": ["evaluatedUserExplanation"],
                    "sourceRefs": ["statement:1"],
                    "checks": [{"check": "两个位置上的数值必须不同。", "sourceRefs": ["statement:1"]}],
                    "dependencies": [],
                })
                edited = repo.edit_draft(goal["goalId"], newer["version"], bad,
                                         expected_draft_revision=newer["draftRevision"])
                save("incorrectRevisionReview")
                rejected = repo.review_and_freeze(goal["goalId"], newer["version"], reviewer,
                                                   expected_draft_revision=edited["draftRevision"])
                item["steps"]["incorrectRevision"] = rejected
                save("incorrectRevisionReviewCompleted")
                current = repo.get_current(goal["goalId"])
                last_review = rejected["reviews"][-1]["result"] if rejected["reviews"] else {}
                inserted_ids = {node['subgoalId'] for node in edited['compilation']['contract']['subgoals']} - {
                    node['subgoalId'] for node in frozen['compilation']['contract']['subgoals']}
                detected = last_review.get('status') == 'needsReview' and any(
                    issue.get('type') in {'conditionMismatch', 'objectMismatch'}
                    and (issue.get('subgoalId') in inserted_ids or issue.get('itemId') in {
                        'source:statement:0', 'source:statement:1'})
                    for issue in last_review.get('issues', []))
                if last_review.get('status') == 'unavailable':
                    item['reason'] = 'incorrectRevisionReviewUnavailable'
                elif not detected:
                    item['reason'] = 'incorrectRevisionNotDetected'
                elif rejected["status"] != "draft" or current["version"] != frozen["version"]:
                    item["reason"] = "incorrectRevisionNotBlocked"
                else:
                    repaired = repo.edit_draft(goal["goalId"], newer["version"], original,
                                               expected_draft_revision=rejected["draftRevision"])
                    save("correctedRevisionReview")
                    adopted = repo.review_and_freeze(goal["goalId"], newer["version"], reviewer,
                                                    expected_draft_revision=repaired["draftRevision"])
                    item["steps"]["correctedRevision"] = adopted
                    save("correctedRevisionReviewCompleted")
                    old = repo.get_version(goal["goalId"], frozen["version"])
                    item["oldVersionStillReadable"] = (old["status"] == "superseded"
                        and old["compilation"] == frozen["compilation"])
                    if adopted["status"] == "frozen" and item["oldVersionStillReadable"]:
                        item["status"] = "passed"
                    else:
                        item["reason"] = "correctedRevisionNeedsReview"
            else:
                item["status"] = "passed"
        except (KeyboardInterrupt, SystemExit):
            item.update(status="failed", interrupted=True)
            if before is not None:
                item["goalRowsUnchanged"] = before == _goal_rows(db_path)
            save(item["phase"])
            raise
        except Exception as error:
            item["errorCode"] = getattr(error, "code", type(error).__name__)
        if before is not None:
            item["goalRowsUnchanged"] = before == _goal_rows(db_path)
            if not item["goalRowsUnchanged"]:
                item["status"] = "failed"
        item["completed"] = True
        save("finished")
    return item


def main(argv=None):
    parser=argparse.ArgumentParser(description="真实模型验证合同审核、冻结与升版，使用临时数据库。")
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--env-file",type=Path)
    args=parser.parse_args(argv)
    try:
        if args.env_file:
            from apps.api.deepseek_provider import load_local_env
            load_local_env(args.env_file)
        from focusmate.adapters.goal_compilation import DeepSeekGoalCompilationProvider
        from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
        return run_examples(args.output_dir,DeepSeekGoalCompilationProvider.from_environment(),
                            DeepSeekGoalContractReviewProvider.from_environment())
    except FileExistsError:
        print("请指定没有既有结果的输出目录。")
    except Exception as error:
        print(f"验证未启动：{getattr(error,'code',type(error).__name__)}。")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

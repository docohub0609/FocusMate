"""Compile an existing Goal without modifying its database or progress."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
for location in (ROOT, ROOT / "src"):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from focusmate.adapters.goal_compilation import DeepSeekGoalCompilationProvider
from focusmate.contracts.goal_compilation import GoalCompilationError
from focusmate.core.goal_compilation import compile_goal_contract
from apps.api.deepseek_provider import load_local_env


def load_inputs(path: Path, goal_id: str) -> tuple[dict, dict, dict]:
    """Read all three objects in one SQLite snapshot; never initialize repositories."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            goal_row = db.execute("SELECT * FROM goal_records WHERE goal_id=?", (goal_id,)).fetchone()
            if goal_row is None:
                raise ValueError("missing goal")
            problem_row = db.execute(
                "SELECT * FROM problem_versions WHERE problem_id=? AND version=?",
                (goal_row["problem_id"], goal_row["problem_version"]),
            ).fetchone()
            if problem_row is None:
                raise ValueError("missing problem")
            feature_row = db.execute(
                "SELECT result_json FROM problem_feature_sets WHERE feature_set_id=?",
                (problem_row["confirmed_feature_set_id"],),
            ).fetchone()
            if feature_row is None:
                raise ValueError("missing confirmed feature report")
            examples = db.execute(
                "SELECT input_text, output_text FROM problem_examples WHERE problem_id=? AND version=? ORDER BY example_index",
                (goal_row["problem_id"], goal_row["problem_version"]),
            ).fetchall()
            problem_ref = {"problemId": goal_row["problem_id"], "problemVersion": goal_row["problem_version"]}
            goal = {"goalId": goal_row["goal_id"], "schemaVersion": goal_row["schema_version"],
                    "createdAt": goal_row["created_at"], "problemRef": problem_ref, "status": goal_row["status"]}
            problem = {
                "problemId": problem_row["problem_id"], "schemaVersion": problem_row["schema_version"],
                "version": problem_row["version"], "createdAt": problem_row["created_at"], "source": "userProvided",
                "statement": problem_row["statement"], "inputSpec": problem_row["input_spec"],
                "outputSpec": problem_row["output_spec"], "constraints": problem_row["constraints_text"],
                "examples": [{"input": row[0], "output": row[1]} for row in examples],
                "languagePolicy": json.loads(problem_row["language_policy_json"]), "ioMode": problem_row["io_mode"],
                "verifierAssetStatus": problem_row["verifier_asset_status"],
            }
            lifecycle = {"status": problem_row["lifecycle_status"], "draftRevision": problem_row["draft_revision"],
                         "contentHash": problem_row["content_hash"], "updatedAt": problem_row["updated_at"]}
            if problem_row["frozen_at"]:
                lifecycle.update(frozenAt=problem_row["frozen_at"], confirmedFeatureSetId=problem_row["confirmed_feature_set_id"])
            return goal, {"problemPackage": problem, "lifecycle": lifecycle}, json.loads(feature_row["result_json"])
    except (sqlite3.Error, ValueError, KeyError, IndexError, OSError, TypeError):
        raise GoalCompilationError("GOAL_INPUT_UNAVAILABLE", "无法读取这次做题对应的已确认题目。") from None


def render_result(result: dict, *, show_internal: bool = False) -> str:
    lines = ["已生成目标草稿（尚未冻结，尚未验证用户进度）："]
    lines.extend(f"{index}. {goal['text']}" for index, goal in enumerate(result["publicGoals"], 1))
    if show_internal:
        lines.extend(["", "内部开发记录（模型判断未经语义验证）：", json.dumps(result, ensure_ascii=False, indent=2)])
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, provider=None) -> int:
    parser = argparse.ArgumentParser(description="只读已有题目，生成题目相关的 Goal 合同草稿。")
    parser.add_argument("--db", type=Path, required=True, help="已有 SQLite 数据库")
    parser.add_argument("--goal-id", required=True, help="已有 Goal 编号")
    parser.add_argument("--env-file", type=Path, help="显式读取本机模型配置，不复制或输出密钥")
    parser.add_argument("--show-internal", action="store_true", help="显式显示内部策略与检查细则，仅供开发审核")
    args = parser.parse_args(argv)
    try:
        goal, problem, features = load_inputs(args.db, args.goal_id)
        if args.env_file:
            try:
                load_local_env(args.env_file)
            except (OSError, ValueError):
                raise GoalCompilationError('GOAL_CONFIG_UNAVAILABLE', '无法读取模型配置。') from None
        active_provider = provider if provider is not None else DeepSeekGoalCompilationProvider.from_environment()
        result = compile_goal_contract(goal, problem, features, active_provider,
                                       created_at=datetime.now(timezone.utc).isoformat())
        print(render_result(result, show_internal=args.show_internal))
        return 0
    except GoalCompilationError as error:
        print(f"生成未完成：{error.code}。请检查输入或模型服务；现有题目和进度未修改。")
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

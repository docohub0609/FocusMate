"""Generate three independent, paid model examples in temporary databases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
for location in (ROOT, ROOT / "src"):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.goal_compilation import DeepSeekGoalCompilationProvider
from focusmate.contracts.goal_compilation import GoalCompilationError
from focusmate.core.goal_compilation import compile_goal_contract
from tools.goal_compilation_examples import EXAMPLES, seed_example


def run_cases(output_dir: Path, provider) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / f"{name}.json" for name in EXAMPLES] + [output_dir / "summary.json"]
    if any(path.exists() for path in paths):
        raise FileExistsError("请指定一个没有既有演示结果的目录。")
    spec = importlib.util.spec_from_file_location("goal_demo", ROOT / "tools/demo-goal-compilation.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    summary = []
    for name in EXAMPLES:
        started = time.monotonic()
        with TemporaryDirectory(prefix="focusmate-t041-") as temporary:
            db_path = Path(temporary) / "example.db"
            goal, _, _ = seed_example(db_path, name)
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()
            inputs = demo.load_inputs(db_path, goal["goalId"])
            item = {"example": name, "goalId": goal["goalId"]}
            try:
                result = compile_goal_contract(*inputs, provider, created_at=datetime.now(timezone.utc).isoformat())
                encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                with (output_dir / f"{name}.json").open("x", encoding="utf-8") as target:
                    target.write(encoded + "\n")
                item.update(status="generated", model=result["generation"]["model"],
                            nodeCount=len(result["contract"]["subgoals"]),
                            candidateCount=len(result["strategyCandidates"]),
                            resultHash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                            semanticReview="pending")
                print(f"{name}：生成 {item['nodeCount']} 个具体目标、{item['candidateCount']} 个候选，待人工审核。", flush=True)
            except GoalCompilationError as error:
                item.update(status="failed", code=error.code)
                print(f"{name}：生成未完成（{error.code}）。", flush=True)
            item["databaseUnchanged"] = before == hashlib.sha256(db_path.read_bytes()).hexdigest()
            item["elapsedSeconds"] = round(time.monotonic() - started, 2)
            summary.append(item)
    with (output_dir / "summary.json").open("x", encoding="utf-8") as target:
        json.dump(summary, target, ensure_ascii=False, indent=2)
        target.write("\n")
    return 0 if all(item["status"] == "generated" and item["databaseUnchanged"] for item in summary) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用三道开发验收题真实调用模型，生成结果与待审核记录。")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, help="显式读取本机模型配置文件，不复制或输出密钥")
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            try:
                load_local_env(args.env_file)
            except (OSError, ValueError):
                raise GoalCompilationError('GOAL_CONFIG_UNAVAILABLE', '无法读取模型配置。') from None
        return run_cases(args.output_dir, DeepSeekGoalCompilationProvider.from_environment())
    except GoalCompilationError as error:
        print(f"演示未启动：{error.code}。")
        return 1
    except FileExistsError:
        print("输出目录已有演示结果，请选择新目录以保留历史记录。")
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

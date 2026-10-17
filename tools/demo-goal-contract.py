"""Inspect and operate versioned Goal contracts; never verify learning progress."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
for location in (ROOT, ROOT / "src"):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))


def render_record(record: dict, *, show_goals: bool = False, show_internal: bool = False) -> str:
    from focusmate.core.goal_contract_review import ISSUE_MESSAGES
    states = {"draft": "草稿", "frozen": "正式版", "superseded": "已由新版接替"}
    reviews_text = {"passed": "审核通过", "needsReview": "仍有内容需要核对", "unavailable": "未取得有效审核结果"}
    status = record['status']
    lines = [f"合同版本 {record['version']}，草稿修改序号 {record['draftRevision']}，状态：{states.get(status, '未知')}（{status}）。",
             "本操作未验收或修改学习进度。"]
    reviews = record.get("reviews", [])
    if reviews:
        latest = reviews[-1]
        review = latest["result"]
        lines.append(f"最近审核：{reviews_text.get(review['status'], '未取得有效审核结果')}" + ("（针对旧草稿）" if latest.get("stale") else ""))
        for issue in review.get("issues", []):
            # Free model rationale is internal. Even stored issue messages are not echoed.
            code = str(issue.get("type", issue.get("code", "reviewRequired")))
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", code):
                code = "reviewRequired"
            message = ISSUE_MESSAGES.get(code, "本次审核未完成，请保留草稿并检查审核记录。")
            lines.append(f"待核对：{message}（{code}）")
    if show_goals:
        lines.append("此版本的用户目标：")
        lines.extend(item["text"] for item in record["compilation"]["publicGoals"])
    if show_internal:
        lines.extend(["内部开发记录：", json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)])
    return "\n".join(lines)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite value")


def _load(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("input too large")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    return value


def main(argv: list[str] | None = None, *, provider=None, repository=None) -> int:
    parser = argparse.ArgumentParser(description="审核、保存与读取 Goal 合同，不修改学习进度。")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--show-goals", action="store_true")
    parser.add_argument("--show-internal", action="store_true")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("import").add_argument("--input", type=Path, required=True)
    edit = actions.add_parser("edit")
    edit.add_argument("--version", type=int, required=True)
    edit.add_argument("--expected-revision", type=int, required=True)
    edit.add_argument("--input", type=Path, required=True)
    revise = actions.add_parser("revise")
    revise.add_argument("--base-version", type=int, required=True)
    revise.add_argument("--reason", required=True)
    review = actions.add_parser("review")
    review.add_argument("--version", type=int, required=True)
    review.add_argument("--expected-revision", type=int, required=True)
    actions.add_parser("show").add_argument("--version", type=int)
    actions.add_parser("history")
    export = actions.add_parser("export")
    export.add_argument("--version", type=int, required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if repository is None:
            if not args.db.is_file():
                print("操作未完成：GOAL_INPUT_UNAVAILABLE。请提供已有业务数据库。")
                return 1
            from apps.api.goal_contract_storage import GoalContractRepository
            repository = GoalContractRepository(args.db)
        if args.action == "import":
            record = repository.import_draft(args.goal_id, _load(args.input))
        elif args.action == "edit":
            record = repository.edit_draft(args.goal_id, args.version, _load(args.input),
                                           expected_draft_revision=args.expected_revision)
        elif args.action == "revise":
            record = repository.create_revision(args.goal_id, args.base_version, args.reason)
        elif args.action == "review":
            if provider is None:
                if args.env_file:
                    from apps.api.deepseek_provider import load_local_env
                    load_local_env(args.env_file)
                from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
                provider = DeepSeekGoalContractReviewProvider.from_environment()
            record = repository.review_and_freeze(args.goal_id, args.version, provider,
                                                  expected_draft_revision=args.expected_revision)
        elif args.action == "show":
            record = (repository.get_current(args.goal_id) if args.version is None
                      else repository.get_version(args.goal_id, args.version))
            if record is None:
                print("尚无当前正式合同；可通过 history 查看草稿。")
                return 2
        elif args.action == "history":
            for record in repository.list_versions(args.goal_id):
                print(render_record(record, show_goals=args.show_goals, show_internal=args.show_internal))
            return 0
        else:
            editable = repository.export_editable(args.goal_id, args.version)
            with args.output.open("x", encoding="utf-8") as target:
                json.dump(editable, target, ensure_ascii=False, indent=2, allow_nan=False)
                target.write("\n")
            print("已导出可编辑草稿；修改后提交仍需重新审核。")
            return 0
        print(render_record(record, show_goals=args.show_goals, show_internal=args.show_internal))
        return 2 if args.action == "review" and record["status"] != "frozen" else 0
    except FileExistsError:
        print("操作未完成：OUTPUT_EXISTS。已有导出文件保持不变。")
    except Exception as error:
        code = getattr(error, "code", "GOAL_CONTRACT_OPERATION_FAILED")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", code):
            code = "GOAL_CONTRACT_OPERATION_FAILED"
        print(f"操作未完成：{code}。未因此修改学习进度；可查看合同历史。")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

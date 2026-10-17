"""Run a human-readable, paid DeepSeek feature extraction demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.api.deepseek_provider import DeepSeekProvider  # noqa: E402
from apps.api.problem_features import (  # noqa: E402
    FeatureExtractionFailure,
    extract_problem_features,
)
from apps.api.problem_intake import (  # noqa: E402
    IntakeFailure,
    create_problem_package,
)


CATEGORY_NAMES = {
    "dataScale": "数据规模",
    "inputStructure": "输入结构",
    "outputStructure": "输出结构",
    "resourceConstraint": "资源限制",
    "domainTag": "题目表面对象",
}
STATUS_NAMES = {
    "confirmed": "模型提取为确定项",
    "hypothesis": "模型推测",
    "unknown": "材料不足",
    "conflict": "原文冲突",
}


def _load_input(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(f"找不到题目文件：{path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"题目文件不是合法 JSON：第 {error.lineno} 行") from error
    if not isinstance(value, dict):
        raise SystemExit("题目文件最外层必须是一个 JSON 对象。")
    return value


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def _print_result(result: dict[str, Any], elapsed: float) -> None:
    extractor = result["extractor"]
    print("\n已生成可核对的解析报告")
    print(f"模型：{extractor['model']}")
    print(f"耗时：{elapsed:.2f} 秒")
    print(f"整体状态：{result['status']}（reviewRequired 表示有待核对项）")
    review = result.get('semanticReview', {})
    if review.get('status') == 'completed':
        print(f"语义审核已完成，审核模型：{review['model']}；模型意见仍需用户确认。")
    else:
        print("语义审核未完成；以下解析不能视为已通过审核。")
    reviews = {(item['category'], item['featureKey']): item for item in review.get('items', [])}
    verdict_names = {'supported': '模型认为原文支持', 'contradicted': '模型认为与原文矛盾',
                     'insufficient': '模型认为依据不足'}

    for category, title in CATEGORY_NAMES.items():
        print(f"\n【{title}】")
        items = [item for item in result["features"] if item["category"] == category]
        for item in items:
            status = STATUS_NAMES[item["status"]]
            if item["status"] == "conflict":
                values = [candidate["value"] for candidate in item["candidates"]]
                detail = _compact(values)
            elif item["status"] == "unknown":
                detail = "没有可靠答案"
            else:
                detail = _compact(item["value"])
            print(f"- {item['featureKey']}：{detail}  [{status}]")
            assessment = reviews.get((item['category'], item['featureKey']))
            if assessment:
                print(f"  语义审核：{verdict_names[assessment['verdict']]}")
                for source in assessment['evidence']:
                    print(f"  审核依据：{source['sourcePath']} → {source['sourceText']}")
            if item.get('review'):
                print(f"  待核对：{item['review']['message']}")
            if item.get("confidence") is not None:
                print(f"  置信度：{item['confidence']}")
            if item.get("reason"):
                print(f"  理由：{item['reason']}")
            evidence = item.get("evidence", [])
            if evidence:
                quoted = "；".join(f"{e['sourcePath']} → {e['sourceText']}" for e in evidence)
                print(f"  原文依据：{quoted}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用真实 DeepSeek 直观查看 FocusMate 对陌生题的特征提取结果。",
    )
    parser.add_argument("problem_file", type=Path, help="UTF-8 JSON 格式的分栏题目文件")
    args = parser.parse_args()

    print("读取题目并创建原始 ProblemPackage……")
    try:
        problem = create_problem_package(_load_input(args.problem_file))
        provider = DeepSeekProvider.from_environment()
        print("调用 DeepSeek 生成解析，再发起独立语义审核（正常流程两次请求，重试可能增加调用）……")
        started = time.perf_counter()
        result = extract_problem_features({"problemPackage": problem}, provider)
        _print_result(result, time.perf_counter() - started)
    except IntakeFailure as error:
        print("\n题目录入失败：")
        for item in error.field_errors:
            print(f"- {item['field']}：{item['message']}")
        return 1
    except FeatureExtractionFailure as error:
        print(f"\n解析失败：{error.message}")
        print(f"错误码：{error.code}；可重试：{'是' if error.retryable else '否'}")
        for item in error.field_errors:
            print(f"- {item['field']}：{item['message']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

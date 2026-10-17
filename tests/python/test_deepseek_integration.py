from __future__ import annotations

import os

import pytest

from apps.api.deepseek_provider import DeepSeekProvider
from apps.api.problem_features import extract_problem_features
from apps.api.problem_intake import create_problem_package
from apps.api.schema_validation import validate


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_INTEGRATION") != "1",
    reason="set RUN_DEEPSEEK_INTEGRATION=1 for the explicit paid integration test",
)


def test_real_deepseek_extracts_traceable_problem_features() -> None:
    problem = create_problem_package({
        "statement": "给定一个长度为 n 的整数数组，找到满足条件的左右端点。",
        "inputSpec": "第一行输入整数 n，第二行输入 n 个整数。",
        "outputSpec": "输出两个整数，分别表示左端点和右端点。",
        "constraints": "1 <= n <= 100000。时间限制 1 秒，内存限制 256 MB。",
        "examples": [{"input": "3\n1 2 3\n", "output": "1 2\n"}],
        "language": "python3",
        "ioMode": "standardInputOutput",
    })
    result = extract_problem_features(
        {"problemPackage": problem},
        DeepSeekProvider.from_environment(),
    )
    validate("ProblemFeatures", result)
    assert result["extractor"]["provider"] == "deepseek"
    assert any(item["category"] == "outputStructure" for item in result["features"])

    assert result["semanticReview"]["status"] == "completed"
    assert len(result["semanticReview"]["items"]) == len(result["features"])

"""Author-reviewed development fixtures; only seed explicitly supplied test databases."""

from __future__ import annotations

import json
from pathlib import Path

from apps.api.problem_features import build_human_feature_set
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository
from apps.api.submission_storage import SubmissionRepository

EXAMPLES = ("two-sum", "graph-connectivity", "merge-intervals")
ROOT = Path(__file__).resolve().parents[1]


def seed_example(db_path: Path, name: str) -> tuple[dict, dict, dict]:
    if name not in EXAMPLES:
        raise ValueError("Unknown development example")
    intake = json.loads((ROOT / "examples/goal-compilation" / f"{name}.json").read_text(encoding="utf-8"))
    package = create_problem_package(intake, id_factory=lambda: f"problem-t041-{name}")
    repo = ProblemRepository(db_path)
    record = repo.create(package)
    features = []
    for key, category, field in (
        ("scale.primary", "dataScale", "constraints"),
        ("input.primary", "inputStructure", "inputSpec"),
        ("output.primary", "outputStructure", "outputSpec"),
    ):
        features.append({"featureKey": key, "category": category, "status": "confirmed",
                         "value": intake[field].splitlines()[0], "confidence": 1,
                         "reason": "开发验收题面已人工核对，此项直接引用题目规定。", "evidenceRefs": [f"{field}:0"]})
    features.append({"featureKey": "resource.primary", "category": "resourceConstraint", "status": "unknown",
                     "reason": "题面未给出单独的执行时间或内存限制。", "evidenceRefs": []})
    features.append({"featureKey": "domain.primary", "category": "domainTag", "status": "confirmed",
                     "value": {"two-sum": "array", "graph-connectivity": "graph", "merge-intervals": "interval"}[name],
                     "confidence": 1, "reason": "题面明确给出处理对象。", "evidenceRefs": ["statement:0"]})
    report = build_human_feature_set(package, {"expectedDraftRevision": 1, "features": features})
    frozen = repo.confirm_and_freeze(package["problemId"], 1, report, record["lifecycle"]["draftRevision"], None)
    goal = SubmissionRepository(db_path, goal_id_factory=lambda: f"goal-t041-{name}").create_goal(report["problemRef"])
    return goal, frozen, report

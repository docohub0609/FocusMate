"""Acceptance tests for the nine T051 static Skill packages."""
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

SKILLS = (
    "problem-understanding",
    "constraint-and-complexity",
    "array-and-hash",
    "two-pointers-and-window",
    "binary-search",
    "stack-and-queue",
    "tree-and-graph",
    "dynamic-programming",
    "testing-and-edge-cases",
)


def load_schema(skill_id, name):
    return json.loads((ROOT / "skills" / skill_id / "schemas" / name).read_text(encoding="utf-8"))


def valid_input():
    return {
        "userRequest": {"ref": "request:1", "text": "请检查我当前的思路。"},
        "goalRequirement": {"ref": "goal:1", "text": "解释当前实现的边界。"},
        "problemFacts": [{"ref": "fact:1", "text": "输入规模最多为 100。"}],
        "code": {"snapshotRef": "code:current", "text": ""},
        "runEvidence": [],
    }


def assert_unique_input_refs(instance):
    refs = [instance["userRequest"]["ref"], instance["goalRequirement"]["ref"]]
    refs += [item["ref"] for item in instance["problemFacts"]]
    refs += [item["ref"] for item in instance["runEvidence"]]
    assert len(refs) == len(set(refs)), "input evidence refs must be unique"


@pytest.mark.parametrize("skill_id", SKILLS)
def test_formal_package_is_valid_and_has_no_tools(skill_id):
    from focusmate.core.skill_package import validate_skill_package

    package = ROOT / "skills" / skill_id
    report = validate_skill_package(package)
    assert report["packageValid"], report
    assert report["declaredToolRequirements"] == []
    assert not (package / "scripts").exists()
    expected_version = {
        "tree-and-graph": 7, "binary-search": 6, "problem-understanding": 5,
        "array-and-hash": 5, "constraint-and-complexity": 5,
        "two-pointers-and-window": 5, "dynamic-programming": 4,
        "stack-and-queue": 4, "testing-and-edge-cases": 4,
    }[skill_id]
    assert report["descriptor"]["version"] == expected_version


@pytest.mark.parametrize("skill_id", SKILLS)
def test_shared_input_contract_accepts_empty_code_and_stale_evidence(skill_id):
    instance = valid_input()
    instance["runEvidence"] = [{
        "ref": "run:old", "text": "旧快照在样例上输出 3。", "codeSnapshotRef": "code:old"
    }]
    assert_unique_input_refs(instance)
    Draft202012Validator(load_schema(skill_id, "input.schema.json")).validate(instance)


@pytest.mark.parametrize("skill_id", SKILLS)
def test_shared_input_contract_rejects_unknown_fields_and_duplicate_refs(skill_id):
    instance = valid_input()
    instance["extra"] = True
    assert not Draft202012Validator(load_schema(skill_id, "input.schema.json")).is_valid(instance)

    duplicate = valid_input()
    duplicate["problemFacts"][0]["ref"] = duplicate["userRequest"]["ref"]
    with pytest.raises(AssertionError, match="unique"):
        assert_unique_input_refs(duplicate)


@pytest.mark.parametrize("skill_id", SKILLS)
@pytest.mark.parametrize("status", ["applicable", "notApplicable"])
def test_shared_output_contract_accepts_evidenced_applicability(skill_id, status):
    result = {
        "applicability": {"status": status, "reason": "由当前请求和题目事实判断。", "evidenceRefs": ["request:1"]},
        "findings": [{"summary": "当前材料只支持这一条局部观察。", "evidenceRefs": ["fact:1"]}],
        "unknowns": [],
    }
    Draft202012Validator(load_schema(skill_id, "result.schema.json")).validate(result)


@pytest.mark.parametrize("skill_id", SKILLS)
def test_shared_output_contract_requires_evidence_and_rejects_state_write(skill_id):
    schema = load_schema(skill_id, "result.schema.json")
    missing_evidence = {
        "applicability": {"status": "uncertain", "reason": "材料不足。", "evidenceRefs": []},
        "findings": [{"summary": "无依据发现。", "evidenceRefs": []}],
        "unknowns": ["缺少题目条件。"],
    }
    assert not Draft202012Validator(schema).is_valid(missing_evidence)
    valid = {
        "applicability": {"status": "uncertain", "reason": "材料不足。", "evidenceRefs": ["request:1"]},
        "findings": [], "unknowns": ["缺少题目条件。"], "goalStateUpdate": {},
    }
    assert not Draft202012Validator(schema).is_valid(valid)


def test_example_tree_and_graph_was_not_promoted_or_modified():
    from focusmate.core.skill_package import validate_skill_package

    example = ROOT / "examples" / "skill-package" / "tree-and-graph"
    report = validate_skill_package(example)
    assert report["packageValid"], report
    assert report["computedHash"] == "sha256:247bb99cd894570390f66a3b0aca409647a1cbc086c87a211a00d85d35bd547b"

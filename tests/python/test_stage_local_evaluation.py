import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from tools.stage_local_evaluation import freeze_experiment, load_cases as _load_cases, run_evaluation

def load_cases():
    return _load_cases(ROOT / "tests/fixtures/t062/stage-local-cases.json")


def test_fixtures_are_distinct_balanced_and_blind():
    cases = load_cases()
    assert len(cases) == 24
    assert len({case["id"] for case in cases}) == 24
    assert Counter(case["group"] for case in cases) == {"development": 12, "holdout": 12}
    for split in ("development", "holdout"):
        split_cases = [case for case in cases if case["group"] == split]
        assert set(Counter(case["expected"]["stage"] for case in split_cases)) == {
            "draft", "strategy_hypothesis", "implementation_in_progress", "test_ready", "unknown"
        }
        assert any(case["expected"]["shouldRunLocal"] for case in split_cases)
        assert any(not case["expected"]["shouldRunLocal"] for case in split_cases)
    serialized_inputs = [json.dumps(case["input"], ensure_ascii=False, sort_keys=True) for case in cases]
    assert len(set(serialized_inputs)) == 24
    forbidden = {"expected", "shouldRunLocal", "mustExplain", "forbiddenConclusions", "stageLabel"}
    assert all(not any(token in text for token in forbidden) for text in serialized_inputs)


def test_fixture_references_are_neutral_and_expectations_are_grounded():
    for case in load_cases():
        records = case["input"]["records"]
        refs = [record["ref"] for record in records]
        assert len(refs) == len(set(refs))
        assert all(ref[0] in "gpcour" and ref[1:].isdigit() for ref in refs)
        assert set(case["expected"]["factRefs"]) <= set(refs)
        assert case["expected"]["reasoning"].strip()
        assert case["expected"]["mustExplain"]
        assert case["expected"]["forbiddenConclusions"]
        code_records = [record for record in records if record["kind"] == "code"]
        assert len(code_records) >= 1
        if case["expected"].get("syntaxValid", True):
            compile(code_records[0]["content"]["text"], f"<{case['id']}>", "exec")
    assert sum(not case["expected"].get("syntaxValid", True) for case in load_cases()) == 1


def test_freeze_has_one_dev_and_two_holdout_runs_and_hashes_sources():
    manifest = freeze_experiment(load_cases(), {"provider": "fake", "model": "m", "apiKey": "SECRET"})
    assert len(manifest["schedule"]) == 36
    assert Counter(row["repeat"] for row in manifest["schedule"] if row["caseId"].startswith("dev-")) == {1: 12}
    assert Counter(row["repeat"] for row in manifest["schedule"] if row["caseId"].startswith("hold-")) == {1: 12, 2: 12}
    assert "SECRET" not in json.dumps(manifest, ensure_ascii=False)
    assert manifest["stagePrompt"]
    assert manifest["toolPrompt"]
    assert manifest["toolDefinitions"]
    assert all(len(value) == 64 for value in manifest["implementationHashes"].values())


def test_bad_fixture_blocks_calls_before_output_directory_is_created(tmp_path):
    cases = load_cases()
    cases[0]["input"]["records"][0]["ref"] = "expected-test-ready"
    calls = []
    with pytest.raises(ValueError):
        run_evaluation(cases, lambda *_: calls.append(1), tmp_path / "run", {}, "all")
    assert calls == []
    assert not (tmp_path / "run").exists()


def test_runner_receives_no_expectations_and_each_record_is_exclusive(tmp_path):
    seen = []

    def runner(case_input, directory):
        seen.append(deepcopy(case_input))
        text = json.dumps(case_input, ensure_ascii=False)
        assert "expected" not in text and "shouldRunLocal" not in text
        Path(directory, "provider-response.json").write_text("{}", encoding="utf-8")
        return {"status": "recognized", "stageReport": {"status": "recognized"}}

    summary = run_evaluation(load_cases(), runner, tmp_path / "run", {"model": "fake"}, "holdout")
    assert len(seen) == 24
    assert summary["counts"] == {"recognized": 24}
    records = list((tmp_path / "run" / "records").glob("*.json"))
    response_files = list((tmp_path / "run" / "records").glob("*/provider-response.json"))
    assert len(records) == 24 and len(response_files) == 24
    assert len({path.parent.name for path in response_files}) == 24
    assert all((path.parent.parent / f"{path.parent.name}.json").exists() for path in response_files)


def test_history_is_not_overwritten_and_failures_are_retained(tmp_path):
    output = tmp_path / "run"

    def fail(*_):
        raise RuntimeError("secret endpoint detail")

    summary = run_evaluation(load_cases(), fail, output, {}, "development")
    assert summary["counts"] == {"runner_failed": 12}
    saved = (output / "records" / "dev-01-1.json").read_text(encoding="utf-8")
    assert "RuntimeError" in saved and "secret endpoint detail" not in saved
    with pytest.raises(FileExistsError):
        run_evaluation(load_cases(), fail, output, {}, "development")


@pytest.mark.parametrize("group,expected", [("development", 12), ("holdout", 24), ("all", 36)])
def test_group_schedule_counts(group, expected):
    manifest = freeze_experiment(load_cases(), {}, group)
    assert len(manifest["schedule"]) == expected

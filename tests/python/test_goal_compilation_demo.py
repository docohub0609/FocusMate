from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def demo_module():
    path = ROOT / "tools/demo-goal-compilation.py"
    assert path.is_file(), "T041 must provide the read-only demo entry"
    spec = importlib.util.spec_from_file_location("goal_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_database_is_not_created(tmp_path):
    demo = demo_module()
    missing = tmp_path / "not-created.db"
    with pytest.raises(Exception) as caught:
        demo.load_inputs(missing, "goal-missing")
    assert caught.value.code == "GOAL_INPUT_UNAVAILABLE"
    assert not missing.exists()


def test_old_database_missing_columns_returns_safe_input_error(tmp_path):
    demo = demo_module()
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE goal_records (goal_id TEXT)")
        db.execute("INSERT INTO goal_records VALUES ('goal-old')")
    before = path.read_bytes()
    with pytest.raises(Exception) as caught:
        demo.load_inputs(path, "goal-old")
    assert caught.value.code == "GOAL_INPUT_UNAVAILABLE"
    assert path.read_bytes() == before


def test_default_render_never_contains_internal_or_provider_text():
    demo = demo_module()
    result = {
        "publicGoals": [{"subgoalId": "s1", "stage": "problemUnderstanding", "text": "明确需要返回两个不同位置。"}],
        "strategyCandidates": [{"label": "INTERNAL STRATEGY SENTINEL"}],
        "rubrics": [{"checks": ["INTERNAL RUBRIC SENTINEL"]}],
        "generation": {"semanticStatus": "unverified"},
    }
    rendered = demo.render_result(result)
    assert "明确需要返回两个不同位置" in rendered
    assert "SENTINEL" not in rendered
    assert "未验证" in rendered
    internal = demo.render_result(result, show_internal=True)
    assert "INTERNAL STRATEGY SENTINEL" in internal


def test_loader_reads_existing_goal_in_snapshot_without_writes(tmp_path):
    from tests.python.goal_compilation_fixtures import stored_inputs

    demo = demo_module()
    db_path = tmp_path / "real.db"
    goal, problem, features = stored_inputs(db_path, "two-sum")
    before = db_path.read_bytes()
    loaded = demo.load_inputs(db_path, goal["goalId"])
    assert loaded == (goal, problem, features)
    assert db_path.read_bytes() == before
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "goal_contracts" not in tables


def test_cli_provider_failure_does_not_print_raw_response(tmp_path, capsys):
    from tests.python.goal_compilation_fixtures import stored_inputs
    from focusmate.contracts.goal_compilation import GoalCompilationError

    demo = demo_module()
    db_path = tmp_path / "real.db"
    goal, _, _ = stored_inputs(db_path, "two-sum")

    class BrokenProvider:
        def complete(self, **kwargs):
            raise GoalCompilationError("LLM_UNAVAILABLE", "RAW SECRET BODY", True)

    code = demo.main(["--db", str(db_path), "--goal-id", goal["goalId"]], provider=BrokenProvider())
    output = capsys.readouterr().out
    assert code == 1
    assert "LLM_UNAVAILABLE" in output
    assert "RAW SECRET BODY" not in output


def test_deepseek_adapter_uses_existing_transport_and_safe_failure():
    from apps.api.deepseek_provider import ProviderFailure, ProviderResponse

    path = ROOT / "src/focusmate/adapters/goal_compilation.py"
    assert path.is_file(), "T041 must supply a goal compilation adapter"
    from focusmate.adapters.goal_compilation import DeepSeekGoalCompilationProvider
    from focusmate.contracts.goal_compilation import GoalCompilationError

    class Transport:
        def complete(self, *, system_prompt, user_prompt, deadline):
            assert system_prompt == "policy" and user_prompt == "source"
            assert deadline > 0
            return ProviderResponse("response-1", "configured-model", '{"subgoals": []}')

    response = DeepSeekGoalCompilationProvider(Transport()).complete(system_prompt="policy", user_prompt="source")
    assert (response.model, response.response_id, json.loads(response.content)) == (
        "configured-model", "response-1", {"subgoals": []},
    )

    class FailedTransport:
        def complete(self, **kwargs):
            raise ProviderFailure("LLM_UNAVAILABLE", "RAW SECRET BODY", True)

    with pytest.raises(GoalCompilationError) as caught:
        DeepSeekGoalCompilationProvider(FailedTransport()).complete(system_prompt="policy", user_prompt="source")
    assert caught.value.code == "LLM_UNAVAILABLE"
    assert "RAW SECRET BODY" not in str(caught.value)


@pytest.mark.parametrize("name", ["two-sum", "graph-connectivity", "merge-intervals"])
def test_development_examples_create_confirmed_inputs_without_ready_assets(tmp_path, name):
    from tests.python.goal_compilation_fixtures import stored_inputs
    goal, problem, report = stored_inputs(tmp_path / "example.db", name)
    assert goal["problemRef"] == report["problemRef"]
    assert problem["lifecycle"]["status"] == "frozen"
    assert report["provenance"]["type"] == "humanCreated"
    assert problem["problemPackage"]["verifierAssetStatus"] == "missing"


def test_integration_runner_retains_each_result_and_does_not_overwrite(tmp_path):
    path = ROOT / "tools/run-goal-compilation-integration.py"
    assert path.is_file(), "T041 must provide reproducible three-problem integration"
    spec = importlib.util.spec_from_file_location("goal_integration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from tests.python.test_goal_compilation import wire
    from focusmate.contracts.goal_compilation import GoalCompilationResponse

    class Provider:
        def complete(self, **kwargs):
            return GoalCompilationResponse(json.dumps(wire(), ensure_ascii=False), "fake-model", "fake-id")

    assert module.run_cases(tmp_path / "results", Provider()) == 0
    summary = json.loads((tmp_path / "results/summary.json").read_text(encoding="utf-8"))
    assert len(summary) == 3
    assert all(item["status"] == "generated" and item["databaseUnchanged"] for item in summary)
    with pytest.raises(FileExistsError):
        module.run_cases(tmp_path / "results", Provider())

@pytest.mark.parametrize('filename', ['demo-goal-compilation.py', 'run-goal-compilation-integration.py'])
def test_env_read_failure_is_safe(filename, tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('env_demo', ROOT / 'tools' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def fail(path):
        raise PermissionError('PRIVATE CONFIG PATH')
    monkeypatch.setattr(module, 'load_local_env', fail)
    args = ['--env-file', str(tmp_path / 'config')]
    if filename.startswith('demo'):
        monkeypatch.setattr(module, 'load_inputs', lambda *args: ({}, {}, {}))
        args += ['--db', str(tmp_path / 'db'), '--goal-id', 'goal']
    else:
        args += ['--output-dir', str(tmp_path / 'out')]
    assert module.main(args) == 1
    output = capsys.readouterr().out
    assert 'GOAL_CONFIG_UNAVAILABLE' in output
    assert 'PRIVATE' not in output

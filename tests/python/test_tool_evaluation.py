import json
from collections import Counter
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))

import pytest

from tools.tool_evaluation import load_cases, freeze_experiment, run_evaluation


def test_balanced_materials_and_no_expected_in_callback(tmp_path):
    cases = load_cases()
    assert len(cases) == 24
    for case in cases:
        for code in case['input']['fixtures']['codes']:
            compile(code['source'], '<fixture>', 'exec')
    assert Counter(c['group'] for c in cases) == {'development': 12, 'holdout': 12}
    for group in ('development', 'holdout'):
        assert set(Counter(c['category'] for c in cases if c['group'] == group).values()) == {2}
    seen = []
    def run(material, output):
        seen.append(material)
        assert 'expected' not in material and 'category' not in material
        assert 'fixtures' not in material['materials']
        return {'status': 'completed', 'answer': 'local answer'}
    result = run_evaluation(cases, run, tmp_path/'run', {'model': 'fake', 'apiKey': 'SECRET'}, 'holdout')
    assert len(seen) == 24
    assert result['contentAccepted'] is None
    assert 'SECRET' not in (tmp_path/'run'/'manifest.json').read_text(encoding='utf-8')


def test_no_overwrite_and_failures_are_not_pass(tmp_path):
    cases = load_cases()
    def fail(material, output):
        raise RuntimeError('SECRET endpoint')
    result = run_evaluation(cases, fail, tmp_path/'run', {}, 'development')
    assert result['counts'] == {'runner_failed': 12}
    assert 'SECRET' not in (tmp_path/'run'/'records'/'01-1.json').read_text(encoding='utf-8')
    with pytest.raises(FileExistsError):
        run_evaluation(cases, fail, tmp_path/'run', {}, 'development')


def test_freeze_rejects_incomplete_or_renamed_cases():
    cases = load_cases()
    with pytest.raises(ValueError):
        freeze_experiment(cases[:-1], {})
    cases[0]['input']['materials']['userRequest']['ref'] = 'correct-answer'
    with pytest.raises(ValueError):
        freeze_experiment(cases, {})


def test_manifest_freezes_tools_prompt_and_actual_sources():
    manifest = freeze_experiment(load_cases(), {'model': 'fake'})
    assert len(manifest['tools']) == 5
    assert 'systemPrompt' in manifest
    assert 'src/focusmate/core/tool_dialogue.py' in manifest['implementationHashes']
    assert all(len(value) == 64 for value in manifest['implementationHashes'].values())

"""Exercise the developer entry with Fake transport; no semantic quality claim."""
import importlib.util
import json
from tests.python.publication_fixtures import audit_args
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def load_cli():
    spec = importlib.util.spec_from_file_location('eval_skills_cli_test', ROOT / 'tools/eval-skills.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Provider:
    public_config = {'model': 'Fake', 'thinkingEnabled': False}

    def __init__(self): self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs['user_prompt'])
        if 'expected' in payload:
            keys = [item['id'] for item in payload['expected']['requirements']] + payload['requiredChecks']
            value = {'checks': {key: {'status': 'pass', 'reason': 'Fake routing result',
                                 'evidenceRefs': ['request']} for key in keys}}
        else:
            value = {'applicability': {'status': 'applicable', 'reason': 'Fake routing result',
                                      'evidenceRefs': ['request']}, 'findings': [], 'unknowns': []}
        return SimpleNamespace(content=json.dumps(value), model='Fake', response_id='fake')


def test_cli_saves_each_case_and_refuses_existing_directory(tmp_path, monkeypatch):
    module = load_cli()
    provider = Provider()
    monkeypatch.setattr(module.DeepSeekSkillEvaluationProvider, 'from_environment', lambda: provider)
    output = tmp_path / 'new-run'
    args = ['--skill', 'tree-and-graph', '--split', 'development', '--output', str(output), '--regression']
    assert module.main(audit_args(tmp_path, approval=True) + args) == 0
    assert len(provider.calls) == 6
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    assert summary['caseCount'] == 3
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert len(manifest['caseIds']) == 3
    assert manifest['evaluationUse'] == 'regression'
    for identity in manifest['caseIds']:
        row = json.loads((output / f'{identity}.json').read_text(encoding='utf-8'))
        assert row['goalContext']['subgoalId']
        assert row['modelConfig']['model'] == 'Fake'
    assert module.main(audit_args(tmp_path, approval=True) + args) == 1
    assert len(provider.calls) == 6


def test_cli_detects_changed_package_before_model(tmp_path, monkeypatch):
    module = load_cli()
    provider = Provider()
    monkeypatch.setattr(module.DeepSeekSkillEvaluationProvider, 'from_environment', lambda: provider)
    original = module.validate_skill_package
    calls = []

    def changing_package(path):
        report = original(path)
        calls.append(path)
        if len(calls) > 1: report['computedHash'] = 'changed-after-manifest'
        return report

    monkeypatch.setattr(module, 'validate_skill_package', changing_package)
    output = tmp_path / 'changed'
    assert module.main(audit_args(tmp_path, approval=True) + ['--skill', 'tree-and-graph', '--split', 'development', '--output', str(output)]) == 1
    assert not provider.calls
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    assert summary['counts'] == {'inputError': 3}

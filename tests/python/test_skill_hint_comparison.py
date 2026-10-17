"""Offline comparison regressions: transport fakes cannot establish hint quality."""
from copy import deepcopy
import importlib.util
import json
from tests.python.publication_fixtures import audit_args
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.deepseek_provider import ProviderFailure
from tools import skill_evaluation
from tools.skill_evaluation_cases import build_cases

ROOT = Path(__file__).resolve().parents[2]


def comparison():
    # A missing implementation fails this assertion before attempting imports.
    assert (ROOT / 'tools/skill_hint_comparison.py').is_file(), 'comparison implementation missing'
    from tools import skill_hint_comparison
    return skill_hint_comparison


def reviewed_cases():
    cases = build_cases()
    for case in cases:
        case['provenance']['independentlyReviewed'] = True
    return cases


class Provider:
    public_config = {'provider': 'Fake', 'model': 'Fake', 'thinkingEnabled': True,
                     'networkAttempts': 2, 'reasoningEffort': 'high'}

    def __init__(self, contents=None, on_call=None):
        self.calls = []
        self.contents = contents or []
        self.on_call = on_call

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call(len(self.calls))
        value = self.contents[len(self.calls) - 1] if len(self.calls) <= len(self.contents) else (
            '{"hint":"核对当前操作的局部条件。","evidenceRefs":["request"]}')
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=value, model='Fake', response_id=f'fake-{len(self.calls)}',
                               reasoning_content='NEVER_SAVE_REASONING')


def records(output):
    return [json.loads(path.read_text(encoding='utf-8')) for path in sorted((output / 'records').glob('*.json'))]


def test_fixed_twelve_cases_and_reversed_two_round_schedule():
    module = comparison()
    selected = module.select_cases(reviewed_cases())
    ids = [case['id'] for case in selected]
    assert len(ids) == 12
    assert 'stack-and-queue-holdout-issue' in ids
    assert 'stack-and-queue-development-issue' not in ids
    assert ids[-3:] == ['array-and-hash-development-correct', 'tree-and-graph-development-correct',
                        'binary-search-development-notApplicable']
    manifest = module.freeze_experiment(selected, ROOT / 'skills', Provider.public_config)
    schedule = manifest['schedule']
    assert len(schedule) == 48
    assert [row['arm'] for row in schedule[:4]] == ['baseline', 'skill', 'skill', 'baseline']
    for index in range(12):
        first = schedule[index * 2:index * 2 + 2]
        second = schedule[24 + index * 2:24 + index * 2 + 2]
        assert {row['caseId'] for row in first + second} == {ids[index]}
        assert [row['arm'] for row in first] == list(reversed([row['arm'] for row in second]))
        assert [row['round'] for row in first + second] == [1, 1, 2, 2]


def test_only_skill_text_differs_and_old_labels_never_reach_model(tmp_path):
    module = comparison()
    provider = Provider()
    module.run_comparison(reviewed_cases(), ROOT / 'skills', provider, tmp_path / 'fair')
    assert len(provider.calls) == 48
    for index in range(0, 48, 2):
        left, right = deepcopy(provider.calls[index:index + 2])
        assert left['system_prompt'] == right['system_prompt']
        assert left['system_prompt'].startswith('结合题意和代码，给出最小提示，不给完整代码或完整题解，只回答当前问题。')
        assert '优先于 Skill' in left['system_prompt']
        left, right = json.loads(left['user_prompt']), json.loads(right['user_prompt'])
        assert ('skill' in left) != ('skill' in right)
        sop = left.pop('skill', None) or right.pop('skill')
        assert '## 处理方法' in sop and '## 结果格式' in sop
        assert left == right
        assert set(left) == {'materials', 'outputSchema', 'runEvidenceVersions'}
        assert set(left['outputSchema']['properties']) == {'hint', 'evidenceRefs'}
        assert left['outputSchema']['additionalProperties'] is False
        text = json.dumps(left)
        for forbidden in ('expected', 'provenance', 'development', 'holdout', 'notApplicable', 'skillId'):
            assert forbidden not in text


@pytest.mark.parametrize('mutate', [
    lambda case: case['inputs']['problemFacts'].append({'ref': 'request', 'text': 'duplicate'}),
    lambda case: case['inputs']['runEvidence'].append({'ref': 'run:x', 'text': 'old', 'codeSnapshotRef': 'request'}),
    lambda case: case['provenance'].update(independentlyReviewed=False),
    lambda case: case['inputs']['code'].update(extra='invalid schema'),
])
def test_preflights_every_case_before_first_call(tmp_path, mutate):
    module = comparison()
    cases = module.select_cases(reviewed_cases())
    mutate(cases[-1])
    provider = Provider()
    summary = module.run_comparison(cases, ROOT / 'skills', provider, tmp_path / 'bad-input')
    assert summary['status'] == 'inputError'
    assert not provider.calls
    assert (tmp_path / 'bad-input/input-failure.json').is_file()


def test_old_run_marked_noncurrent_without_inventing_current_evidence():
    module = comparison()
    manifest = module.freeze_experiment(reviewed_cases(), ROOT / 'skills', Provider.public_config)
    case = next(case for case in manifest['cases'] if case['id'] == 'testing-and-edge-cases-development-issue')
    payload = case['prompts']['baseline']
    assert payload['runEvidenceVersions'] == {'run:1': {'codeSnapshotRef': 'code:v1', 'isCurrent': False}}


def test_every_attempt_persists_visible_raw_content_and_safe_errors(tmp_path):
    module = comparison()
    contents = ['not JSON', '{"hint":5,"evidenceRefs":[]}',
                '{"hint":"x","evidenceRefs":["invented"]}',
                '{"hint":"a","hint":"b","evidenceRefs":["request"]}',
                ProviderFailure('SENSITIVE_KEY', 'SECRET_NETWORK_DETAILS', True),
                '{"hint":"middle = (left + right) // 2","evidenceRefs":["code:v2"]}']
    provider = Provider(contents)
    output = tmp_path / 'raw'
    summary = module.run_comparison(reviewed_cases(), ROOT / 'skills', provider, output)
    rows = records(output)
    assert len(rows) == len(provider.calls) == 48
    assert [row['status'] for row in rows[:6]] == ['invalid', 'invalid', 'invalid', 'invalid', 'unavailable', 'generated']
    assert [row['validationFailure']['code'] for row in rows[:4]] == [
        'INVALID_JSON', 'INVALID_OUTPUT_SCHEMA', 'INVALID_EVIDENCE_REFERENCES', 'INVALID_JSON']
    assert rows[0]['validationFailure'] == {'code': 'INVALID_JSON', 'kind': 'JSONDecodeError',
                                           'line': 1, 'column': 1, 'position': 0}
    for index in [0, 1, 2, 3, 5]:
        assert rows[index]['response']['content'] == contents[index]
    for row in rows:
        assert row['goalContext']['goalId'] and row['goalContext']['subgoalId']
        assert row['logicalCallCount'] == 1 and row['durationSeconds'] >= 0
        assert row['tokenUsage'] is None and row['cost'] is None
    saved = ''.join(path.read_text(encoding='utf-8') for path in output.rglob('*.json'))
    for forbidden in ('NEVER_SAVE_REASONING', 'SECRET_NETWORK_DETAILS', 'SENSITIVE_KEY'):
        assert forbidden not in saved
    assert summary['logicalCallCount'] == 48
    assert summary['counts'] == {'invalid': 4, 'unavailable': 1, 'generated': 43}
    assert summary['semanticReviewStatus'] == 'pending'


def test_manifest_exists_before_calls_and_snapshots_survive_source_drift(tmp_path, monkeypatch):
    module = comparison()
    cases = reviewed_cases()
    output = tmp_path / 'frozen'
    manifest_bytes = []

    def after_freeze(count):
        manifest_bytes.append((output / 'manifest.json').read_bytes())
        if count == 1:
            for case in cases:
                case['inputs']['userRequest']['text'] = 'MUTATED_SOURCE'
            monkeypatch.setattr(module, '_validate_case', lambda *args: pytest.fail('reread mutable package'))

    provider = Provider(on_call=after_freeze)
    module.run_comparison(cases, ROOT / 'skills', provider, output)
    assert len(set(manifest_bytes)) == 1
    assert 'MUTATED_SOURCE' not in json.dumps(provider.calls)
    frozen = json.loads(manifest_bytes[0])
    assert frozen['experimentHash'] == skill_evaluation.digest({key: value for key, value in frozen.items() if key != 'experimentHash'})
    assert all(item['skillRef']['hash'].startswith('sha256:') for item in frozen['cases'])
    assert frozen['modelConfig']['model'] == 'Fake'


def test_blind_export_hides_arm_package_labels_and_has_random_unique_ids(tmp_path):
    module = comparison()
    output = tmp_path / 'blind'
    module.run_comparison(reviewed_cases(), ROOT / 'skills', Provider(['bad JSON']), output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    blind = json.loads((output / 'blind-review.json').read_text(encoding='utf-8'))
    assert blind['experimentHash'] == manifest['experimentHash']
    assert len(blind['items']) == len({item['answerId'] for item in blind['items']}) == 48
    assert set(manifest['blindMapping']) == {item['answerId'] for item in blind['items']}
    assert {item['id'] for item in blind['rubric']} == {'accuracy', 'focus', 'helpfulness', 'coachingScope'}
    assert any(item['hint'] is None for item in blind['items'])
    for item in blind['items']:
        assert item['answerHash'] == skill_evaluation.digest({'hint': item['hint'], 'evidenceRefs': item['evidenceRefs']})
    assert manifest['comparisonPolicy']['mixedDirections'] == 'insufficientEvidence'
    assert manifest['comparisonPolicy']['missingOrUncertain'] == 'insufficientEvidence'
    text = json.dumps(blind)
    for forbidden in ('baseline', 'skillId', 'caseId', 'development', 'holdout', 'notApplicable',
                      'expected', 'provenance', 'SOP', '## 处理方法', 'record-001'):
        assert forbidden not in text


def test_existing_directory_is_never_overwritten_or_called(tmp_path):
    module = comparison()
    output = tmp_path / 'existing'
    output.mkdir()
    (output / 'manifest.json').write_text('keep me', encoding='utf-8')
    provider = Provider()
    with pytest.raises(FileExistsError):
        module.run_comparison(reviewed_cases(), ROOT / 'skills', provider, output)
    assert not provider.calls
    assert (output / 'manifest.json').read_text(encoding='utf-8') == 'keep me'


def test_cli_uses_reviewed_fixed_cases_env_file_and_shared_provider_config(tmp_path, monkeypatch):
    comparison()
    path = ROOT / 'tools/compare-skill-hints.py'
    assert path.is_file(), 'comparison CLI missing'
    spec = importlib.util.spec_from_file_location('hint_comparison_cli_test', path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    provider = Provider()
    loaded = []
    monkeypatch.setattr(cli, 'load_local_env', loaded.append)
    monkeypatch.setattr(cli.DeepSeekSkillEvaluationProvider, 'from_environment', lambda: provider)
    output = tmp_path / 'cli'
    env = tmp_path / '.env'
    assert cli.main(audit_args(tmp_path) + ['--output', str(output), '--env-file', str(env)]) == 0
    assert loaded == [env]
    assert len(provider.calls) == 48
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert all(case['sourceCase']['provenance']['independentlyReviewed'] for case in manifest['cases'])
    assert cli.main(audit_args(tmp_path) + ['--output', str(output)]) == 1
    assert len(provider.calls) == 48


def test_configuration_unavailable_keeps_all_scheduled_records_without_calls(tmp_path):
    module = comparison()
    output = tmp_path / 'missing-config'
    summary = module.run_comparison(reviewed_cases(), ROOT / 'skills', None, output)
    assert summary['counts'] == {'unavailable': 48}
    assert summary['logicalCallCount'] == 0
    assert len(records(output)) == 48
    assert all(row['response'] is None for row in records(output))


def test_package_drift_during_preflight_never_enters_either_arm(tmp_path, monkeypatch):
    module = comparison()
    original = module.read_files

    def changed_snapshot(path):
        files = original(path)
        files['SKILL.md'] += '\nMUTATED_PACKAGE'
        return files

    monkeypatch.setattr(module, 'read_files', changed_snapshot)
    provider = Provider()
    summary = module.run_comparison(reviewed_cases(), ROOT / 'skills', provider, tmp_path / 'drift')
    assert summary['status'] == 'inputError'
    assert not provider.calls


@pytest.mark.parametrize('alter', [
    lambda cases: cases.pop(),
    lambda cases: cases.append(deepcopy(cases[0])),
    lambda cases: cases[-1].update(materialVersion=0),
])
def test_missing_duplicate_or_invalid_version_prevents_any_call(tmp_path, alter):
    module = comparison()
    cases = module.select_cases(reviewed_cases())
    alter(cases)
    provider = Provider()
    summary = module.run_comparison(cases, ROOT / 'skills', provider, tmp_path / 'invalid-set')
    assert summary['status'] == 'inputError'
    assert not provider.calls

import importlib.util
import json
from tests.python.publication_fixtures import audit_args
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def fixture_case():
    from test_skill_evaluation import fixture, actor
    return fixture(), {'id': 'reviewer-one', 'baseCaseId': 'graph-development-issue',
                       'analysis': actor(), 'expectedReviewStatus': 'failed',
                       'requiredCheckStatuses': {'evidenceSupport': 'fail'},
                       'basis': 'PRIVATE_CALIBRATION_BASIS', 'provenance': {'draftedBy': 'AI'}}


class Provider:
    public_config = {'model': 'Fake'}

    def __init__(self, verdict='fail'): self.calls, self.verdict = [], verdict

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        data = json.loads(kwargs['user_prompt'])
        checks = {key: {'status': self.verdict if key == 'evidenceSupport' else 'pass',
                        'reason': 'Fake result', 'evidenceRefs': ['request']} for key in data['checkIds']}
        return SimpleNamespace(content=json.dumps({'checks': checks}), model='Fake', response_id='id')


def test_fixed_answer_expected_label_never_enters_reviewer():
    from tools.skill_reviewer_evaluation import evaluate_fixed_answer
    base, fixture = fixture_case()
    provider = Provider()
    row = evaluate_fixed_answer(fixture, base, ROOT / 'skills', provider)
    assert row['calibrationStatus'] == 'passed'  # Correctly rejecting bad analysis is success.
    assert row['status'] == 'failed'
    assert row['goalContext'] == base['goalContext']
    assert len(provider.calls) == 1
    payload = provider.calls[0]['user_prompt']
    for secret in ['expectedReviewStatus', 'requiredCheckStatuses', 'PRIVATE_CALIBRATION_BASIS', 'reviewer-one']:
        assert secret not in payload


def test_reviewer_false_negative_is_not_calibration_pass():
    from tools.skill_reviewer_evaluation import evaluate_fixed_answer
    base, fixture = fixture_case()
    row = evaluate_fixed_answer(fixture, base, ROOT / 'skills', Provider('pass'))
    assert row['calibrationStatus'] == 'failed'


def test_service_failure_not_misattributed_to_content():
    from tools.skill_reviewer_evaluation import evaluate_fixed_answer
    from apps.api.deepseek_provider import ProviderFailure
    class Unavailable:
        def complete(self, **kwargs): raise ProviderFailure('LLM_UNAVAILABLE', 'secret', True)
    base, fixture = fixture_case()
    row = evaluate_fixed_answer(fixture, base, ROOT / 'skills', Unavailable())
    assert row['status'] == 'unavailable'
    assert row['calibrationStatus'] == 'incomplete'
    assert 'secret' not in json.dumps(row)


def test_fixture_audit_must_match_actual_contents():
    from tools.skill_reviewer_evaluation import validate_fixture_audit
    from tools.skill_evaluation import digest
    _, item = fixture_case()
    audit = {'reviewer': 'AI-independent', 'fixturesHash': digest([item]),
             'cases': [{'id': item['id'], 'status': 'accepted', 'reason': 'checked'}]}
    validate_fixture_audit([item], audit)
    item['analysis']['unknowns'].append('changed')
    with pytest.raises(ValueError): validate_fixture_audit([item], audit)


def test_review_only_cli_records_four_calls_and_preserves_history(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('reviewer_cli', ROOT / 'tools/eval-skill-reviewer.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = Provider()
    monkeypatch.setattr(module.DeepSeekSkillEvaluationProvider, 'from_environment', lambda: provider)
    output = tmp_path / 'calibration'
    assert module.main(audit_args(tmp_path, reviewer=True) + ['--output', str(output)]) == 1  # Fake rejects both correct controls too.
    assert len(provider.calls) == 4
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    assert summary['counts'] == {'passed': 2, 'failed': 2}
    original = (output / 'manifest.json').read_bytes()
    assert module.main(audit_args(tmp_path, reviewer=True) + ['--output', str(output)]) == 1
    assert len(provider.calls) == 4
    assert (output / 'manifest.json').read_bytes() == original
    assert all('calibrationExpectation' not in call['user_prompt'] for call in provider.calls)

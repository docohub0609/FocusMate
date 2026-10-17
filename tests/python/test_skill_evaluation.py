from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools.skill_evaluation import evaluate_case, save_record, build_actor_input, apply_independent_review

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    return {
        'id': 'graph-development-issue', 'split': 'development',
        'goalContext': {'goalId': 'eval-graph', 'scope': 'subgoal', 'subgoalId': 'termination'},
        'skillId': 'tree-and-graph', 'materialVersion': 1,
        'inputs': {'userRequest': {'ref': 'request', 'text': '检查为什么不结束'},
                   'goalRequirement': {'ref': 'goal', 'text': '检查遍历终止条件'},
                   'problemFacts': [{'ref': 'fact', 'text': '无向图允许环'}],
                   'code': {'snapshotRef': 'code-v2', 'text': 'def visit(u):\n    for v in adj[u]: visit(v)'},
                   'runEvidence': []},
        'expected': {'category': 'issue', 'requirements': [
            {'id': 'e1', 'text': '指出缺少访问控制可能重复递归', 'evidenceRefs': ['code-v2', 'fact']}]},
        'provenance': {'draftedBy': 'AI', 'independentlyReviewed': True, 'humanReviewed': False},
    }


def actor():
    return {'applicability': {'status': 'applicable', 'reason': '当前请求检查遍历', 'evidenceRefs': ['request']},
            'findings': [{'summary': '没有阻止重复访问', 'evidenceRefs': ['code-v2', 'fact']}], 'unknowns': []}


def review():
    return {'checks': {key: {'status': 'pass', 'reason': '材料支持此判断', 'evidenceRefs': ['code-v2', 'fact']}
                       for key in ['e1', 'evidenceSupport', 'coachingScope', 'applicability', 'unknowns']}}


class Fake:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def complete(self, **kwargs):
        from types import SimpleNamespace
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=json.dumps(value), model='fake', response_id='fake-response')


def evaluate(case=None, values=None):
    provider = Fake(values or [actor(), review()])
    return evaluate_case(case or fixture(), ROOT / 'skills', provider), provider


def test_two_calls_and_expected_never_leaks_to_actor():
    case = fixture()
    case['expected']['requirements'][0]['text'] += ' SECRET_LABEL'
    result, provider = evaluate(case)
    assert result['status'] == 'passed'
    assert len(provider.calls) == 2
    assert 'SECRET_LABEL' not in provider.calls[0]['user_prompt']
    assert 'SECRET_LABEL' in provider.calls[1]['user_prompt']
    assert 'provenance' not in build_actor_input(case, 'skill', {})
    assert result['goalContext'] == case['goalContext']


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'bad_expected_ref', 'no_review'])
def test_invalid_inputs_do_not_call_provider(mutation):
    case = fixture()
    if mutation == 'missing': del case['inputs']['userRequest']
    if mutation == 'duplicate': case['inputs']['problemFacts'][0]['ref'] = 'request'
    if mutation == 'bad_expected_ref': case['expected']['requirements'][0]['evidenceRefs'] = ['invented']
    if mutation == 'no_review': case['provenance']['independentlyReviewed'] = False
    result, provider = evaluate(case)
    assert result['status'] == 'inputError'
    assert not provider.calls


@pytest.mark.parametrize('mutation', ['format', 'reference'])
def test_invalid_analysis_is_not_success(mutation):
    value = actor()
    if mutation == 'format': del value['unknowns']
    else: value['findings'][0]['evidenceRefs'] = ['invented']
    result, provider = evaluate(values=[value])
    assert result['status'] == 'analysisInvalid'
    assert len(provider.calls) == 1


def test_stale_run_is_visible_but_not_current_evidence():
    case = fixture()
    case['inputs']['runEvidence'] = [{'ref': 'run-old', 'text': '旧记录', 'codeSnapshotRef': 'code-v1'}]
    data = build_actor_input(case, 'skill', {})
    assert data['runEvidenceVersions'] == {'run-old': {'codeSnapshotRef': 'code-v1', 'isCurrent': False}}


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'bad_ref'])
def test_incomplete_review_cannot_pass(mutation):
    value = review()
    if mutation == 'missing': value['checks'].pop('e1')
    if mutation == 'duplicate': value['checks']['unexpected'] = deepcopy(value['checks']['e1'])
    if mutation == 'bad_ref': value['checks']['e1']['evidenceRefs'] = ['invented']
    result, _ = evaluate(values=[actor(), value])
    assert result['status'] == 'reviewIncomplete'


@pytest.mark.parametrize('verdict,expected', [('fail', 'failed'), ('uncertain', 'uncertain')])
def test_review_verdicts(verdict, expected):
    value = review()
    value['checks']['e1']['status'] = verdict
    result, _ = evaluate(values=[actor(), value])
    assert result['status'] == expected


def test_service_failure_is_not_skill_failure():
    from apps.api.deepseek_provider import ProviderFailure
    result, _ = evaluate(values=[ProviderFailure('LLM_UNAVAILABLE', 'secret detail', True)])
    assert result['status'] == 'unavailable'
    assert 'secret detail' not in json.dumps(result)


def test_record_is_exclusive(tmp_path):
    save_record(tmp_path, 'one', {'status': 'passed'})
    with pytest.raises(FileExistsError): save_record(tmp_path, 'one', {'status': 'failed'})
    assert json.loads((tmp_path / 'one.json').read_text())['status'] == 'passed'


def test_run_cannot_bind_to_problem_fact():
    case = fixture()
    case['inputs']['runEvidence'] = [{'ref': 'run', 'text': 'report', 'codeSnapshotRef': 'fact'}]
    result, provider = evaluate(case)
    assert result['status'] == 'inputError'
    assert not provider.calls


def test_invalid_package_prevents_calls(tmp_path):
    provider = Fake([])
    row = evaluate_case(fixture(), tmp_path, provider)
    assert row['status'] == 'inputError'
    assert not provider.calls


def test_critic_service_failure_retains_analysis():
    from apps.api.deepseek_provider import ProviderFailure
    result, provider = evaluate(values=[actor(), ProviderFailure('LLM_UNAVAILABLE', 'secret', True)])
    assert result['status'] == 'unavailable'
    assert result['failedStage'] == 'review'
    assert result['analysis'] == actor()
    assert len(provider.calls) == 2


def test_not_applicable_cannot_force_findings():
    value = actor()
    value['applicability']['status'] = 'notApplicable'
    result, provider = evaluate(values=[value])
    assert result['status'] == 'analysisInvalid'
    assert len(provider.calls) == 1


def test_pass_review_requires_evidence():
    value = review()
    value['checks']['e1']['evidenceRefs'] = []
    result, _ = evaluate(values=[actor(), value])
    assert result['status'] == 'reviewIncomplete'


@pytest.mark.parametrize('identity', ['bad/name', 'manifest', 'summary', 'configuration-failure'])
def test_invalid_record_identity_prevents_calls(identity):
    case = fixture()
    case['id'] = identity
    result, provider = evaluate(case)
    assert result['status'] == 'inputError'
    assert not provider.calls


def test_nonfinite_input_returns_record_without_call():
    case = fixture()
    case['materialVersion'] = float('nan')
    result, provider = evaluate(case)
    assert result['status'] == 'inputError'
    json.dumps(result, allow_nan=False)
    assert result['goalContext'] == case['goalContext']
    assert not provider.calls


def test_independent_review_is_bound_to_source_and_all_cases():
    case = fixture()
    case['provenance']['independentlyReviewed'] = False
    audit = {'reviewedSourceSha256': 'abc', 'reviewer': 'AI-independent',
             'cases': [{'id': case['id'], 'status': 'accepted', 'reason': 'static basis checked'}]}
    result = apply_independent_review([case], audit, 'abc')
    assert result[0]['provenance']['independentlyReviewed'] is True
    assert case['provenance']['independentlyReviewed'] is False
    with pytest.raises(ValueError): apply_independent_review([case], audit, 'changed')
    audit['cases'][0]['status'] = 'needsChange'
    with pytest.raises(ValueError): apply_independent_review([case], audit, 'abc')


def test_invalid_candidate_preserves_debug_fields_but_not_extra_reasoning():
    value = actor()
    value['internal_reasoning'] = 'DO_NOT_LOG'
    result, _ = evaluate(values=[value])
    assert result['status'] == 'analysisInvalid'
    assert result['analysisCandidate']['findings'] == value['findings']
    assert 'DO_NOT_LOG' not in json.dumps(result)
    assert result['responses'][0]['contentHash'].startswith('sha256:')


def test_schema_examples_are_not_problem_material_and_are_not_sent():
    schema = {'type': 'object', 'examples': [{'secretExample': 'not actual material'}],
              'properties': {'examples': {'type': 'string', 'examples': ['annotation']}}}
    model_input = build_actor_input(fixture(), 'skill', schema)
    assert 'examples' not in model_input['outputSchema']
    assert model_input['outputSchema']['properties']['examples'] == {'type': 'string'}
    assert schema['examples']  # Do not mutate the package schema used for validation.


def test_model_review_schema_requires_every_named_check():
    from jsonschema import Draft202012Validator, ValidationError
    result, provider = evaluate()
    sent = json.loads(provider.calls[1]['user_prompt'])
    assert sent['checkIds'] == ['e1', 'evidenceSupport', 'coachingScope', 'applicability', 'unknowns']
    schema = sent['reviewSchema']
    Draft202012Validator(schema).validate(review())
    missing = review()
    del missing['checks']['e1']
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(missing)
    assert isinstance(result['review']['checks'], list)  # Historical record shape retained.
    assert result['reviewProtocolVersion'] == 'named-checks-v1'


def test_review_only_skips_actor_and_preserves_context():
    provider = Fake([review()])
    row = evaluate_case(fixture(), ROOT / 'skills', provider, fixed_analysis=actor())
    assert row['status'] == 'passed'
    assert len(provider.calls) == 1
    assert row['evaluationMode'] == 'reviewOnly'
    assert row['goalContext'] == fixture()['goalContext']
    assert row['responses'][0]['stage'] == 'review'


def test_invalid_fixed_analysis_never_calls_model():
    provider = Fake([])
    bad = actor()
    bad['findings'][0]['evidenceRefs'] = ['nonexistent']
    row = evaluate_case(fixture(), ROOT / 'skills', provider, fixed_analysis=bad)
    assert row['status'] == 'analysisInvalid'
    assert not provider.calls

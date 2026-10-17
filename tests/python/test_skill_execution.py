import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))


def module():
    return importlib.import_module('focusmate.core.skill_execution')


def request():
    d = json.loads((ROOT / 'skills/tree-and-graph/descriptor.json').read_text(encoding='utf-8'))
    return {'goalContext': {'goalId': 'g', 'scope': 'goal'},
        'skillRef': {'skillId': d['skillId'], 'skillVersion': d['version'], 'skillHash': d['hash']},
        'inputs': {'userRequest': {'ref': 'req', 'text': '检查 visited'},
            'goalRequirement': {'ref': 'goal', 'text': '解释遍历'}, 'problemFacts': [],
            'code': {'snapshotRef': 'code', 'text': 'visited.add(node)'}, 'runEvidence': []},
        'dialogue': [], 'hintLevel': 2}


def candidate():
    return {'applicability': {'status': 'applicable', 'reason': '检查遍历', 'evidenceRefs': ['req']},
            'findings': [{'summary': '记录已访问节点', 'evidenceRefs': ['code']}], 'unknowns': []}


class Provider:
    def __init__(self, reviews=('pass',), candidates=None):
        self.reviews = iter(reviews)
        self.candidates = iter(candidates or [candidate(), candidate()])
        self.calls = []

    def complete(self, **kwargs):
        payload = json.loads(kwargs['user_prompt'])
        self.calls.append((kwargs, payload))
        if 'candidate' in payload and 'criteria' in payload:
            verdict = next(self.reviews)
            value = {'checks': {key: {'verdict': verdict, 'reason': '对照材料可验证', 'sourceFacts': '原材料给出当前代码片段。',
                'candidatePointer': item['candidatePointer'], 'quote': item['quote'], 'materialRefs': ['req']}
                for key, item in payload['criteria'].items()}}
        else:
            value = next(self.candidates)
        return module().ProviderResponse(json.dumps(value, ensure_ascii=False), 'fake')


def test_all_pass_accepts_validated_program_invocation():
    p = Provider()
    report = module().execute_skill(request(), p)
    assert report['status'] == 'accepted'
    assert report['invocation']['result'] == candidate()
    assert len(report['trail']) == 2
    assert report['invocation']['goalContext'] == request()['goalContext']
    assert len({c[0]['deadline'] for c in p.calls}) == 1


def test_review_requires_separate_source_facts_and_enumerates_refs():
    p = Provider()
    report = module().execute_skill(request(), p)
    assert report['status'] == 'accepted'
    review_input = p.calls[1][1]
    schema = review_input['reviewSchema']['properties']['checks']['properties']['findings/0']
    assert 'sourceFacts' in schema['required']
    assert set(schema['properties']['materialRefs']['items']['enum']) == {'req', 'goal', 'code'}
    assert review_input['allowedMaterialRefs'] == ['code', 'goal', 'req']


@pytest.mark.parametrize('mutation', [lambda r: r.update(expected={'secret': 1}),
    lambda r: r['goalContext'].update(subgoalId='invalid'),
    lambda r: r['skillRef'].update(skillHash='sha256:' + '0' * 64),
    lambda r: r.update(sections=[]), lambda r: r.update(hintLevel=True),
    lambda r: r['dialogue'].append({'ref': 'req', 'role': 'user', 'text': 'duplicate'})])
def test_invalid_inputs_do_not_call_provider(mutation):
    r, p = request(), Provider()
    mutation(r)
    report = module().execute_skill(r, p)
    assert report['status'] == 'rejected'
    assert report['failure']['stage'] == 'input'
    assert not p.calls


def test_definite_fail_repairs_once_fresh_context():
    p = Provider(('fail', 'pass'))
    report = module().execute_skill(request(), p)
    assert report['status'] == 'accepted' and len(p.calls) == 4
    assert 'issues' in p.calls[2][1] and 'messages' not in p.calls[2][1]
    assert 'expected' not in json.dumps(p.calls)


def test_uncertain_does_not_repair():
    p = Provider(('uncertain',))
    r = module().execute_skill(request(), p)
    assert r['failure']['reason'] == 'REVIEW_UNCERTAIN'
    assert len(p.calls) == 2 and 'result' not in r.get('invocation', {})


def test_malformed_candidate_reference_no_review_or_repair():
    c = candidate(); c['findings'][0]['evidenceRefs'] = ['hidden']
    p = Provider(candidates=[c])
    r = module().execute_skill(request(), p)
    assert r['failure']['reason'] == 'INVALID_CANDIDATE' and len(p.calls) == 1


def test_second_failure_never_third_candidate():
    p = Provider(('fail', 'fail'))
    r = module().execute_skill(request(), p)
    assert r['status'] == 'rejected' and len(p.calls) == 4


def test_fixed_candidate_review_uses_same_reviewer():
    p = Provider()
    r = module().review_candidate(request(), candidate(), p)
    assert r['status'] == 'accepted' and len(p.calls) == 1
    assert set(p.calls[0][1]['criteria']) == {'applicability', 'findings/0', 'overall'}

def test_empty_arrays_still_have_global_scope_review():
    c = candidate(); c['findings'] = []; c['unknowns'] = ['缺少相关边界条件']
    p = Provider(candidates=[c])
    r = module().execute_skill(request(), p)
    assert r['status'] == 'accepted'
    assert set(p.calls[1][1]['criteria']) == {'applicability', 'unknowns/0', 'overall'}


@pytest.mark.parametrize('fault', ['missing', 'quote', 'pointer', 'refs', 'extra', 'invalid_json'])
def test_invalid_reviews_never_repair(fault):
    class Invalid(Provider):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            if len(self.calls) != 2:
                return response
            value = json.loads(response.content)
            if fault == 'missing': del value['checks']['overall']
            if fault == 'quote': value['checks']['overall']['quote'] = 'invented'
            if fault == 'pointer': value['checks']['overall']['candidatePointer'] = '/wrong'
            if fault == 'refs': value['checks']['overall']['materialRefs'] = ['private']
            if fault == 'extra': value['checks']['extra'] = value['checks']['overall']
            return module().ProviderResponse('bad' if fault == 'invalid_json' else json.dumps(value))
    # This tests malformed review content, not elapsed wall time. Host pauses
    # must not turn it into a deadline test; expiry is tested separately below.
    p = Invalid(); r = module().execute_skill(request(), p, clock=lambda: 0.0)
    assert r['failure']['reason'] == 'INVALID_REVIEW' and len(p.calls) == 2
    assert len(r['trail']) == 2


def test_provider_failure_retains_first_candidate_and_has_typed_stage():
    class Broken(Provider):
        def complete(self, **kwargs):
            if self.calls:
                raise module().ProviderError('PROVIDER_UNAVAILABLE')
            return super().complete(**kwargs)
    r = module().execute_skill(request(), Broken())
    assert r['failure'] == {'stage': 'review', 'reason': 'PROVIDER_UNAVAILABLE'}
    assert len(r['trail']) == 1
    assert 'result' not in r['invocation']


def test_late_candidate_return_is_preserved_but_not_accepted():
    now = [0.0]
    class Late(Provider):
        def complete(self, **kwargs):
            value = super().complete(**kwargs); now[0] = 121
            return value
    p = Late(); r = module().execute_skill(request(), p, clock=lambda: now[0])
    assert r['failure']['reason'] == 'DEADLINE_EXCEEDED' and len(p.calls) == 1
    assert len(r['trail']) == 1


def test_mixed_fail_and_uncertain_does_not_repair():
    class Mixed(Provider):
        def complete(self, **kwargs):
            value = super().complete(**kwargs)
            if len(self.calls) == 2:
                raw = json.loads(value.content); raw['checks']['overall']['verdict'] = 'uncertain'
                return module().ProviderResponse(json.dumps(raw))
            return value
    p = Mixed(('fail',)); r = module().execute_skill(request(), p)
    assert r['failure']['reason'] == 'REVIEW_UNCERTAIN' and len(p.calls) == 2

def test_overall_quote_is_plain_candidate_text():
    assert module().review_criteria(candidate())['overall']['quote'] == candidate()['applicability']['reason']


def test_cli_preserves_stages_and_refuses_existing_directory(tmp_path):
    import runpy
    cli = runpy.run_path(str(ROOT / 'tools/run-skill.py'))
    source = tmp_path / 'input.json'; source.write_text(json.dumps(request()), encoding='utf-8')
    out = tmp_path / 'out'
    assert cli['main']([str(source), '--output-dir', str(out)], provider_factory=Provider) == 0
    assert json.loads((out / 'report.json').read_text(encoding='utf-8'))['status'] == 'accepted'
    assert len(list(out.glob('stage-*.json'))) == 2
    with pytest.raises(FileExistsError):
        cli['main']([str(source), '--output-dir', str(out)], provider_factory=Provider)

@pytest.mark.parametrize('budget', ['120', None, float('nan'), float('inf'), -1])
def test_invalid_budget_is_typed_input_failure(budget):
    p = Provider()
    r = module().execute_skill(request(), p, deadline_seconds=budget)
    assert r['failure']['stage'] == 'input' and not p.calls


def test_adapter_forwards_exact_deadline_and_discards_nonvisible_fields():
    from types import SimpleNamespace
    from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
    from apps.api.deepseek_provider import DeepSeekConfig
    class Transport:
        def complete(self, **kwargs):
            assert kwargs['deadline'] == 43.5
            return SimpleNamespace(content='{}', model='configured', reasoning_content='SECRET')
    provider = DeepSeekSkillExecutionProvider(DeepSeekConfig('SECRET', 'https://example.test', 'model'), transport=Transport())
    value = provider.complete(system_prompt='s', user_prompt='u', deadline=43.5)
    assert value == module().ProviderResponse('{}', 'configured')
    assert 'SECRET' not in json.dumps(provider.public_config)

def test_failed_call_is_counted_with_exact_prompt_snapshot():
    class Broken(Provider):
        def complete(self, **kwargs):
            raise module().ProviderError('PROVIDER_UNAVAILABLE')
    r = module().execute_skill(request(), Broken())
    assert r['logicalRequests'] == 1
    assert r['requests'][0]['systemPrompt'] == module().ACTOR_PROMPT
    assert json.loads(r['requests'][0]['userPrompt'])['goalContext'] == request()['goalContext']
    assert r['promptVersion']

def test_fixed_null_candidate_never_calls_actor():
    p = Provider()
    r = module().review_candidate(request(), None, p)
    assert r['failure']['reason'] == 'INVALID_CANDIDATE' and not p.calls


def test_selected_body_excludes_unselected_text():
    r = request()
    index = json.loads((ROOT / 'skills/tree-and-graph/references/index.json').read_text(encoding='utf-8'))
    r['sections'] = [index['sections'][0]['sectionId']]
    p = Provider(); report = module().execute_skill(r, p)
    assert report['status'] == 'accepted'
    assert set(p.calls[0][1]['skill']['sections']) == set(r['sections'])


def test_stale_evidence_is_labeled_without_rewriting_original_materials():
    r = request(); r['inputs']['runEvidence'] = [{'ref': 'old-run', 'text': 'passed', 'codeSnapshotRef': 'old-code'}]
    p = Provider(); report = module().execute_skill(r, p)
    assert report['status'] == 'accepted'
    assert p.calls[0][1]['materials']['runEvidence'] == r['inputs']['runEvidence']
    assert p.calls[0][1]['runEvidenceVersions']['old-run']['isCurrent'] is False

def test_unexpected_transport_exception_is_service_failure_not_bad_review():
    class Broken(Provider):
        def complete(self, **kwargs):
            if self.calls:
                raise TimeoutError('secret endpoint details')
            return super().complete(**kwargs)
    r = module().execute_skill(request(), Broken())
    assert r['failure'] == {'stage': 'review', 'reason': 'PROVIDER_FAILED'}
    assert 'secret endpoint' not in json.dumps(r)


@pytest.mark.parametrize('valid', [True, False])
def test_cli_loads_explicit_environment_only_after_input_passes(tmp_path, monkeypatch, valid):
    import runpy, os
    monkeypatch.delenv('T054_TEST_ENV', raising=False)
    env = tmp_path/'chosen.env';env.write_text('T054_TEST_ENV=selected\n', encoding='utf-8')
    source=tmp_path/'request.json';source.write_text(json.dumps(request() if valid else {}), encoding='utf-8')
    calls=[]
    def factory():
        calls.append(os.environ.get('T054_TEST_ENV'))
        return Provider()
    cli=runpy.run_path(str(ROOT/'tools/run-skill.py'))
    result=cli['main']([str(source),'--output-dir',str(tmp_path/'out'),'--env-file',str(env)],provider_factory=factory)
    assert result==(0 if valid else 1)
    assert calls==(['selected'] if valid else [])
    if not valid: assert 'T054_TEST_ENV' not in os.environ

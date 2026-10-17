from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.skill_evaluation import digest


def fixture():
    from tools.skill_routing_cases import get_cases
    case = get_cases('development')[0]
    case.update(skillId='tree-and-graph', id='anonymous-case')
    case['goalContext'] = {'goalId': 'current', 'scope': 'subgoal', 'subgoalId': 'current'}
    package = Path('skills/tree-and-graph')
    packages = {'tree-and-graph': {p.relative_to(package).as_posix(): p.read_text(encoding='utf-8') for p in package.rglob('*') if p.is_file()}}
    audit = {'reviewer': 'AI-independent', 'humanReviewed': False,
             'cases': [{'id': case['id'], 'caseHash': digest(case), 'status': 'accepted', 'reason': 'test fixture'}]}
    return case, packages, audit


class Fake:
    public_config = {'model': 'fake'}

    def __init__(self, *values):
        self.values = iter(values)
        self.requests = []

    def complete(self, **request):
        self.requests.append(request)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value),
                               model='fake', response_id='test')


def setup(arms=('ordinary', 'full', 'selective')):
    from tools.skill_improvement_experiment import freeze
    case, packages, audit = fixture()
    manifest = freeze([case], packages, {}, audit, arms=arms)
    return manifest, manifest['cases'][0]


def test_payload_is_fair_and_no_expectation_leaks():
    from tools.skill_improvement_experiment import generate
    manifest, case = setup()
    for arm in ('ordinary', 'full', 'selective'):
        hint = {'hint': '可以统一改名。', 'evidenceRefs': ['request:1']}
        provider = Fake({'action': 'directHint', **hint} if arm == 'selective' else hint)
        row = generate({**manifest['schedule'][0], 'arm': arm}, case, manifest, provider)
        assert row['status'] == 'generated'
        payload = json.loads(provider.requests[0]['user_prompt'])
        assert payload['materials'] == case['payload']['materials']
        assert not {'expected', 'caseId', 'skillId', 'provenance', 'arm'} & payload.keys()
        assert 'anonymous-case' not in provider.requests[0]['user_prompt']


def test_select_only_requested_sections_two_calls():
    from tools.skill_improvement_experiment import generate
    manifest, case = setup()
    route = {'action': 'useSkill', 'skillId': 'tree-and-graph', 'version': json.loads(manifest['packages']['tree-and-graph']['descriptor.json'])['version'],
             'sectionIds': ['visit-control'], 'reason': '核对局部访问顺序', 'evidenceRefs': ['code:v2']}
    provider = Fake(route, {'hint': '检查访问标记。', 'evidenceRefs': ['code:v2']})
    row = generate({**manifest['schedule'][0], 'arm': 'selective'}, case, manifest, provider)
    assert row['logicalCallCount'] == 2
    assert set(row['loaded']['sections']) == {'visit-control'}
    first = json.loads(provider.requests[0]['user_prompt'])
    assert 'skill' not in first
    assert '只说明当前局部风险' not in provider.requests[0]['user_prompt']
    assert 'skillDirectory' not in json.loads(provider.requests[1]['user_prompt'])


@pytest.mark.parametrize('change', [{'version': 99}, {'skillId': 'made-up'},
    {'sectionIds': ['visit-control', 'visit-control']}, {'evidenceRefs': ['fake']}])
def test_bad_selection_never_gets_second_request(change):
    from tools.skill_improvement_experiment import generate
    manifest, case = setup()
    value = {'action': 'useSkill', 'skillId': 'tree-and-graph', 'version': json.loads(manifest['packages']['tree-and-graph']['descriptor.json'])['version'],
             'sectionIds': ['visit-control'], 'reason': 'check', 'evidenceRefs': ['code:v2']}
    provider = Fake({**value, **change})
    row = generate({**manifest['schedule'][0], 'arm': 'selective'}, case, manifest, provider)
    assert row['status'] == 'invalid' and len(provider.requests) == 1


def test_invalid_output_keeps_raw_and_no_repair():
    from tools.skill_improvement_experiment import generate
    manifest, case = setup()
    row = generate(manifest['schedule'][0], case, manifest, Fake('not json'))
    assert row['status'] == 'invalid'
    assert row['calls'][0]['response']['content'] == 'not json'
    assert row['logicalCallCount'] == 1


def test_malformed_provider_envelope_is_invalid_not_service_unavailable():
    from apps.api.deepseek_provider import ProviderFailure
    from tools.skill_improvement_experiment import generate
    manifest, case = setup()
    row = generate(manifest['schedule'][0], case, manifest,
                   Fake(ProviderFailure('LLM_OUTPUT_INVALID', 'not logged', False)))
    assert row['status'] == 'invalid'
    assert row['failureReason'] == 'LLM_OUTPUT_INVALID'


def test_regression_exit_status_does_not_hide_failures():
    import importlib.util
    spec = importlib.util.spec_from_file_location('regression_cli', Path('tools/run-skill-regression.py'))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.exit_status({'mode': 'calibration', 'records': 24, 'matched': 23}) == 1
    assert cli.exit_status({'mode': 'calibration', 'records': 24, 'matched': 24}) == 0
    assert cli.exit_status({'mode': 'regression', 'records': 54, 'statuses': {'accepted': 53, 'rejected': 1}}) == 1
    assert cli.exit_status({'mode': 'regression', 'records': 54, 'statuses': {'accepted': 54}}) == 0


def test_bad_case_or_audit_rejected_before_calls():
    from tools.skill_improvement_experiment import freeze
    case, packages, audit = fixture()
    case['hintLevel'] = 0
    with pytest.raises(ValueError):
        freeze([case], packages, {}, audit)


def test_disk_independent_freeze_and_tamper_check():
    from tools.skill_improvement_experiment import freeze, generate
    case, packages, audit = fixture()
    manifest = freeze([case], packages, {}, audit)
    packages['tree-and-graph']['SKILL.md'] = 'changed after freeze'
    assert manifest['packages']['tree-and-graph']['SKILL.md'] != 'changed after freeze'
    manifest['packages']['tree-and-graph']['SKILL.md'] = 'tamper'
    provider = Fake()
    row = generate(manifest['schedule'][0], manifest['cases'][0], manifest, provider)
    assert row['status'] == 'invalid' and not provider.requests


def test_records_are_exclusive_and_blind(tmp_path):
    from tools.skill_improvement_experiment import run
    case, packages, audit = fixture()
    hint = {'hint': '统一改名即可。', 'evidenceRefs': ['request:1']}
    provider = Fake(hint, hint)
    out = tmp_path / 'result'
    result = run([case], packages, provider, out, audit, arms=('ordinary',), workers=1)
    assert result['counts'] == {'generated': 2}
    packet = json.loads((out / 'blind-packet.json').read_text(encoding='utf-8'))
    assert len(packet['items']) == 2
    assert not {'arm', 'caseId', 'skillId', 'expected'} & packet['items'][0].keys()
    with pytest.raises(FileExistsError):
        run([case], packages, provider, out, audit, workers=1)


@pytest.mark.parametrize('damage',['chapter','output-ref'])
def test_loaded_baseline_buffers_are_checked_beyond_their_hash(damage):
    from focusmate.contracts.skill_files import fingerprint, PackageError
    from tools.skill_improvement_experiment import freeze
    case,packages,audit=fixture();files=packages['tree-and-graph']
    if damage=='chapter': files['SKILL.md']=files['SKILL.md'].replace('## 目标','## removed')
    else:
        schema=json.loads(files['schemas/result.schema.json'])
        schema['$ref']='missing.schema.json';files['schemas/result.schema.json']=json.dumps(schema)
    descriptor=json.loads(files['descriptor.json'])
    descriptor['hash']=fingerprint(files,descriptor)[0]
    files['descriptor.json']=json.dumps(descriptor)
    with pytest.raises((ValueError,PackageError)):
        freeze([case],packages,{},audit,arms=('full',))

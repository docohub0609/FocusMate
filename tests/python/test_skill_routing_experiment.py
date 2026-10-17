from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from jsonschema.exceptions import ValidationError

from tools.skill_routing_experiment import validate_hint, validate_route, common_payload


def test_hint_rejects_unknown_reference():
    with pytest.raises(ValueError):
        validate_hint({'hint': '检查这里', 'evidenceRefs': ['fake']}, {'code:1'})


def test_route_requires_explicit_identity_and_two_sections_maximum():
    metadata = {'skillId': 'tree-and-graph', 'version': 4,
                'sections': [{'id': 'visit'}, {'id': 'coverage'}, {'id': 'path'}]}
    route = {'action': 'useSkill', 'skillId': 'tree-and-graph', 'version': 4,
             'sectionIds': ['visit'], 'reason': '检查重复访问', 'evidenceRefs': ['code:1']}
    validate_route(route, metadata, {'code:1'})
    for changes in ({'version': 3}, {'sectionIds': ['visit', 'coverage', 'path']},
                    {'sectionIds': ['visit', 'visit']}, {'evidenceRefs': ['fake']}):
        with pytest.raises((ValueError, ValidationError)):
            validate_route({**route, **changes}, metadata, {'code:1'})


def fixture_run():
    from pathlib import Path
    from tools.skill_routing_cases import get_cases
    from tools.skill_evaluation import digest
    from tools.skill_routing_experiment import freeze
    cases = get_cases('development')[:1]
    audit = {'reviewer': 'AI-independent', 'humanReviewed': False,
             'cases': [{'id': c['id'], 'caseHash': digest(c), 'status': 'accepted', 'reason': 'fixture only'} for c in cases]}
    return freeze(cases, Path(__file__).resolve().parents[2] / 'skills/tree-and-graph', 7, {}, audit)


class Fake:
    public_config = {}

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, **request):
        self.requests.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value),
                               model='fake', response_id='fake')


def test_direct_one_request_and_selected_two_requests_only_requested_sections():
    from tools.skill_routing_experiment import generate
    manifest, snapshot = fixture_run()
    case = manifest['cases'][0]
    entry = {**manifest['schedule'][0], 'arm': 'selective'}
    hint = {'hint': '局部提示', 'evidenceRefs': ['code:v2']}
    direct = Fake([{'action': 'directHint', **hint}])
    row = generate(entry, case, manifest, snapshot, direct)
    assert row['status'] == 'generated' and row['logicalCallCount'] == 1
    first = json.loads(direct.requests[0]['user_prompt'])
    assert 'skill' not in first and 'skillDirectory' in first
    chosen = snapshot.sections[0].section_id
    selected = Fake([{'action': 'useSkill', 'skillId': snapshot.skill_id, 'version': snapshot.version,
                      'sectionIds': [chosen], 'reason': '局部检查', 'evidenceRefs': ['code:v2']}, hint])
    row = generate(entry, case, manifest, snapshot, selected)
    assert row['status'] == 'generated' and row['logicalCallCount'] == 2
    second = json.loads(selected.requests[1]['user_prompt'])
    assert set(second['skill']['sections']) == {chosen}
    full = Fake([hint])
    generate({**entry, 'arm': 'full'}, case, manifest, snapshot, full)
    full_payload = json.loads(full.requests[0]['user_prompt'])
    assert full_payload['skill']['common'] == second['skill']['common']
    for key in ('materials', 'hintLevel', 'dialogue', 'goalContext', 'outputSchema'):
        assert second[key] == full_payload[key]


@pytest.mark.parametrize('response', ['broken', {'hint': 'x', 'evidenceRefs': ['fake']},
                                      {'action': 'useSkill', 'version': 999}])
def test_invalid_response_stops_without_repair(response):
    from tools.skill_routing_experiment import generate
    manifest, snapshot = fixture_run()
    fake = Fake([response])
    row = generate({**manifest['schedule'][0], 'arm': 'selective'}, manifest['cases'][0], manifest, snapshot, fake)
    assert row['status'] == 'invalid' and len(fake.requests) == 1
    assert row['calls'][0]['response']['content']


def test_unavailable_and_history_not_overwritten(tmp_path):
    from pathlib import Path
    from tools.skill_routing_experiment import run
    from tools.skill_routing_cases import get_cases
    from tools.skill_evaluation import digest
    cases = get_cases('development')[:1]
    audit = {'reviewer': 'AI-independent', 'humanReviewed': False,
             'cases': [{'id': c['id'], 'caseHash': digest(c), 'status': 'accepted', 'reason': 'fixture'} for c in cases]}
    path = tmp_path / 'run'
    result = run(cases, Path(__file__).resolve().parents[2] / 'skills/tree-and-graph', 7, None, path, audit)
    assert result['counts'] == {'unavailable': 6}
    before = (path / 'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):
        run(cases, Path('skills/tree-and-graph'), 7, None, path, audit)
    assert (path / 'manifest.json').read_bytes() == before


@pytest.mark.parametrize('mutation', ['schema', 'context', 'dialogue', 'audit', 'runReference'])
def test_preflight_errors_are_recorded_without_model_calls(tmp_path, mutation):
    from pathlib import Path
    from tools.skill_routing_experiment import run
    from tools.skill_routing_cases import get_cases
    from tools.skill_evaluation import digest
    cases = get_cases('development')[:1]
    if mutation == 'schema': cases[0]['inputs']['code']['text'] = 42
    if mutation == 'context': cases[0]['goalContext'] = None
    if mutation == 'dialogue': cases[0]['dialogue'] = [{'ref': 'd', 'role': 'user', 'text': 42}]
    if mutation == 'runReference':
        cases[0]['inputs']['runEvidence'] = [{'ref': 'run:1', 'text': 'old record', 'codeSnapshotRef': 'request:1'}]
    audit = {'reviewer': 'AI-independent', 'humanReviewed': False,
             'cases': [{'id': c['id'], 'caseHash': digest(c), 'status': 'accepted', 'reason': 'fixture'} for c in cases]}
    fake = Fake([])
    result = run(cases, Path(__file__).resolve().parents[2] / 'skills/tree-and-graph', 7,
                 fake, tmp_path / mutation, None if mutation == 'audit' else audit)
    assert result['status'] == 'inputError' and fake.requests == []


def test_service_failure_records_one_attempt():
    from apps.api.deepseek_provider import ProviderFailure
    from tools.skill_routing_experiment import generate
    manifest, snapshot = fixture_run()
    fake = Fake([ProviderFailure('UNAVAILABLE', 'fixture', retryable=True)])
    row = generate({**manifest['schedule'][0], 'arm': 'ordinary'}, manifest['cases'][0], manifest, snapshot, fake)
    assert row['status'] == 'unavailable' and row['logicalCallCount'] == 1


def test_selection_then_file_change_stops_before_second_call(tmp_path):
    import shutil
    from pathlib import Path
    from tools.skill_routing_experiment import freeze, generate
    from tools.skill_routing_cases import get_cases
    from tools.skill_evaluation import digest
    path = tmp_path / 'tree-and-graph'
    shutil.copytree(Path(__file__).resolve().parents[2] / 'skills/tree-and-graph', path)
    cases = get_cases('development')[:1]
    audit = {'reviewer': 'AI-independent', 'humanReviewed': False,
             'cases': [{'id': c['id'], 'caseHash': digest(c), 'status': 'accepted', 'reason': 'fixture'} for c in cases]}
    manifest, snapshot = freeze(cases, path, 7, {}, audit)
    route = {'action': 'useSkill', 'skillId': snapshot.skill_id, 'version': snapshot.version,
             'sectionIds': [snapshot.sections[0].section_id], 'reason': '需要', 'evidenceRefs': ['code:v2']}
    class Drifting(Fake):
        def complete(self, **request):
            result = super().complete(**request)
            with (path / snapshot.sections[-1].file_ref).open('a', encoding='utf-8') as stream:
                stream.write('\nchanged')
            return result
    fake = Drifting([route])
    result = generate({**manifest['schedule'][0], 'arm': 'selective'}, manifest['cases'][0], manifest, snapshot, fake)
    assert result['status'] == 'invalid' and len(fake.requests) == 1


def test_payload_does_not_leak_case_labels_or_expected():
    case = {'id': 'secret', 'expected': {'answer': 'secret'}, 'phase': 'holdout',
            'goalContext': {'goalId': 'g', 'subgoalId': 's'}, 'hintLevel': 2,
            'dialogue': [], 'inputs': {'userRequest': {'ref': 'u', 'text': '问'},
            'goalRequirement': {'ref': 'g', 'text': '目标'}, 'problemFacts': [],
            'code': {'snapshotRef': 'c', 'text': ''}, 'runEvidence': []}}
    payload = common_payload(case)
    assert 'secret' not in json.dumps(payload)
    assert payload['hintLevel'] == 2

"""Material checks are structural/factual checks, not semantic acceptance."""
from collections import Counter
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from tools.skill_improvement_cases import ACTOR_FIELDS, SKILLS, get_cases, get_reviewer_cases


ROOT = Path(__file__).resolve().parents[2]


def _refs(case):
    data = case['inputs']
    return {data['userRequest']['ref'], data['goalRequirement']['ref'],
            data['code']['snapshotRef'],
            *[f['ref'] for f in data['problemFacts']],
            *[r['ref'] for r in data['runEvidence']],
            *[d['ref'] for d in case['dialogue']]}


def test_fixed_balanced_counts_and_unique_opaque_identity():
    development, holdout = get_cases('development'), get_cases('holdout')
    assert len(development) == 18
    assert len(holdout) == 27
    assert [c['id'] for c in development + holdout] == [f'case-{i:03d}' for i in range(1, 46)]
    assert Counter((c['skillId'], c['category']) for c in development) == {
        (skill, category): 1 for skill in SKILLS for category in ('risk', 'reasonable')}
    assert Counter((c['skillId'], c['category']) for c in holdout) == {
        (skill, category): 1 for skill in SKILLS for category in ('issue', 'reasonable', 'simple')}


def test_input_schemas_context_and_truthful_provenance():
    for case in get_cases('development') + get_cases('holdout') + get_reviewer_cases():
        schema = json.loads((ROOT / 'skills' / case['skillId'] / 'schemas/input.schema.json').read_text(encoding='utf-8'))
        Draft202012Validator(schema).validate(case['inputs'])
        assert case['goalContext'] == {'goalId': 'coaching', 'scope': 'subgoal', 'subgoalId': 'current-question'}
        assert type(case['hintLevel']) is int and 1 <= case['hintLevel'] <= 4
        assert case['materialVersion'] == 1
        assert case['inputs']['runEvidence'] == []
        assert case['provenance']['draftedBy'] == 'AI'
        assert case['provenance']['humanReviewed'] is False
        assert case['provenance']['conversationSource'] == 'fixed-authored-scenario'
        for source in case['provenance']['sources']:
            if source.startswith('historical-case:'):
                # Historical identifiers are provenance only; private reports
                # are deliberately not shipped or used as execution evidence.
                assert source.removeprefix('historical-case:').strip()
            else:
                assert (ROOT / source).is_file()
        assert set(case['expected']) == {'reason', 'acceptable', 'forbidden', 'evidenceRefs'}
        assert set(case['expected']['evidenceRefs']) <= _refs(case)
        assert all(case['expected'][key] for key in case['expected'])


def test_actor_material_carries_no_case_labels_or_identity_in_references():
    for case in get_cases('development') + get_cases('holdout') + get_reviewer_cases():
        actor = {key: case[key] for key in ACTOR_FIELDS}
        encoded = json.dumps(actor, ensure_ascii=False)
        for secret in (case['id'], case['skillId'], '"expected"', '"provenance"',
                       '"category"', '"phase"', '"pairId"', '"expectedVerdict"'):
            assert secret not in encoded
        assert _refs(case) <= {'request:1', 'goal:1', 'code:v2', 'fact:1', 'fact:2',
                               'dialogue:1', 'dialogue:2', 'dialogue:3'}


def test_independent_returns_and_explicit_phase():
    first = get_cases('development')
    first[0]['inputs']['userRequest']['text'] = 'changed'
    assert get_cases('development')[0]['inputs']['userRequest']['text'] != 'changed'
    with pytest.raises(ValueError):
        get_cases('other')
    reviewer = get_reviewer_cases()
    reviewer[0]['analysis']['findings'].clear()
    assert get_reviewer_cases()[0]['analysis']['findings']


def test_new_holdout_material_does_not_reuse_development_request_or_code():
    old = get_cases('development')
    for case in get_cases('holdout'):
        same_pack = [c for c in old if c['skillId'] == case['skillId']]
        assert all(case['inputs']['userRequest'] != c['inputs']['userRequest'] for c in same_pack)
        if case['inputs']['code']['text']:
            assert all(case['inputs']['code']['text'] != c['inputs']['code']['text'] for c in same_pack)
        if case['category'] == 'simple':
            assert case['hintLevel'] == 1
            assert '只' in case['inputs']['userRequest']['text']


def test_reviewer_pairs_share_material_and_have_valid_local_citations():
    cases = get_reviewer_cases()
    assert len(cases) == 12
    assert len({c['id'] for c in cases}) == 12
    assert Counter(c['expectedVerdict'] for c in cases) == {'fail': 6, 'pass': 6}
    assert len({c['calibrationKind'] for c in cases}) == 6
    for bad, good in zip(cases[::2], cases[1::2]):
        assert bad['pairId'] == good['pairId']
        assert {k: bad[k] for k in ACTOR_FIELDS} == {k: good[k] for k in ACTOR_FIELDS}
        assert bad['analysis'] != good['analysis']
        for case in (bad, good):
            schema = json.loads((ROOT / 'skills' / case['skillId'] / 'schemas/result.schema.json').read_text(encoding='utf-8'))
            # Schema includes program-injected identity alongside analysis fields.
            analysis_schema = {'type': 'object', 'properties': {
                k: schema['properties'][k] for k in ('applicability', 'findings', 'unknowns')},
                '$defs': schema.get('$defs', {})}
            Draft202012Validator(analysis_schema).validate(case['analysis'])
            assert set(case['analysis']['applicability']['evidenceRefs']) <= _refs(case)
            assert all(set(f['evidenceRefs']) <= _refs(case) for f in case['analysis']['findings'])


def test_documented_arithmetic_and_order_expectations():
    assert 3 - 8 == -5
    assert sum(1 for i in range(30) for _ in range(i + 1, 30)) == 435
    graph = {0: [1, 2], 1: [3], 2: [3], 3: []}
    queue, seen, popped = [0], {0}, []
    while queue:
        u = queue.pop(0)
        popped.append(u)
        for v in graph[u]:
            if v not in seen:
                queue.append(v)
                seen.add(v)
    assert popped == [0, 1, 2, 3]
    assert set('abb') == set('aab')
    assert min(0, 4 + 3) == 0
    freq = {'a': 1, 'b': 1}
    freq['b'] += 1
    freq['a'] -= 1
    assert freq['b'] == 2
    assert not (17 < 17)


def test_full_solution_calibration_is_whole_program_not_requested_local_code():
    bad, good = [c for c in get_reviewer_cases() if c['calibrationKind'] == 'full-solution']
    assert bad['hintLevel'] == 1
    assert '只给我一个观察问题' in bad['inputs']['userRequest']['text']
    assert 'def count_components' in bad['analysis']['findings'][0]['summary']
    assert '```' not in good['analysis']['findings'][0]['summary']

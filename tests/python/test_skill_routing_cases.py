import json
from collections import Counter, deque
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools.skill_routing_cases import get_cases


ROOT = Path(__file__).resolve().parents[2]


def _case(suffix):
    return next(case for phase in ('development', 'holdout') for case in get_cases(phase)
                if case['id'].endswith(suffix))


def test_fixed_splits_use_original_schema_and_resolved_references():
    schema = json.loads((ROOT / 'skills/tree-and-graph/schemas/input.schema.json').read_text(encoding='utf-8'))
    ids = set()
    codes = set()
    for phase in ('development', 'holdout'):
        cases = get_cases(phase)
        assert len(cases) == 8
        assert Counter(case['hintLevel'] for case in cases) == {1: 2, 2: 2, 3: 2, 4: 2}
        for case in cases:
            assert case['id'] not in ids
            ids.add(case['id'])
            assert case['inputs']['code']['text'] not in codes
            codes.add(case['inputs']['code']['text'])
            Draft202012Validator(schema).validate(case['inputs'])
            assert case['inputs']['runEvidence'] == []
            assert case['phase'] == phase
            assert all(case['goalContext'].values())
            assert case['materialVersion'] == 1
            assert case['provenance']['independentlyReviewed'] is False
            records = [case['inputs']['userRequest'], case['inputs']['goalRequirement'],
                       *case['inputs']['problemFacts'], *case['dialogue']]
            refs = [record['ref'] for record in records] + [case['inputs']['code']['snapshotRef']]
            assert len(refs) == len(set(refs))
            assert set(case['expected']['evidenceRefs']) <= set(refs)
            assert set(case['expected']) == {'reason', 'acceptable', 'forbidden', 'evidenceRefs'}
            assert all(case['expected'].values())
            assert all(turn['role'] in ('user', 'assistant') for turn in case['dialogue'])


def test_return_values_do_not_share_mutable_conversations_or_expectations():
    first = get_cases('development')
    reference = get_cases('development')
    first[-1]['dialogue'].append({'ref': 'generated', 'role': 'assistant', 'text': 'adaptive'})
    first[0]['expected']['acceptable'].clear()
    assert get_cases('development') == reference
    with pytest.raises(ValueError):
        get_cases('all')


def test_diamond_reference_count_is_reproduced_from_actual_case_code():
    state = {'adj': {0: [1, 2], 1: [3], 2: [3], 3: []},
             'queue': [0], 'seen': set(), 'count': 0}
    exec(_case('diamond-count')['inputs']['code']['text'], state)
    assert state['count'] == 5
    assert state['seen'] == {0, 1, 2, 3}


def test_coverage_reference_reaches_exactly_first_component():
    state = {'adj': {0: [1], 1: [0], 2: [3], 3: [2]}}
    exec(_case('island-coverage')['inputs']['code']['text'], state)
    assert state['seen'] == {0, 1}
    assert state['answer'] == 1


def test_parent_comparison_counterexample_uses_supplied_snapshot():
    state = {'parent': [0, 0, 1]}
    exec(_case('stale-dsu-claim')['inputs']['code']['text'], state)
    assert state['connected'](0, 2) is False
    assert state['parent'][state['parent'][2]] == state['parent'][0] == 0


def test_weighted_reference_blocks_the_cheaper_route():
    state = {'adj': {'s': [('a', 10), ('b', 1)], 'a': [], 'b': [('a', 1)]},
             'queue': deque(['s']), 'dist': {'s': 0}}
    code = _case('weighted-first-arrival')['inputs']['code']['text']
    while state['queue']:
        exec(code, state)
    assert state['dist'] == {'s': 0, 'a': 10, 'b': 1}
    assert state['dist']['b'] + 1 == 2 < state['dist']['a']


def test_isolated_vertex_never_enters_endpoint_derived_set():
    state = {'edges': [('A', 'B')]}
    exec(_case('isolated-build-task')['inputs']['code']['text'], state)
    assert state['nodes'] == {'A', 'B'}
    assert 'Z' not in state['nodes']

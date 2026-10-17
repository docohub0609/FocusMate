from collections import Counter
from copy import deepcopy

import pytest

from tools.stage_local_evaluation import load_cases, freeze_experiment


def test_new_protocol_separates_old_regressions_and_fresh_tasks():
    cases = load_cases()
    assert Counter(c['group'] for c in cases) == {'regression': 24, 'new': 12}
    assert Counter(c['input']['mode'] for c in cases if c['group'] == 'new') == {
        'stage_only': 6, 'assist_only': 4, 'combined': 2}
    assert len(freeze_experiment(cases, {}, 'new')['schedule']) == 24
    assert len(freeze_experiment(cases, {}, 'regression')['schedule']) == 48


def test_modes_and_scoring_are_separate_from_provider_materials():
    cases = load_cases()
    manifest = freeze_experiment(cases, {}, 'new')
    assert manifest['version'] == 't062-request-evaluation-2'
    assert 'reasonSupport' in manifest['rubric']
    assert 'src/focusmate/core/code_materials.py' in manifest['implementationSnapshots']
    assert manifest['guidance']
    case = deepcopy(cases[0]); case['input']['mode'] = 'guess'
    with pytest.raises(ValueError): freeze_experiment([case], {}, 'all')


def test_stage_only_cases_do_not_require_a_tool_judgment():
    for case in load_cases():
        if case['input']['mode'] == 'stage_only':
            assert case['expected']['shouldRunLocal'] is None
        if case['input']['mode'] == 'assist_only':
            assert case['expected']['stage'] is None

import json

import pytest

from focusmate.contracts.skill_execution import ProviderError, ProviderResponse
from focusmate.core.context_builder import MapContextResolver, hash_content
from focusmate.core.stage_recognition import recognize_stage


GOAL = {'goalId': 'goal-stage', 'subgoalId': 'write-loop', 'scope': 'subgoal'}


def fixture():
    contents = {
        'goal:v3': ('goal', {'requirement': '完成数组题，并能解释循环边界'}),
        'problem:v2': ('problemFact', {'text': '返回两数之和的下标。'}),
        'code:v4': ('code', {'snapshotRef': 'code:v4', 'language': 'python3',
                             'text': 'def solve(nums, target):\n    seen = {}\n'}),
        'observation:7': ('observation', {'text': '用户连续保存了函数声明和初始化。',
                                          'codeSnapshotRef': 'code:v4'}),
        'statement:2': ('dialogue', {'ref': 'statement:2', 'role': 'user',
                                     'text': '我还没写循环。'}),
        'run:1': ('runEvidence', {'text': '旧版本语法检查通过。', 'codeSnapshotRef': 'code:v3'}),
        'code:v3': ('code', {'snapshotRef': 'code:v3', 'language': 'python3', 'text': 'pass\n'}),
    }
    records = []
    for index, (ref, (kind, content)) in enumerate(contents.items(), 1):
        suffix = ref.rsplit(':', 1)[1].removeprefix('v')
        record = {'ref': ref, 'goalContext': GOAL, 'kind': kind,
                  'version': int(suffix) if suffix.isdigit() else 1,
                  'content': content, 'contentHash': hash_content(content)}
        if kind == 'dialogue':
            record.update(sequence=index, turnId='turn-1')
        records.append(record)
    request = {'schemaVersion': '1.0.0', 'goalContext': GOAL,
               'goalRef': 'goal:v3', 'problemRequirementRef': 'problem:v2',
               'codeSnapshotRef': 'code:v4', 'observationRefs': ['observation:7'],
               'historicalCodeRefs': ['code:v3'],
               'userStatementRefs': ['statement:2'], 'runRecordRefs': ['run:1'],
               'dialogueRefs': ['statement:2'],
               'currentVersions': {r['ref']: r['version'] for r in records},
               'budget': {'contextWindowTokens': 8000, 'reservedOutputTokens': 1000,
                          'safetyMarginTokens': 500}}
    return request, records


def candidate(stage='implementation_in_progress'):
    return {'stage': stage,
            'reasons': [] if stage == 'unknown' else [{'text': '用户正在改写循环，当前快照保留初始化。',
                         'evidenceRefs': ['code:v4', 'statement:2']}],
            'unknowns': [{'text': '循环与返回语句尚未提供。', 'evidenceRefs': ['statement:2']}]}


class Provider:
    public_config = {'provider': 'Fake', 'model': 'fake-stage'}
    def __init__(self, value=None, error=None):
        self.value = candidate() if value is None else value
        self.error = error
        self.calls = []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ProviderResponse(json.dumps(self.value, ensure_ascii=False), 'fake-stage')








def test_recognizes_stage_and_preserves_old_run_as_old_evidence():
    request, records = fixture(); provider = Provider()
    result = recognize_stage(request, MapContextResolver(records), provider)
    assert result['status'] == 'recognized'
    assert result['result']['stage'] == 'implementation_in_progress'
    assert result['contextSnapshot']['sourceVersions']['code:v4']['version'] == 4
    payload = json.loads(provider.calls[0]['user_prompt'])
    assert payload['materials']['codeLines'][-1] == {'line': 2, 'text': '    seen = {}'}
    assert payload['materials']['runRecords'][0]['isCurrentCode'] is False
    assert result['goalContext'] == GOAL
    assert 'goalState' not in json.dumps(result).lower()


@pytest.mark.parametrize('mutate,reason', [
    (lambda q, rs: q['currentVersions'].__setitem__('code:v4', 3), 'SOURCE_VERSION_MISMATCH'),
    (lambda q, rs: rs[2].update(contentHash='sha256:' + '0' * 64), 'SOURCE_HASH_MISMATCH'),
    (lambda q, rs: rs[3].update(goalContext={**GOAL, 'subgoalId': 'other'}), 'SOURCE_SCOPE_MISMATCH'),
])
def test_stale_or_mismatched_sources_fail_before_model(mutate, reason):
    request, records = fixture(); mutate(request, records); provider = Provider()
    result = recognize_stage(request, MapContextResolver(records), provider)
    assert result['status'] == 'failed'
    assert result['failure']['reason'] == reason
    assert not provider.calls


def test_unsupported_source_kind_fails_before_model():
    request, records = fixture(); records[3]['kind'] = 'hiddenTest'; provider = Provider()
    result = recognize_stage(request, MapContextResolver(records), provider)
    assert result['failure']['reason'] == 'SOURCE_KIND_MISMATCH'
    assert not provider.calls


@pytest.mark.parametrize('change,reason', [
    (lambda rs: rs[0]['content'].__setitem__('hiddenTests', ['secret']), 'FORBIDDEN_INPUT'),
    (lambda rs: rs[3]['content'].__setitem__('codeSnapshotRef', 'code:missing'), 'CODE_SNAPSHOT_REFERENCE_MISSING'),
])
def test_sensitive_material_or_unresolved_observation_code_never_reaches_model(change, reason):
    request, records = fixture(); change(records)
    changed = next(record for record in records if record['contentHash'] != hash_content(record['content']))
    changed['contentHash'] = hash_content(changed['content'])
    provider = Provider(); result = recognize_stage(request, MapContextResolver(records), provider)
    assert result['failure']['reason'] == reason
    assert not provider.calls


@pytest.mark.parametrize('value', [
    {'stage': 'finished', 'reasons': [], 'unknowns': [], 'localTestableScopes': []},
    {'stage': 'test_ready', 'reasons': [{'text': '语法合法', 'evidenceRefs': ['missing']}],
     'unknowns': [], 'localTestableScopes': []},
    {'stage': 'draft', 'reasons': [], 'unknowns': [], 'localTestableScopes': [
        {'kind': 'file', 'codeSnapshotRef': 'code:v4', 'selector': {'startLine': 1, 'endLine': 1},
         'purpose': 'x', 'conditions': ['x'], 'evidenceRefs': ['code:v4']} ]},
])
def test_invalid_model_output_is_not_reported_as_unknown(value):
    request, records = fixture(); result = recognize_stage(request, MapContextResolver(records), Provider(value))
    assert result['status'] == 'failed'
    assert result['failure']['reason'] == 'PROVIDER_OUTPUT_INVALID'


def test_service_failure_is_distinct_from_valid_unknown_stage():
    request, records = fixture()
    failed = recognize_stage(request, MapContextResolver(records), Provider(error=ProviderError('PROVIDER_UNAVAILABLE')))
    assert failed['status'] == 'failed' and failed['failure']['reason'] == 'PROVIDER_UNAVAILABLE'
    unknown = recognize_stage(request, MapContextResolver(records), Provider(candidate('unknown')))
    assert unknown['status'] == 'recognized' and unknown['result']['stage'] == 'unknown'


def test_context_budget_failure_does_not_call_stage_model():
    request, records = fixture(); request['budget']['contextWindowTokens'] = 100; provider = Provider()
    result = recognize_stage(request, MapContextResolver(records), provider)
    assert result['status'] == 'failed'
    assert result['failure']['reason'] == 'REQUIRED_MATERIAL_TOO_LONG'
    assert not provider.calls


def test_reserved_output_must_cover_configured_provider_maximum():
    request, records = fixture(); provider = Provider()
    provider.public_config = {'model': 'fake-stage', 'maxTokens': 4096}
    result = recognize_stage(request, MapContextResolver(records), provider)
    assert result['failure']['reason'] == 'OUTPUT_BUDGET_TOO_SMALL'
    assert not provider.calls


def test_valid_json_does_not_prove_candidate_reason_is_semantically_correct():
    request, records = fixture()
    code = next(record for record in records if record['ref'] == 'code:v4')
    code['content']['text'] = 'def solve(nums, target):\n    return []\n'
    code['contentHash'] = hash_content(code['content'])
    value = candidate('test_ready')
    value['reasons'] = [{'text': '当前函数结构完整，可对局部输入观察返回值；尚未验证题目正确性。',
                         'evidenceRefs': ['code:v4']}]
    value['unknowns'] = []
    result = recognize_stage(request, MapContextResolver(records), Provider(value))
    assert result['status'] == 'recognized'
    assert result['result']['stage'] == 'test_ready'
    assert not any(key in result['result'] for key in ('correct', 'passed', 'completed'))


def test_incomplete_syntax_can_still_be_recognized():
    request, records = fixture()
    code = next(record for record in records if record['ref'] == 'code:v4')
    code['content']['text'] = 'for x in xs:\n'
    code['contentHash'] = hash_content(code['content'])
    value = candidate('implementation_in_progress')
    result = recognize_stage(request, MapContextResolver(records), Provider(value))
    assert result['status'] == 'recognized'


def test_observation_cannot_point_to_goal_instead_of_code():
    request, records = fixture(); provider = Provider()
    observation = next(r for r in records if r['kind'] == 'observation')
    observation['content']['codeSnapshotRef'] = request['goalRef']
    observation['contentHash'] = hash_content(observation['content'])
    report = recognize_stage(request, MapContextResolver(records), provider)
    assert report['status'] == 'failed' and not provider.calls




def test_old_observation_requires_pinned_real_code_and_remains_old():
    request, records = fixture(); provider = Provider()
    observation = next(r for r in records if r['kind'] == 'observation')
    observation['content']['codeSnapshotRef'] = 'code:v3'
    observation['contentHash'] = hash_content(observation['content'])
    report = recognize_stage(request, MapContextResolver(records), provider)
    assert report['status'] == 'recognized'
    payload = json.loads(provider.calls[0]['user_prompt'])
    assert payload['materials']['observations'][0]['isCurrentCode'] is False
    request['historicalCodeRefs'] = []
    request['currentVersions'].pop('code:v3')
    provider = Provider()
    report = recognize_stage(request, MapContextResolver(records), provider)
    assert report['status'] == 'failed' and not provider.calls

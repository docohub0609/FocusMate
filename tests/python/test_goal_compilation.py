import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from copy import deepcopy
import json
import pytest
from apps.api.problem_features import build_human_feature_set
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository
from tests.python.test_problem_features import INTAKE, base_features

STAGES = ['problemUnderstanding', 'feasibleStrategy', 'coreStateAndCorrectness', 'coreImplementation', 'boundaryAndTesting']

@pytest.fixture
def inputs(tmp_path):
    repo = ProblemRepository(tmp_path / 'goal.db')
    record = repo.create(create_problem_package(INTAKE, id_factory=lambda: 'problem-goal'))
    features = build_human_feature_set(record['problemPackage'], {'expectedDraftRevision': 1, 'features': base_features()})
    record = repo.confirm_and_freeze('problem-goal', 1, features, 1, None)
    goal = {'goalId': 'goal-real', 'schemaVersion': '1.0.0', 'createdAt': '2026-09-12T01:00:00Z', 'problemRef': features['problemRef'], 'status': 'active'}
    return goal, record, features

def wire():
    return {'subgoals': [{'key': str(i), 'stage': stage, 'objective': '说明区间端点的题目要求', 'userGoal': '说明区间端点需要满足的条件', 'acceptableEvidence': ['evaluatedUserExplanation'] if i < 3 else ['runResult'] if i == 3 else ['targetedRunResult'], 'sourceRefs': ['statement:0'], 'dependsOn': [], 'rubric': [{'check': '说明端点含义', 'sourceRefs': ['outputSpec:0']}]} for i, stage in enumerate(STAGES)], 'strategyCandidates': [], 'unknowns': ['当前尚无经过验证的算法候选']}

class Provider:
    def __init__(self, value=None):
        self.value = wire() if value is None else value
        self.calls = []
    def complete(self, **kwargs):
        from focusmate.contracts.goal_compilation import GoalCompilationResponse
        self.calls.append(kwargs)
        return GoalCompilationResponse(self.value if isinstance(self.value, str) else json.dumps(self.value), 'fake', 'response-1')

def compile(inputs, provider=None):
    from focusmate.core.goal_compilation import compile_goal_contract
    return compile_goal_contract(*inputs, provider or Provider(), created_at='2026-09-12T02:00:00Z')

def test_compiles_without_mutation_and_preserves_unknowns(inputs):
    original = deepcopy(inputs)
    result = compile(inputs)
    assert inputs == original
    assert result['contract']['goalId'] == 'goal-real'
    assert result['contract']['contractStatus'] == 'draft'
    assert result['generation']['semanticStatus'] == 'unverified'
    assert result['generation']['inputUncertainties'][0]['status'] == 'unknown'
    assert result['rubrics'][0]['sources'][0]['sourceText'] == INTAKE['statement']
    assert len(result['publicGoals']) == 5

@pytest.mark.parametrize('mutation', ['goalRef', 'hash', 'frozen', 'featureId', 'provenance'])
def test_invalid_inputs_rejected_before_provider(inputs, mutation):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    goal, record, features = inputs
    if mutation == 'goalRef': goal['problemRef'] = {'problemId': 'other', 'problemVersion': 1}
    elif mutation == 'hash': record['problemPackage']['constraints'] = 'changed'
    elif mutation == 'frozen': record['lifecycle']['status'] = 'draft'
    elif mutation == 'featureId': record['lifecycle']['confirmedFeatureSetId'] = 'other'
    else: features.pop('provenance')
    provider = Provider()
    with pytest.raises(GoalCompilationError): compile(inputs, provider)
    assert not provider.calls

@pytest.mark.parametrize('mutation', ['extra', 'missingStage', 'reference', 'dependency', 'cycle', 'emptyUnknown', 'algorithm', 'code', 'duplicateKey'])
def test_invalid_model_content_rejected(inputs, mutation):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    if mutation == 'extra': value['coachPolicy'] = {'completeSolutionAllowed': True}
    elif mutation == 'missingStage': value['subgoals'][0]['stage'] = STAGES[1]
    elif mutation == 'reference': value['subgoals'][0]['sourceRefs'] = ['private:0']
    elif mutation == 'dependency': value['subgoals'][0]['dependsOn'] = [{'key': 'absent', 'reason': '需要', 'sourceRefs': ['statement:0']}]
    elif mutation == 'cycle': value['subgoals'][0]['dependsOn'] = [{'key': '0', 'reason': '需要', 'sourceRefs': ['statement:0']}]
    elif mutation == 'emptyUnknown': value['unknowns'] = []
    elif mutation == 'algorithm': value['subgoals'][0]['objective'] = '使用双指针完成区间查找'
    elif mutation == 'code': value['subgoals'][0]['userGoal'] = '```python\nprint(1)\n```'
    else: value['subgoals'][1]['key'] = '0'
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(value))

@pytest.mark.parametrize('content', ['{"subgoals": [], "subgoals": []}', '{"unknowns": [NaN]}', '{"unknowns": [Infinity]}'])
def test_strict_json(inputs, content):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(content))

def test_strategy_change_does_not_change_contract(inputs):
    value = wire()
    first = compile(inputs)
    value['strategyCandidates'] = [{'label': '扫描', 'approach': '逐项检查', 'applicability': '规模允许时', 'uncertainties': ['待分析复杂度'], 'subgoalKeys': ['1'], 'sourceRefs': ['constraints:0']}]
    second = compile(inputs, Provider(value))
    assert first['contract'] == second['contract']
    assert second['strategyCandidates'][0]['status'] == 'hypothesis'


def test_escaped_internal_code_fence_is_rejected_after_json_decoding(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['subgoals'][0]['rubric'][0]['check'] = '```python\nprint(1)\n```'
    escaped = json.dumps(value, ensure_ascii=True).replace('`', '\\u0060')
    with pytest.raises(GoalCompilationError):
        compile(inputs, Provider(escaped))


@pytest.mark.parametrize('text', ['能够确保程序在所有边界情况下输出正确结果。', '我需要理解算法正确性。', '通过样例确认程序基本正确性。'])
def test_public_goals_reject_abstract_self_reports_and_overclaiming(inputs, text):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['subgoals'][0]['userGoal'] = text
    with pytest.raises(GoalCompilationError):
        compile(inputs, Provider(value))


@pytest.mark.parametrize('text', ['实现核心搜索过程并输出结果。', '说明如何更新当前合并区间。', '维护访问标记并判断是否到达终点。'])
def test_contract_rejects_unselected_implementation_details(inputs, text):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['subgoals'][0]['objective'] = text
    with pytest.raises(GoalCompilationError):
        compile(inputs, Provider(value))


def test_strategy_is_a_short_direction_not_a_complete_branching_procedure(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['strategyCandidates'] = [{'label': '合并方向', 'approach': '若新区间相交则更新右端点为两者较大值，否则输出当前区间并开始新的一组。',
                                   'applicability': '区间题', 'uncertainties': [], 'subgoalKeys': ['1'], 'sourceRefs': ['statement:0']}]
    with pytest.raises(GoalCompilationError):
        compile(inputs, Provider(value))


@pytest.mark.parametrize('text', ['说明排序扫描策略的时间复杂度。', '实现按左端点排序并合并区间的逻辑。'])
def test_ordering_output_does_not_authorize_prescribing_sorting_algorithm(inputs, text):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['subgoals'][0]['userGoal'] = text
    with pytest.raises(GoalCompilationError):
        compile(inputs, Provider(value))



def test_dependency_reason_and_sources_retained(inputs):
    value = wire()
    value['subgoals'][1]['dependsOn'] = [{'key': '0', 'reason': '需要先明确待判断的区间条件', 'sourceRefs': ['statement:0']}]
    result = compile(inputs, Provider(value))
    assert result['contract']['subgoals'][1]['dependsOn'] == [result['contract']['subgoals'][0]['subgoalId']]
    assert result['rubrics'][1]['dependencies'][0]['reason'] == '需要先明确待判断的区间条件'
    assert result['rubrics'][1]['dependencies'][0]['sources'][0]['sourceText'] == INTAKE['statement']

def test_two_node_cycle_rejected(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    for source, target in [(0, '1'), (1, '0')]:
        value['subgoals'][source]['dependsOn'] = [{'key': target, 'reason': '依赖', 'sourceRefs': ['statement:0']}]
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(value))

def test_raw_provider_failure_is_sanitized(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    class Broken:
        def complete(self, **kwargs): raise RuntimeError('secret-original-model-content')
    with pytest.raises(GoalCompilationError) as error: compile(inputs, Broken())
    assert 'secret' not in str(error.value)
    assert error.value.retryable

def test_adapter_safe_error_preserved(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    failure = GoalCompilationError('PROVIDER_TIMEOUT', '连接超时。', True)
    class Broken:
        def complete(self, **kwargs): raise failure
    with pytest.raises(GoalCompilationError) as error: compile(inputs, Broken())
    assert error.value is failure

@pytest.mark.parametrize('leak', ['x = nums[0]', 'print(answer)', '使用堆排序计算结果', '采用线段树查询区间'])
def test_additional_visible_solution_leaks_rejected(inputs, leak):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['subgoals'][0]['userGoal'] = leak
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(value))

@pytest.mark.parametrize('content', [' ' * 10, 'x' * (128 * 1024 + 1)], ids=['blank', 'oversized'])
def test_empty_or_oversized_response_rejected(inputs, content):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(content))

def test_code_fence_in_internal_rubric_rejected(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['subgoals'][0]['rubric'][0]['check'] = '```python\nprint(answer)\n```'
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(value))


def test_oversized_otherwise_valid_json_rejected(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    value['unknowns'] = ['界' * 45000]
    with pytest.raises(GoalCompilationError): compile(inputs, Provider(json.dumps(value, ensure_ascii=False)))

def test_hypothesis_preserved_without_upgrade(inputs):
    feature = inputs[2]['features'][0]
    feature['status'] = 'hypothesis'
    feature['confidence'] = 0.5
    provider = Provider()
    result = compile(inputs, provider)
    saved = result['generation']['inputUncertainties'][0]
    assert saved == feature
    payload = json.loads(provider.calls[0]['user_prompt'])
    assert feature not in payload['confirmedFacts']
    assert feature in payload['inputUncertainties']

def test_frozen_conflict_rejected_before_provider(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    feature = inputs[2]['features'][0]
    feature['status'] = 'conflict'
    feature.pop('confidence')
    value = feature.pop('value')
    feature['candidates'] = [{'value': value, 'evidence': feature['evidence']}, {'value': {'maximum': 10}, 'evidence': feature['evidence']}]
    provider = Provider()
    with pytest.raises(GoalCompilationError): compile(inputs, provider)
    assert not provider.calls


def test_unknown_input_evidence_id_is_not_accepted_for_matching_text(inputs):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    inputs[2]['features'][0]['evidence'][0]['evidenceId'] = 'missing:0'
    provider = Provider()
    with pytest.raises(GoalCompilationError):
        compile(inputs, provider)
    assert not provider.calls

@pytest.mark.parametrize('field', ['objective', 'userGoal', 'rubric'])
def test_invented_complexity_requirement_rejected(inputs, field):
    from focusmate.contracts.goal_compilation import GoalCompilationError
    value = wire()
    text = '说明时间复杂度不超过O(n log n)的方案。'
    if field == 'rubric':
        value['subgoals'][1]['rubric'][0]['check'] = text
    else:
        value['subgoals'][1][field] = text
    with pytest.raises(GoalCompilationError):
        compile(inputs, Provider(value))

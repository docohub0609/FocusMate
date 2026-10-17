"""T042 boundary tests; fake model output tests protocol, not semantic accuracy."""
from copy import deepcopy
import json
import pytest

from tests.python.test_goal_compilation import inputs, compile as compile_draft

NOW = '2026-09-12T03:00:00Z'


@pytest.fixture
def draft(inputs):
    return compile_draft(inputs)


class Reviewer:
    def __init__(self, mutate=None, raw=None):
        self.calls = []
        self.mutate = mutate
        self.raw = raw

    def complete(self, **kwargs):
        from focusmate.contracts.goal_compilation import GoalCompilationResponse
        self.calls.append(kwargs)
        payload = json.loads(kwargs['user_prompt'])
        value = {'items': [{'itemId': item['itemId'], 'verdict': 'supported',
                            'issueTypes': [], 'reason': '该项与题面及模板规则一致。',
                            'sourceRefs': [payload['sourceCatalog'][0]['evidenceId']],
                            'templateRefs': [], 'blocking': False}
                           for item in payload['scanItems']]}
        if self.mutate:
            self.mutate(value, payload)
        return GoalCompilationResponse(self.raw if self.raw is not None else json.dumps(value), 'fake', 'review-1')


def review(inputs, draft, provider=None):
    from focusmate.core.goal_contract_review import review_goal_contract
    return review_goal_contract(*inputs, draft, provider or Reviewer(), created_at=NOW)


def test_complete_review_passes_without_mutating_generation_or_progress(inputs, draft):
    before = deepcopy((inputs, draft))
    result = review(inputs, draft)
    assert result['status'] == 'passed'
    assert result['goalId'] == inputs[0]['goalId']
    assert result['contractVersion'] == 1
    assert result['items'] and result['issues'] == []
    assert result['model'] == 'fake'
    assert (inputs, draft) == before
    assert draft['generation']['semanticStatus'] == 'unverified'


def test_hash_binds_edited_requirements_but_not_assets_strategies_or_lifecycle(inputs, draft):
    from focusmate.core.goal_contract_review import validate_review_input
    first = validate_review_input(*inputs, draft)['contentHash']
    inputs[1]['problemPackage']['verifierAssetStatus'] = 'ready'
    inputs[1]['lifecycle']['updatedAt'] = NOW
    draft['contract']['contractStatus'] = 'frozen'
    draft['contract']['createdAt'] = NOW
    assert validate_review_input(*inputs, draft)['contentHash'] == first
    draft['generation']['model'] = 'different-generation'
    assert validate_review_input(*inputs, draft)['contentHash'] == first
    draft['rubrics'][0]['checks'][0]['check'] += '，并说明两个值不同。'
    assert validate_review_input(*inputs, draft)['contentHash'] != first


@pytest.mark.parametrize('mutation', [
    lambda d: d['publicGoals'][1].update(subgoalId=d['publicGoals'][0]['subgoalId']),
    lambda d: d['publicGoals'][0].update(stage='boundaryAndTesting'),
    lambda d: d['rubrics'][0].update(goalId='other-goal'),
    lambda d: d['sourceCatalog'][0].update(sourceText='伪造权威来源'),
    lambda d: d['rubrics'][0]['sources'][0].update(sourceText='伪造引用原文'),
    lambda d: d['rubrics'][0]['checks'][0].update(sourceRefs=['missing:0']),
    lambda d: d['contract']['subgoals'][0].update(dependsOn=[d['contract']['subgoals'][1]['subgoalId']]),
    lambda d: d['contract']['subgoals'][0].update(acceptableEvidence=['compileResult']),
    lambda d: d['contract'].update(objective='改变固定总目标'),
    lambda d: d['generation'].update(featureSetId='other-feature'),
    lambda d: d['generation'].update(inputUncertainties=[]),
])
def test_manual_structural_corruption_is_rejected_before_provider(inputs, draft, mutation):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    mutation(draft)
    provider = Reviewer()
    with pytest.raises(GoalContractReviewError) as caught:
        review(inputs, draft, provider)
    assert caught.value.code == 'INVALID_GOAL_CONTRACT_REVIEW_INPUT'
    assert caught.value.issues
    assert not provider.calls
    assert '伪造' not in str(caught.value)


def test_dependency_cycle_rejected_even_when_rubric_agrees(inputs, draft):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    for source, target in [(0, 1), (1, 0)]:
        target_id = draft['contract']['subgoals'][target]['subgoalId']
        draft['contract']['subgoals'][source]['dependsOn'] = [target_id]
        draft['rubrics'][source]['dependencies'] = [{
            'subgoalId': target_id, 'reason': '先完成另一节点',
            'sourceRefs': draft['rubrics'][source]['sourceRefs'],
            'sources': draft['rubrics'][source]['sources'],
        }]
    with pytest.raises(GoalContractReviewError):
        review(inputs, draft)


def test_unknown_or_omitted_scans_cannot_silently_pass(inputs, draft):
    for mutate in [lambda v, p: v['items'].pop(),
                   lambda v, p: v['items'].append(deepcopy(v['items'][0])),
                   lambda v, p: v['items'][0].update(itemId='invented'),
                   lambda v, p: v['items'][0].update(sourceRefs=['invented']),
                   lambda v, p: v['items'][0].update(templateRefs=['invented'])]:
        result = review(inputs, draft, Reviewer(mutate))
        assert result['status'] == 'unavailable'
        assert result['items'] == []


def test_public_issues_are_canned_and_internal_reason_is_retained(inputs, draft):
    def mutate(value, payload):
        value['items'][0].update(verdict='contradicted', issueTypes=['conditionMismatch'],
                                 reason='private-model-reason', blocking=True)
    result = review(inputs, draft, Reviewer(mutate))
    assert result['status'] == 'needsReview'
    assert result['items'][0]['reason'] == 'private-model-reason'
    assert 'private-model-reason' not in json.dumps(result['issues'])
    assert result['issues'][0]['type'] == 'conditionMismatch'


def test_negative_verdict_cannot_be_made_nonblocking_by_model(inputs, draft):
    def mutate(value, payload):
        value['items'][0].update(verdict='insufficient', issueTypes=['insufficientEvidence'], blocking=False)
    result = review(inputs, draft, Reviewer(mutate))
    assert result['status'] == 'needsReview'
    assert result['items'][0]['blocking'] is True


def test_source_reverse_scan_missing_requirement_blocks_without_node_error(inputs, draft):
    def mutate(value, payload):
        source = next(item for item in value['items'] if item['itemId'].startswith('source:'))
        source.update(verdict='insufficient', issueTypes=['missingRequirement'], blocking=True)
    result = review(inputs, draft, Reviewer(mutate))
    assert result['status'] == 'needsReview'
    assert result['issues'][0]['itemId'].startswith('source:')


def test_missing_unknown_scan_is_unavailable_even_when_every_node_is_supported(inputs, draft):
    def mutate(value, payload):
        value['items'] = [item for item in value['items'] if not item['itemId'].startswith('unknown:')]
    assert review(inputs, draft, Reviewer(mutate))['status'] == 'unavailable'


def test_critical_unknown_blocks_but_nonessential_unknown_can_pass(inputs, draft):
    def mutate(value, payload):
        unknown = next(item for item in value['items'] if item['itemId'].startswith('unknown:'))
        unknown.update(verdict='insufficient', issueTypes=['criticalUnknown'],
                       blocking=True, reason='缺少决定结果的条件')
    assert review(inputs, draft)['status'] == 'passed'
    assert review(inputs, draft, Reviewer(mutate))['status'] == 'needsReview'


def test_supported_but_critical_unknown_still_blocks(inputs, draft):
    def mutate(value, payload):
        unknown = next(item for item in value['items'] if item['itemId'].startswith('unknown:'))
        unknown.update(blocking=True, reason='未知描述真实，但该条件仍决定验收结果。')
    result = review(inputs, draft, Reviewer(mutate))
    assert result['status'] == 'needsReview'
    assert result['issues'][0]['type'] == 'criticalUnknown'


def test_unknown_description_is_reviewed_and_bound_to_hash(inputs, draft):
    from focusmate.core.goal_contract_review import validate_review_input
    first = validate_review_input(*inputs, draft)
    actual = next(feature for feature in inputs[2]['features'] if feature['status'] == 'unknown')
    original_description = actual['reason']
    replacement = '无法确定两个区间端点相接时是否需要合并。'
    actual['reason'] = replacement
    historical = next(feature for feature in draft['generation']['inputUncertainties']
                      if feature['featureKey'] == actual['featureKey'])
    historical['reason'] = replacement
    second = validate_review_input(*inputs, draft)
    assert first['contentHash'] != second['contentHash']
    assert first['inputUncertainties'][0]['unknownDescription'] == original_description
    assert second['inputUncertainties'][0]['unknownDescription'] == replacement
    provider = Reviewer()
    review(inputs, draft, provider)
    assert replacement in provider.calls[0]['user_prompt']
    assert 'unknownDescription' in provider.calls[0]['user_prompt']


def test_unknown_description_change_cannot_bypass_confirmed_input_consistency(inputs, draft):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    actual = next(feature for feature in inputs[2]['features'] if feature['status'] == 'unknown')
    actual['reason'] = '另一个会影响验收条件的未知。'
    with pytest.raises(GoalContractReviewError) as caught:
        review(inputs, draft)
    assert caught.value.issues[0]['type'] == 'uncertaintyMismatch'


def test_supported_with_contradictory_issue_type_never_passes(inputs, draft):
    def mutate(value, payload):
        value['items'][0]['issueTypes'] = ['missingRequirement']
    assert review(inputs, draft, Reviewer(mutate))['status'] == 'unavailable'


@pytest.mark.parametrize('mutate', [
    lambda i: i[0].update(problemRef={'problemId': 'another', 'problemVersion': 1}),
    lambda i: i[1]['lifecycle'].update(confirmedFeatureSetId='another'),
    lambda i: i[1]['problemPackage'].update(constraints='新范围'),
    lambda i: i[2].update(problemContentHash='0' * 64),
    lambda i: i[2]['features'][0]['evidence'][0].update(sourceText='错误证据'),
])
def test_actual_frozen_context_mismatch_is_rejected(inputs, draft, mutate):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    mutate(inputs)
    with pytest.raises(GoalContractReviewError):
        review(inputs, draft)


def test_valid_semantic_mutation_reaches_independent_model(inputs, draft):
    draft['publicGoals'][1]['text'] = '必须使用BFS完成图上的搜索。'
    def mutate(value, payload):
        value['items'][1].update(verdict='contradicted', issueTypes=['prescribedStrategy'],
                                 blocking=True, reason='题面没有指定算法')
    provider = Reviewer(mutate)
    assert review(inputs, draft, provider)['status'] == 'needsReview'
    assert len(provider.calls) == 1
    assert 'BFS' in provider.calls[0]['user_prompt']


def test_prompt_omits_generation_confidence_reasons_and_strategy_history(inputs, draft):
    inputs[2]['features'][0]['reason'] = 'secret-generation-reason'
    draft['strategyCandidates'] = [{
        'goalId': inputs[0]['goalId'], 'status': 'hypothesis', 'label': 'secret-strategy-label',
        'approach': '候选方向', 'applicability': '适用前提', 'uncertainties': [],
        'subgoalIds': [draft['contract']['subgoals'][0]['subgoalId']],
        'sourceRefs': draft['rubrics'][0]['sourceRefs'], 'sources': draft['rubrics'][0]['sources'],
    }]
    provider = Reviewer()
    assert review(inputs, draft, provider)['status'] == 'passed'
    prompt = provider.calls[0]['user_prompt']
    assert 'secret-generation-reason' not in prompt
    assert 'secret-strategy-label' not in prompt
    assert 'confidence' not in prompt
    payload = json.loads(prompt)
    assert len([x for x in payload['scanItems'] if x['kind'] == 'source']) == len(draft['sourceCatalog'])
    assert len([x for x in payload['scanItems'] if x['kind'] == 'node']) == len(draft['contract']['subgoals'])
    from focusmate.core.goal_contract_review import validate_review_input
    first = validate_review_input(*inputs, draft)['contentHash']
    draft['strategyCandidates'][0]['label'] = 'another-unverified-strategy'
    draft['strategyCandidates'][0]['approach'] = '另一个待验证方向'
    assert validate_review_input(*inputs, draft)['contentHash'] == first


@pytest.mark.parametrize('raw', ['{"items":[],"items":[]}', '{"items":NaN}', '```json\n{}\n```',
                                  '[]', ' ', 'x' * (128 * 1024 + 1), '{"items":[],"status":"passed"}'],
                         ids=['duplicates', 'nan', 'fenced', 'array', 'blank', 'oversized', 'extra-status'])
def test_malformed_responses_are_unavailable_with_no_repair(inputs, draft, raw):
    provider = Reviewer(raw=raw)
    result = review(inputs, draft, provider)
    assert result['status'] == 'unavailable'
    assert len(provider.calls) == 1
    assert result['items'] == []


def test_provider_exception_is_safe_unavailable(inputs, draft):
    class Broken:
        def complete(self, **kwargs):
            raise RuntimeError('private-secret-provider-data')
    result = review(inputs, draft, Broken())
    assert result['status'] == 'unavailable'
    assert 'private-secret' not in json.dumps(result)


@pytest.mark.parametrize('field,value', [('model', ''), ('model', 17), ('response_id', None), ('content', [])])
def test_bad_provider_envelope_never_crashes_or_passes(inputs, draft, field, value):
    from focusmate.contracts.goal_compilation import GoalCompilationResponse
    class BadEnvelope:
        def complete(self, **kwargs):
            fields = {'content': '{}', 'model': 'fake', 'response_id': 'id'}
            fields[field] = value
            return GoalCompilationResponse(**fields)
    assert review(inputs, draft, BadEnvelope())['status'] == 'unavailable'


def test_untrusted_provider_error_code_and_message_are_not_exposed(inputs, draft):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    class Broken:
        def complete(self, **kwargs):
            raise GoalContractReviewError('secret-code', 'secret-message', retryable=True)
    result = review(inputs, draft, Broken())
    assert result['status'] == 'unavailable'
    assert result['error']['code'] == 'GOAL_CONTRACT_REVIEW_PROVIDER_FAILED'
    assert 'secret-' not in json.dumps(result)


def test_complete_code_cannot_be_retained_as_model_review_reason(inputs, draft):
    def mutate(value, payload):
        value['items'][0]['reason'] = '```python\nprint(answer)\n```'
    assert review(inputs, draft, Reviewer(mutate))['status'] == 'unavailable'


def test_input_code_fence_is_rejected_before_review(inputs, draft):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    draft['rubrics'][0]['checks'][0]['check'] = '```python\nprint(answer)\n```'
    with pytest.raises(GoalContractReviewError):
        review(inputs, draft)


def test_invalid_review_time_is_safe_input_error(inputs, draft):
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    from focusmate.core.goal_contract_review import review_goal_contract
    with pytest.raises(GoalContractReviewError) as caught:
        review_goal_contract(*inputs, draft, Reviewer(), created_at='yesterday')
    assert caught.value.issues[0]['type'] == 'invalidTimestamp'


def test_template_rule_alone_is_legitimate_review_basis(inputs, draft):
    def mutate(value, payload):
        for item in value['items']:
            item.update(sourceRefs=[], templateRefs=[payload['templateRules'][0]['ruleId']])
    assert review(inputs, draft, Reviewer(mutate))['status'] == 'passed'


def test_adapter_uses_bounded_transport_and_safe_errors():
    import time
    from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
    from focusmate.contracts.goal_contract_review import GoalContractReviewError
    from apps.api.deepseek_provider import ProviderFailure
    class FailedTransport:
        def complete(self, **kwargs):
            assert 0 < kwargs['deadline'] - time.monotonic() <= 120
            raise ProviderFailure('LLM_UNAVAILABLE', 'secret-network-details', True)
    with pytest.raises(GoalContractReviewError) as caught:
        DeepSeekGoalContractReviewProvider(FailedTransport()).complete(system_prompt='s', user_prompt='u')
    assert caught.value.retryable
    assert 'secret-network-details' not in str(caught.value)

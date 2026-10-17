"""Wire review coverage is mechanical; these fixtures do not test model judgment."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from focusmate.core.dynamic_coach_roles import DYNAMIC_REVIEW_SCHEMA
from focusmate.core.coach_goal_review_wire import (
    goal_requirement_catalog, goal_review_wire_schema, validate_goal_review_wire,
)


def prepared():
    def goal(key, checks, version=1):
        return {'goalId': 'g1', 'subgoalId': key, 'status': 'active',
                'versions': [{'version': version, 'checks': checks}]}
    state = {'goalContext': {'goalId': 'g1'},
             'workGoals': {'w1': goal('w1', ['历史条件'])}}
    round_state = {'prepared': {
        'candidate': {'action': {'subgoalIds': ['w2', 'w1']}, 'checkTargets': ['w1']},
        'goals': {'w1': goal('w1', ['余数不为零时多分一组', '整除时不多分组'], 2),
                  'w2': goal('w2', ['输出组数是整数']),
                  'unrelated': goal('unrelated', ['不应进入当前审核'])}}}
    return state, round_state


def reviewed(catalog, *, status='passed', verdict='pass'):
    return {'actionReview': {'checks': {}}, 'goalReview': {
        'status': status, 'reason': '模型逐项判断后给出的结论', 'evidenceRefs': ['code:2'],
        'requirementReviews': [
            {'requirementId': item['requirementId'], 'verdict': verdict,
             'reason': '这是代码应满足的局部性质'} for item in catalog]}}


def test_catalog_uses_prepared_versions_and_only_associated_goals_without_mutation():
    state, round_state = prepared()
    before = deepcopy((state, round_state))
    catalog = goal_requirement_catalog(state, round_state)
    assert [x['subgoalId'] for x in catalog] == ['w2', 'w1', 'w1']
    assert [x['workGoalVersion'] for x in catalog] == [1, 2, 2]
    assert [x['requirement'] for x in catalog] == [
        '输出组数是整数', '余数不为零时多分一组', '整除时不多分组']
    assert (state, round_state) == before
    assert goal_requirement_catalog(state, round_state) == catalog
    catalog[0]['requirement'] = '不能污染原目标'
    assert goal_requirement_catalog(state, round_state)[0]['requirement'] == '输出组数是整数'


def test_requirement_ids_pin_version_text_and_duplicate_ordinals():
    state, round_state = prepared()
    round_state['prepared']['goals']['w1']['versions'][-1]['checks'] = ['相同原文', '相同原文']
    original = goal_requirement_catalog(state, round_state)
    assert len({x['requirementId'] for x in original}) == 3
    changed = deepcopy(round_state)
    changed['prepared']['goals']['w1']['versions'][-1]['version'] += 1
    assert goal_requirement_catalog(state, changed)[1]['requirementId'] != original[1]['requirementId']
    changed = deepcopy(round_state)
    changed['prepared']['goals']['w1']['versions'][-1]['checks'][0] = '改变成功条件'
    assert goal_requirement_catalog(state, changed)[1]['requirementId'] != original[1]['requirementId']


@pytest.mark.parametrize('bad_target', ['missing', 'cross_goal', 'retired'])
def test_catalog_rejects_invalid_association(bad_target):
    state, round_state = prepared()
    if bad_target == 'missing':
        round_state['prepared']['candidate']['action']['subgoalIds'].append('missing')
    elif bad_target == 'cross_goal':
        round_state['prepared']['goals']['w1']['goalId'] = 'other-goal'
    else:
        round_state['prepared']['goals']['w1']['status'] = 'withdrawn'
    with pytest.raises(ValueError, match='GOAL_REVIEW_TARGET_INVALID'):
        goal_requirement_catalog(state, round_state)


def test_changed_goal_cannot_escape_coverage_by_omitting_action_association():
    state, round_state = prepared()
    prepared_plan = round_state['prepared']
    prepared_plan['candidate']['action']['subgoalIds'] = []
    prepared_plan['candidate']['checkTargets'] = []
    prepared_plan['candidate']['goalChanges'] = [
        {'kind': 'create', 'key': 'new:grouping'},
        {'kind': 'revise', 'key': 'w1'},
        {'kind': 'withdraw', 'key': 'unrelated'},
    ]
    prepared_plan['aliases'] = {'new:grouping': 'w2'}
    prepared_plan['goals']['unrelated']['status'] = 'withdrawn'
    assert [x['subgoalId'] for x in goal_requirement_catalog(state, round_state)] == [
        'w2', 'w1', 'w1']


def test_schema_extension_is_required_bounded_and_does_not_mutate_legacy_schema():
    catalog = goal_requirement_catalog(*prepared())
    before = deepcopy(DYNAMIC_REVIEW_SCHEMA)
    schema = goal_review_wire_schema(DYNAMIC_REVIEW_SCHEMA, catalog)
    goal_schema = schema['properties']['goalReview']
    assert 'requirementReviews' in goal_schema['required']
    item_schema = goal_schema['properties']['requirementReviews']
    assert item_schema['minItems'] == item_schema['maxItems'] == len(catalog)
    assert item_schema['items']['properties']['requirementId']['enum'] == [
        x['requirementId'] for x in catalog]
    Draft202012Validator(goal_schema).validate(reviewed(catalog)['goalReview'])
    with pytest.raises(ValidationError):
        Draft202012Validator(goal_schema).validate(reviewed(catalog[:-1])['goalReview'])
    assert DYNAMIC_REVIEW_SCHEMA == before
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize('failure, code', [
    ('absent', 'GOAL_REVIEW_COVERAGE_REQUIRED'),
    ('omitted', 'INCOMPLETE_GOAL_REVIEW_COVERAGE'),
    ('duplicate', 'DUPLICATE_GOAL_REVIEW_REQUIREMENT'),
    ('invented', 'UNKNOWN_GOAL_REVIEW_REQUIREMENT'),
])
def test_wire_rejects_missing_duplicate_and_fabricated_reviews(failure, code):
    catalog = goal_requirement_catalog(*prepared())
    reply = reviewed(catalog)
    checks = reply['goalReview']['requirementReviews']
    if failure == 'absent': del reply['goalReview']['requirementReviews']
    elif failure == 'omitted': checks.pop()
    elif failure == 'duplicate': checks[-1] = {**checks[0], 'reason': '不同理由不能掩盖重复编号'}
    else: checks[-1]['requirementId'] = 'requirement-invented'
    with pytest.raises(ValueError, match=code):
        validate_goal_review_wire(reply, catalog)


@pytest.mark.parametrize('verdict', ['fail', 'uncertain'])
def test_overall_pass_cannot_cover_an_unapproved_requirement(verdict):
    catalog = goal_requirement_catalog(*prepared())
    reply = reviewed(catalog)
    reply['goalReview']['requirementReviews'][-1]['verdict'] = verdict
    with pytest.raises(ValueError, match='GOAL_REVIEW_VERDICT_CONFLICT'):
        validate_goal_review_wire(reply, catalog)


@pytest.mark.parametrize('bad_field, value, expected_path, message', [
    ('materialRefs', ['code:2'], ['goalReview', 'requirementReviews', 1], 'materialRefs'),
    ('reason', 42, ['goalReview', 'requirementReviews', 1, 'reason'], "not of type 'string'"),
])
def test_item_schema_error_retains_exact_path_and_reason_without_altering_reply(
        bad_field, value, expected_path, message):
    catalog = goal_requirement_catalog(*prepared())
    reply = reviewed(catalog)
    reply['goalReview']['requirementReviews'][1][bad_field] = value
    original = deepcopy(reply)
    with pytest.raises(ValidationError) as error:
        validate_goal_review_wire(reply, catalog)
    assert list(error.value.absolute_path) == expected_path
    assert message in error.value.message
    assert reply == original
    # The Executor's existing error formatter can now tell repair which field failed.
    formatted = '.'.join(str(x) for x in error.value.absolute_path) + ': ' + error.value.message
    assert 'goalReview.requirementReviews.1' in formatted
    assert message in formatted


@pytest.mark.parametrize('reply', [None, [], 'not-an-object'])
def test_malformed_root_has_a_correctable_error(reply):
    with pytest.raises(ValueError, match='GOAL_REVIEW_COVERAGE_REQUIRED'):
        validate_goal_review_wire(reply, [])


@pytest.mark.parametrize('status, verdict', [('passed', 'pass'), ('failed', 'fail'), ('inconclusive', 'uncertain')])
def test_valid_wire_strips_only_extension_and_retains_detached_audit(status, verdict):
    catalog = goal_requirement_catalog(*prepared())
    reply = reviewed(catalog, status=status, verdict=verdict)
    before = deepcopy(reply)
    legacy, audit = validate_goal_review_wire(reply, catalog)
    assert 'requirementReviews' not in legacy['goalReview']
    assert legacy['goalReview']['status'] == status
    assert legacy['actionReview'] == reply['actionReview']
    assert audit['requirements'] == catalog
    assert audit['requirementReviews'] == reply['goalReview']['requirementReviews']
    assert reply == before
    audit['requirementReviews'][0]['reason'] = '不能改写原回复'
    assert reply == before


def test_helper_with_empty_catalog_accepts_explicit_empty_coverage():
    state, round_state = prepared()
    round_state['prepared']['candidate'] = {'action': {'subgoalIds': []}, 'checkTargets': []}
    catalog = goal_requirement_catalog(state, round_state)
    assert catalog == []
    schema = goal_review_wire_schema(DYNAMIC_REVIEW_SCHEMA, catalog)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema['properties']['goalReview']).validate(reviewed([])['goalReview'])
    legacy, audit = validate_goal_review_wire(reviewed([]), catalog)
    assert audit['requirements'] == audit['requirementReviews'] == []
    assert legacy['goalReview']['status'] == 'passed'


def test_historical_review_still_valid_under_original_schema():
    legacy = reviewed([])['goalReview']
    legacy.pop('requirementReviews')
    Draft202012Validator(DYNAMIC_REVIEW_SCHEMA['properties']['goalReview']).validate(legacy)

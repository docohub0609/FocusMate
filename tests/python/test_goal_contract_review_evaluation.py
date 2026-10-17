"""Fixture and scoring checks: these never call a paid model."""

from collections import Counter
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))


def case_builder():
    path = ROOT / 'tools/goal_contract_review_cases.py'
    assert path.exists(), 'The complete contract-review fixture builder is missing.'
    from tools.goal_contract_review_cases import build_cases
    return build_cases


def runner():
    path = ROOT / 'tools/eval-goal-contract-review.py'
    assert path.exists(), 'The contract-review evaluation runner is missing.'
    spec = importlib.util.spec_from_file_location('goal_contract_evaluation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_set_has_complete_balanced_splits_and_honest_provenance():
    cases = case_builder()()
    assert len(cases) == 24
    assert len({case['id'] for case in cases}) == 24
    assert Counter(case['split'] for case in cases) == {'development': 12, 'holdout': 12}
    approved = [case for case in cases if case['provenance']['humanReview'] == 'representativeApproved']
    assert len(approved) == 6
    assert all(case['split'] == 'development' and case['expected']['status'] == 'needsReview' for case in approved)
    for split in ('development', 'holdout'):
        selected = [case for case in cases if case['split'] == split]
        assert Counter(case['expected']['status'] for case in selected) == {'passed': 6, 'needsReview': 6}
        assert {case['expected']['issueType'] for case in selected if case['expected']['status'] == 'needsReview'} == {
            'numericMismatch', 'conditionMismatch', 'missingRequirement', 'prescribedStrategy',
            'unverifiableObjective', 'insufficientEvidence',
        }
    assert all(case['provenance']['humanFeatureReport'] == 'syntheticFixture' for case in cases)
    assert sum(case['provenance']['humanReview'] == 'aiPreparedUnreviewed' for case in cases) == 18


def test_each_fixture_has_a_full_valid_compilation_and_authoritative_sources():
    from focusmate.contracts.schema_validation import validate
    from focusmate.contracts.problem_source import build_evidence_catalog, problem_content_hash
    for case in case_builder()():
        inputs = case['inputs']
        for key, schema in [('goal_record', 'GoalRecord'), ('problem_record', 'ProblemRecord'),
                            ('feature_set', 'ProblemFeatures'), ('compilation', 'GoalCompilation')]:
            validate(schema, inputs[key])
        compilation = inputs['compilation']
        problem = inputs['problem_record']['problemPackage']
        assert compilation['generation']['problemContentHash'] == problem_content_hash(problem)
        assert compilation['sourceCatalog'] == build_evidence_catalog(problem)
        assert len(problem['examples']) >= 2
        assert {node['stage'] for node in compilation['contract']['subgoals']} == {
            'problemUnderstanding', 'feasibleStrategy', 'coreStateAndCorrectness',
            'coreImplementation', 'boundaryAndTesting',
        }
        assert {item['category'] for item in inputs['feature_set']['features']} == {
            'dataScale', 'inputStructure', 'outputStructure', 'resourceConstraint', 'domainTag',
        }
        assert compilation['generation']['semanticStatus'] == 'unverified'


def test_saved_cases_are_reproducible_and_readable_without_building_a_database():
    cases = case_builder()()
    stored = json.loads((ROOT / 'examples/goal-contract-review/cases.json').read_text(encoding='utf-8'))
    assert stored == cases
    assert cases == case_builder()()


def test_interval_missing_requirement_is_absent_from_every_acceptance_text():
    case = next(case for case in case_builder()() if case['id'] == 'merge-intervals-missing-requirement')
    compilation = case['inputs']['compilation']
    texts = [node['objective'] for node in compilation['contract']['subgoals']]
    texts += [node['text'] for node in compilation['publicGoals']]
    texts += [check['check'] for node in compilation['rubrics'] for check in node['checks']]
    assert not any('完全相同' in text or '不得扩大' in text or '不能填补' in text for text in texts)
    assert any('覆盖每个原区间' in text for text in texts)
    assert any('互不相交' in text for text in texts)
    assert case['expected']['counterexample'] == {'input': '2\n1 2\n5 6\n', 'badOutput': '1\n1 6\n', 'correctOutput': '2\n1 2\n5 6\n'}


def test_unknown_variants_add_only_a_nonessential_unknown():
    cases = case_builder()()
    for case in cases:
        if not case['id'].endswith('-nonessential-unknown'):
            continue
        baseline = next(item for item in cases if item['id'] == case['id'].replace('-nonessential-unknown', '-correct'))
        original = deepcopy(baseline['inputs'])
        actual = deepcopy(case['inputs'])
        before = original['compilation']['generation']['unknowns']
        after = actual['compilation']['generation']['unknowns']
        assert len(after) == len(before) + 1
        actual['compilation']['generation']['unknowns'] = before
        assert actual == original
        assert case['expected']['status'] == 'passed'


def test_runner_keeps_labels_out_of_review_and_separates_misses_false_alarms_and_unavailable():
    module = runner()
    source = case_builder()()
    valid = next(case for case in source if case['id'] == 'two-sum-correct')
    invalid = next(case for case in source if case['id'] == 'two-sum-numeric-mismatch')
    cases = [deepcopy(invalid), deepcopy(valid), deepcopy(invalid), deepcopy(invalid)]
    reports = iter([
        {'status': 'passed', 'issues': [], 'items': [], 'model': 'test-model'},
        {'status': 'needsReview', 'issues': [{'type': 'conditionMismatch'}], 'items': [], 'model': 'test-model'},
        {'status': 'unavailable', 'issues': [], 'items': [], 'model': 'test-model'},
        {'status': 'needsReview', 'issues': [{'type': 'numericMismatch', 'itemId': 'source:constraints:0'}], 'items': [], 'model': 'test-model'},
    ])
    for index, case in enumerate(cases):
        case['id'] = f'count-{index}'
        case['expected']['labelSecret'] = 'LABEL_ONLY_SENTINEL'
        case['provenance']['marker'] = 'PROVENANCE_ONLY_SENTINEL'
    observed = []

    def review(goal_record, problem_record, feature_set, compilation, provider, *, created_at):
        observed.append([goal_record, problem_record, feature_set, compilation])
        return next(reports)

    result = module.evaluate_cases(cases, object(), reviewer=review)
    assert result['missedError'] == 1
    assert result['falseAlarm'] == 1
    assert result['unavailable'] == 1
    assert result['expectedIssueMatch'] == 1
    assert result['matched'] == 1
    assert result['models'] == ['test-model']
    assert 'LABEL_ONLY_SENTINEL' not in json.dumps(observed)
    assert 'PROVENANCE_ONLY_SENTINEL' not in json.dumps(observed)
    assert result['results'][0]['expected']['labelSecret'] == 'LABEL_ONLY_SENTINEL'
    assert result['results'][3]['review']['issues'][0]['type'] == 'numericMismatch'


def test_runner_does_not_count_bad_program_inputs_as_model_detection():
    module = runner()
    cases = [next(case for case in case_builder()() if case['id'] == 'two-sum-numeric-mismatch')]

    class InvalidInput(ValueError):
        code = 'INVALID_GOAL_CONTRACT_REVIEW_INPUT'

    def reject(*args, **kwargs):
        raise InvalidInput('private debug content must not be printed')

    result = module.evaluate_cases(cases, object(), reviewer=reject)
    assert result['inputError'] == 1
    assert result['expectedIssueMatch'] == result['matched'] == result['missedError'] == 0
    assert result['results'][0]['actual'] == 'inputError'
    assert 'private debug content' not in json.dumps(result)


def test_cli_defaults_to_development_and_refuses_overwrite_before_model_setup(tmp_path, monkeypatch):
    module = runner()
    output = tmp_path / 'history.json'
    output.write_text('preserved', encoding='utf-8')
    assert module.main(['--output', str(output)]) == 1
    assert output.read_text(encoding='utf-8') == 'preserved'
    assert [case['split'] for case in module.select_cases(case_builder()(), 'development')] == ['development'] * 12
    assert len(module.select_cases(case_builder()(), 'holdout')) == 12
    assert len(module.select_cases(case_builder()(), 'all')) == 24


def test_declared_alternative_issue_types_must_point_to_the_relevant_requirement():
    module = runner()
    case = next(case for case in case_builder()() if case['id'] == 'two-sum-condition-mismatch')
    assert case['expected']['acceptableIssueTypes'] == ['conditionMismatch', 'objectMismatch']
    for item_id, expected_count in [('source:statement:1', 1), ('source:outputSpec:0', 0)]:
        def review(*args, **kwargs):
            return {'status': 'needsReview', 'issues': [{'type': 'objectMismatch', 'itemId': item_id}],
                    'items': [], 'model': 'test-model'}
        result = module.evaluate_cases([case], object(), reviewer=review)
        assert result['expectedIssueMatch'] == expected_count
        assert result['matched'] == expected_count


def test_all_cases_reach_semantic_review_input_validation_without_compiler_or_database(monkeypatch):
    import sqlite3
    import focusmate.core.goal_compilation as compiler
    from focusmate.core.goal_contract_review import validate_review_input

    def forbidden(*args, **kwargs):
        raise AssertionError('Evaluation fixture construction cannot use a DB or the T041 compiler.')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(compiler, 'compile_goal_contract', forbidden)
    for case in case_builder()():
        inputs = case['inputs']
        snapshot = validate_review_input(inputs['goal_record'], inputs['problem_record'],
                                         inputs['feature_set'], inputs['compilation'])
        assert len(snapshot['scanItems']) > 5


def test_labels_and_generation_metadata_never_reach_actual_model_prompt():
    from focusmate.contracts.goal_compilation import GoalCompilationResponse
    case = next(case for case in case_builder()() if case['id'] == 'two-sum-correct')
    case['expected']['sentinel'] = 'NEVER_SEND_EXPECTED_SENTINEL'
    case['provenance']['sentinel'] = 'NEVER_SEND_PROVENANCE_SENTINEL'
    prompts = []

    class Provider:
        def complete(self, *, system_prompt, user_prompt):
            prompts.append(system_prompt + user_prompt)
            payload = json.loads(user_prompt)
            response = {'items': [
                {'itemId': item['itemId'], 'verdict': 'supported', 'issueTypes': [],
                 'reason': '协议测试意见，不表示真实语义准确率。', 'sourceRefs': ['statement:0'],
                 'templateRefs': [], 'blocking': False}
                for item in payload['scanItems']
            ]}
            return GoalCompilationResponse(json.dumps(response), 'test-protocol-model', 'test-request')

    result = runner().evaluate_cases([case], Provider())
    assert result['matched'] == 1
    assert result['models'] == ['test-protocol-model']
    assert len(prompts) == 1
    assert 'NEVER_SEND_EXPECTED_SENTINEL' not in prompts[0]
    assert 'NEVER_SEND_PROVENANCE_SENTINEL' not in prompts[0]
    assert 'synthetic-fixture-no-model' not in prompts[0]


def test_cli_preserves_each_completed_real_review_when_interrupted(tmp_path, monkeypatch):
    from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
    from focusmate.contracts.goal_compilation import GoalCompilationResponse
    calls = []

    class InterruptedProvider:
        def complete(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            payload = json.loads(kwargs['user_prompt'])
            candidate = {'items': [{'itemId': item['itemId'], 'verdict': 'supported',
                                    'issueTypes': [], 'reason': 'Protocol-only test.',
                                    'sourceRefs': ['statement:0'], 'templateRefs': [], 'blocking': False}
                                   for item in payload['scanItems']]}
            return GoalCompilationResponse(json.dumps(candidate), 'test-interrupted-model', 'request-one')

    monkeypatch.setattr(DeepSeekGoalContractReviewProvider, 'from_environment', lambda: InterruptedProvider())
    output = tmp_path / 'partial.json'
    with pytest.raises(KeyboardInterrupt):
        runner().main(['--output', str(output)])
    partial = json.loads(output.read_text(encoding='utf-8'))
    assert partial['completed'] is False
    assert partial['processedCount'] == 1
    assert len(partial['results']) == 1
    assert partial['split'] == 'development'
    rows = [json.loads(line) for line in output.with_name('partial.json.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 1
    assert rows[0]['review']['providerResponseId'] == 'request-one'
    assert rows[0]['actual'] == 'passed'


def test_cli_does_not_overwrite_an_existing_incremental_log(tmp_path, monkeypatch):
    from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
    def forbidden():
        raise AssertionError('Existing logs must be checked before provider setup.')
    monkeypatch.setattr(DeepSeekGoalContractReviewProvider, 'from_environment', forbidden)
    output = tmp_path / 'run.json'
    log = tmp_path / 'run.json.jsonl'
    log.write_text('previous review', encoding='utf-8')
    assert runner().main(['--output', str(output)]) == 1
    assert log.read_text(encoding='utf-8') == 'previous review'
    assert not output.exists()

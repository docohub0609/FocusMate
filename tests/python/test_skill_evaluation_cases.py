from collections import Counter, defaultdict
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tools.skill_evaluation import catalog
from tools.skill_evaluation_cases import build_cases


ROOT = Path(__file__).resolve().parents[2]
SKILLS = {
    'problem-understanding', 'constraint-and-complexity', 'array-and-hash',
    'two-pointers-and-window', 'binary-search', 'stack-and-queue',
    'tree-and-graph', 'dynamic-programming', 'testing-and-edge-cases',
}
CATEGORIES = {'issue', 'correct', 'notApplicable'}
SPLITS = {'development', 'holdout'}


def test_builds_balanced_distinct_case_matrix():
    cases = build_cases()
    assert len(cases) == 54
    assert len({case['id'] for case in cases}) == 54
    assert Counter((c['skillId'], c['split'], c['expected']['category']) for c in cases) == {
        (skill, split, category): 1
        for skill in SKILLS for split in SPLITS for category in CATEGORIES
    }


def test_every_case_matches_skill_input_contract_and_references_original_material():
    for case in build_cases():
        schema = json.loads((ROOT / 'skills' / case['skillId'] / 'schemas/input.schema.json').read_text(encoding='utf-8'))
        Draft202012Validator(schema).validate(case['inputs'])
        available = catalog(case['inputs'])
        requirements = case['expected']['requirements']
        assert requirements
        assert all(set(requirement['evidenceRefs']) <= available for requirement in requirements)
        assert all(requirement['evidenceRefs'] for requirement in requirements)


def test_holdout_problem_families_are_separate_and_each_skill_has_six_situations():
    by_skill = defaultdict(list)
    for case in build_cases():
        by_skill[case['skillId']].append(case)
    for cases in by_skill.values():
        assert len({case['problemFamily'] for case in cases}) == 6
        development = {case['problemFamily'] for case in cases if case['split'] == 'development'}
        holdout = {case['problemFamily'] for case in cases if case['split'] == 'holdout'}
        assert development.isdisjoint(holdout)


def test_cases_retain_goal_context_and_truthful_provenance():
    for case in build_cases():
        assert case['goalContext']['goalId']
        assert case['goalContext']['scope'] == 'subgoal'
        assert case['goalContext']['subgoalId']
        assert case['provenance'] == {
            'draftedBy': 'AI', 'independentlyReviewed': False,
            'humanReviewed': False,
            'representativeScaleApproved': (
                case['split'] == 'development' and case['expected']['category'] == 'issue'
            ),
        }


def test_not_applicable_is_decided_by_current_request():
    for case in build_cases():
        if case['expected']['category'] == 'notApplicable':
            assert case['inputs']['userRequest']['text'].startswith(('请把', '请只', '帮我'))
            assert any('当前请求' in req['text'] for req in case['expected']['requirements'])


def test_representative_development_issues_match_the_nine_approved_cards():
    expected_families = {
        'problem-understanding': 'return-indices',
        'constraint-and-complexity': 'quadratic-without-limits',
        'array-and-hash': 'self-pair-after-insert',
        'two-pointers-and-window': 'negative-window-monotonicity',
        'binary-search': 'non-shrinking-left-bound',
        'stack-and-queue': 'pop-empty-stack',
        'tree-and-graph': 'cycle-without-visited',
        'dynamic-programming': 'forward-01-update',
        'testing-and-edge-cases': 'stale-version-evidence',
    }
    actual = {c['skillId']: c['problemFamily'] for c in build_cases()
              if c['split'] == 'development' and c['expected']['category'] == 'issue'}
    assert actual == expected_families


def test_material_reference_names_do_not_encode_case_labels():
    for case in build_cases():
        references = catalog(case['inputs']) | {run['codeSnapshotRef'] for run in case['inputs']['runEvidence']}
        assert all(not any(label in ref for label in ('development', 'holdout', 'issue', 'correct', 'notApplicable'))
                   for ref in references)

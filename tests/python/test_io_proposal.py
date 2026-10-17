from copy import deepcopy

import pytest

from apps.api.problem_features import FeatureExtractionFailure, extract_problem_features
from apps.api.problem_features import build_human_feature_set
from apps.api.problem_storage import ProblemRepository, StorageFailure
from tests.python.test_problem_features import FakeProvider, base_features, problem_request


def proposal():
    return {
        'reason': '原题没有说明数组的分隔方式，建议明确为两行输入。',
        'inputSpec': '第一行输入数组，使用 JSON 格式；第二行输入整数 k。',
        'outputSpec': '输出 JSON 数组，不包含“输出：”前缀。',
        'examples': [{'input': '[1,2,3]\n2\n', 'output': '[2,1,3]\n'}],
        'evidenceRefs': ['inputSpec:0', 'outputSpec:0'],
    }


def test_proposal_is_separate_reviewed_and_does_not_modify_source():
    request = problem_request()
    original = deepcopy(request)
    result = extract_problem_features(request, FakeProvider([{'features': base_features(), 'ioProposal': proposal()}]))
    assert request == original
    assert result['ioProposal']['suggestion'] == proposal()
    assert result['ioProposal']['original']['inputSpec'] == original['problemPackage']['inputSpec']
    assert result['semanticReview']['items'][-1]['itemId'] == 'io-proposal'
    assert all(f['featureKey'] != 'io.proposal' for f in result['features'])


@pytest.mark.parametrize('change', [
    {'evidenceRefs': ['not-real']},
    {'examples': []},
    {'examples': proposal()['examples'] * 2},
    {'inputSpec': ''},
])
def test_invalid_proposals_are_rejected(change):
    value = {**proposal(), **change}
    with pytest.raises(FeatureExtractionFailure):
        extract_problem_features(problem_request(), FakeProvider([
            {'features': base_features(), 'ioProposal': value},
            {'features': base_features(), 'ioProposal': value},
        ]))


def test_absent_proposal_keeps_legacy_reports_compatible():
    result = extract_problem_features(problem_request(), FakeProvider([{'features': base_features()}]))
    assert 'ioProposal' not in result


def test_confirmation_requires_explicit_decision_and_preserves_source(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    problem = repo.create(problem_request()['problemPackage'])['problemPackage']
    report = extract_problem_features({'problemPackage': problem}, FakeProvider([
        {'features': base_features(), 'ioProposal': proposal()},
    ]))
    repo.save_feature_set(report)
    body = {'expectedDraftRevision': 1, 'basedOnFeatureSetId': report['featureSetId'], 'features': base_features()}
    with pytest.raises(FeatureExtractionFailure):
        build_human_feature_set(problem, body, base_feature_set=report)
    with pytest.raises(StorageFailure):
        repo.freeze(problem['problemId'], 1, report['featureSetId'], 1)
    bypass = build_human_feature_set(problem, {'expectedDraftRevision': 1, 'features': base_features()})
    with pytest.raises(StorageFailure):
        repo.confirm_and_freeze(problem['problemId'], 1, bypass, 1, None)
    accepted = build_human_feature_set(problem, {**body, 'ioProposalDecision': 'keepOriginal'}, base_feature_set=report)
    repo.confirm_and_freeze(problem['problemId'], 1, accepted, 1, report['featureSetId'])
    assert repo.get(problem['problemId'], 1)['problemPackage'] == problem
    assert repo.confirmed_feature_set(problem['problemId'], 1)['ioProposalDecision'] == 'keepOriginal'
    with pytest.raises(StorageFailure):
        repo.update_draft(problem['problemId'], 1, {**problem, 'inputSpec': 'changed'}, 1)


def test_adopting_format_invalidates_old_report_and_retains_proposal_history(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    problem = repo.create(problem_request()['problemPackage'])['problemPackage']
    report = extract_problem_features({'problemPackage': problem}, FakeProvider([
        {'features': base_features(), 'ioProposal': proposal()},
    ]))
    repo.save_feature_set(report)
    updated = {**problem, **{key: proposal()[key] for key in ('inputSpec', 'outputSpec', 'examples')}}
    saved = repo.update_draft(problem['problemId'], 1, updated, 1)
    assert saved['lifecycle']['contentHash'] != report['problemContentHash']
    assert saved['problemPackage']['examples'] == proposal()['examples']
    with pytest.raises(StorageFailure):
        repo.freeze(problem['problemId'], 1, report['featureSetId'], 2)
    with pytest.raises(StorageFailure):
        repo.update_draft(problem['problemId'], 1, updated, 1)


def test_missing_proposal_review_is_not_reported_as_reviewed():
    class Incomplete(FakeProvider):
        def review(self, problem, *, features, evidence_catalog):
            return super().review(problem, features=[x for x in features if x['itemId'] != 'io-proposal'], evidence_catalog=evidence_catalog)
    report = extract_problem_features(problem_request(), Incomplete([{'features': base_features(), 'ioProposal': proposal()}]))
    assert report['semanticReview']['status'] == 'unavailable'
    assert report['status'] == 'reviewRequired'


def test_bad_display_paths_use_existing_single_repair_with_specific_paths():
    from apps.api.feature_presentation import with_legacy_labels
    good = [with_legacy_labels(f) for f in base_features()]
    bad = deepcopy(good)
    bad[0]['presentation']['labels'] = {'/minimum': '最小值'}
    provider = FakeProvider([{'features': bad}, {'features': good}])
    result = extract_problem_features(problem_request(), provider)
    assert '/value/minimum' in provider.calls[1]
    assert result['features'][0]['presentation'] == good[0]['presentation']

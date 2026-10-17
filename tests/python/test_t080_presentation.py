from copy import deepcopy
from importlib import import_module

import pytest

from apps.api.problem_features import build_human_feature_set, FeatureExtractionFailure
from tests.python.test_problem_features import base_features, problem_request


def test_missing_nested_label_cannot_be_confirmed_as_fully_displayed():
    module = import_module('apps.api.feature_presentation')
    feature = base_features()[0]
    feature['presentation'] = {'title': '数组长度', 'labels': {'/value': '范围', '/value/variable': '变量', '/value/minimum': '最小长度'}}
    assert not module.complete_presentation(feature)
    feature['presentation']['labels']['/value/maximum'] = '最大长度'
    assert module.complete_presentation(feature)
    feature['presentation']['labels']['/value/invented'] = '不存在'
    assert not module.complete_presentation(feature)


def test_manual_confirmation_rejects_duplicate_keys():
    features = base_features()
    features.append(deepcopy(features[0]))
    with pytest.raises(FeatureExtractionFailure):
        build_human_feature_set(problem_request()['problemPackage'], {'features': features, 'expectedDraftRevision': 1})


def test_web_confirmation_requires_all_display_labels():
    with pytest.raises(FeatureExtractionFailure):
        build_human_feature_set(problem_request()['problemPackage'], {
            'features': base_features(), 'expectedDraftRevision': 1,
            'requireCompletePresentation': True})
    from apps.api.feature_presentation import with_legacy_labels
    result = build_human_feature_set(problem_request()['problemPackage'], {
        'features': [with_legacy_labels(f) for f in base_features()],
        'expectedDraftRevision': 1, 'requireCompletePresentation': True})
    assert result['features']


def test_presentation_survives_hydration_and_is_seen_by_reviewer():
    from apps.api.problem_features import extract_problem_features
    from tests.python.test_problem_features import FakeProvider
    class Provider(FakeProvider):
        def review(self, problem, *, features, evidence_catalog):
            assert features[0]['presentation']['title'] == '数组长度'
            return super().review(problem, features=features, evidence_catalog=evidence_catalog)
    features = base_features()
    features[0]['presentation'] = {'title': '数组长度', 'labels': {
        '/value': '范围', '/value/variable': '变量', '/value/minimum': '最小长度', '/value/maximum': '最大长度'}}
    report = extract_problem_features(problem_request(), Provider([{'features': features}]))
    assert report['features'][0]['presentation'] == features[0]['presentation']
    assert report['semanticReview']['status'] == 'completed'

from copy import deepcopy
import json

from apps.api.deepseek_provider import DeepSeekConfig, DeepSeekProvider, ProviderFailure, ProviderResponse, StructuredChatTransport
from apps.api.problem_features import extract_problem_features
from tests.python.test_problem_features import FakeProvider, base_features, problem_request


class ReviewedProvider(FakeProvider):
    def __init__(self, features, verdict='supported', broken=False):
        super().__init__([{'features': features}])
        self.verdict, self.broken = verdict, broken
        self.review_calls = []

    def review(self, problem, *, features, evidence_catalog):
        self.review_calls.append((deepcopy(problem), deepcopy(features), deepcopy(evidence_catalog)))
        if self.broken:
            raise ProviderFailure('LLM_UNAVAILABLE', 'PRIVATE FAILURE BODY', True)
        items = []
        for index, feature in enumerate(features):
            verdict = self.verdict if index == 0 else 'supported'
            refs = feature.get('evidenceRefs', [])
            items.append({'itemId': feature['itemId'], 'verdict': verdict,
                          'issue': ('objectMismatch' if verdict == 'contradicted' else 'insufficientEvidence' if verdict == 'insufficient' else 'none'),
                          'evidenceRefs': refs})
        return ProviderResponse('review-1', 'fake-review', json.dumps({'items': items}))


def test_semantic_review_catches_object_mismatch_without_changing_extracted_value():
    features = base_features()
    features[0]['value']['maximum'] = 10000
    provider = ReviewedProvider(features, 'contradicted')
    result = extract_problem_features(problem_request(constraints='1 <= n <= 100; each element is at most 10000.'), provider)
    assert result['features'][0]['value']['maximum'] == 10000
    assert result['features'][0]['review']['issue'] == 'semanticContradiction'
    assert result['semanticReview']['status'] == 'completed'
    assert result['semanticReview']['items'][0]['verdict'] == 'contradicted'
    assert len(provider.calls) == len(provider.review_calls) == 1
    assert result['status'] == 'reviewRequired'


def test_supported_review_preserves_unknown_and_local_number_warning():
    features = base_features()
    features[0]['value']['maximum'] = 999999
    provider = ReviewedProvider(features)
    result = extract_problem_features(problem_request(), provider)
    assert result['semanticReview']['status'] == 'completed'
    assert result['features'][0]['review']['issue'] == 'locallyUnverifiable'
    assert result['features'][3]['status'] == 'unknown'
    assert 'value' not in result['features'][3]
    assert result['provenance']['type'] == 'llmGenerated'


def test_review_failure_retains_extraction_and_requests_review_without_regenerating():
    provider = ReviewedProvider(base_features(), broken=True)
    result = extract_problem_features(problem_request(), provider)
    assert len(provider.calls) == len(provider.review_calls) == 1
    assert result['semanticReview']['status'] == 'unavailable'
    assert result['status'] == 'reviewRequired'
    assert result['features'][0]['value']['maximum'] == 100000
    assert 'PRIVATE' not in json.dumps(result)


def test_provider_without_review_is_explicitly_unavailable():
    class ExtractionOnly:
        __init__ = FakeProvider.__init__
        extract = FakeProvider.extract
    result = extract_problem_features(problem_request(), ExtractionOnly([{'features': base_features()}]))
    assert result['semanticReview']['status'] == 'unavailable'
    assert result['features'][0]['review']['issue'] == 'semanticReviewUnavailable'


def test_deepseek_review_uses_separate_prompt_and_whitelisted_source(monkeypatch):
    calls = []
    def complete(self, **kwargs):
        calls.append(kwargs)
        return ProviderResponse('review-real', 'review-model', '{"items":[]}')
    monkeypatch.setattr(StructuredChatTransport, 'complete', complete)
    provider = DeepSeekProvider(DeepSeekConfig('test-key', 'https://example.test', 'test-model'))
    problem = problem_request()['problemPackage']
    problem['hiddenTests'] = 'PRIVATE HIDDEN TEST'
    result = provider.review(problem, features=[], evidence_catalog=[])
    assert result.response_id == 'review-real'
    assert len(calls) == 1
    assert 'object' in calls[0]['system_prompt'].lower()
    assert 'contradicted' in calls[0]['system_prompt']
    payload = json.loads(calls[0]['user_prompt'])
    assert 'PRIVATE' not in json.dumps(payload)
    assert set(payload['problemSource']) == {'statement','inputSpec','outputSpec','constraints','examples'}


def test_insufficient_evidence_is_a_per_feature_warning():
    provider = ReviewedProvider(base_features(), 'insufficient')
    result = extract_problem_features(problem_request(), provider)
    assert result['features'][0]['review']['issue'] == 'semanticInsufficient'
    assert result['semanticReview']['items'][0]['verdict'] == 'insufficient'


def test_extraction_format_repair_is_followed_by_only_one_semantic_review():
    provider = ReviewedProvider(base_features())
    provider.responses.insert(0, '')
    result = extract_problem_features(problem_request(), provider)
    assert len(provider.calls) == 2
    assert len(provider.review_calls) == 1
    assert result['semanticReview']['status'] == 'completed'


def test_http_review_failure_returns_editable_report(monkeypatch):
    from tests.python.test_problem_feature_api import ApiFakeProvider, running_api, post
    def broken(*args, **kwargs):
        raise ProviderFailure('LLM_UNAVAILABLE','PRIVATE RESPONSE',True)
    monkeypatch.setattr(ApiFakeProvider,'review',broken)
    with running_api() as url:
        status, result, _ = post(url,problem_request())
    assert status == 201
    assert result['semanticReview']['status'] == 'unavailable'
    assert result['status'] == 'reviewRequired'
    assert 'PRIVATE' not in json.dumps(result)


def test_review_metadata_persists_and_is_not_reused_after_human_edit(tmp_path):
    from apps.api.problem_features import build_human_feature_set
    from apps.api.problem_storage import ProblemRepository
    repo=ProblemRepository(tmp_path/'review.db')
    problem=problem_request()['problemPackage']
    repo.create(problem)
    report=extract_problem_features({'problemPackage':problem}, ReviewedProvider(base_features(),'contradicted'))
    repo.save_feature_set(report)
    reopened=ProblemRepository(repo.path)
    assert reopened.get_feature_set(report['featureSetId'])['semanticReview'] == report['semanticReview']
    human=build_human_feature_set(problem,{'expectedDraftRevision':1,'basedOnFeatureSetId':report['featureSetId'],
                                        'features':base_features()},base_feature_set=report)
    assert 'semanticReview' not in human
    frozen=reopened.confirm_and_freeze(problem['problemId'],problem['version'],human,1,report['featureSetId'])
    assert frozen['lifecycle']['status']=='frozen'
    assert reopened.get_feature_set(report['featureSetId'])['semanticReview']==report['semanticReview']


def test_flagged_confirmed_feature_is_not_used_as_builder_fact(tmp_path):
    import pytest
    from apps.api.problem_storage import ProblemRepository
    from services.verifier_builder.candidates import _context, CandidateFailure
    repo=ProblemRepository(tmp_path/'builder.db')
    problem=problem_request()['problemPackage']
    repo.create(problem)
    result=extract_problem_features({'problemPackage':problem},ReviewedProvider(base_features(),'contradicted'))
    repo.save_feature_set(result)
    frozen=repo.freeze(problem['problemId'],problem['version'],result['featureSetId'],1)
    with pytest.raises(CandidateFailure) as caught:
        _context(frozen,result)
    assert caught.value.code=='PROBLEM_FACTS_INSUFFICIENT'


def test_demo_displays_review_failure_and_does_not_call_it_verified(capsys):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'tools/demo-problem-features.py'
    spec=importlib.util.spec_from_file_location('feature_demo',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result=extract_problem_features(problem_request(),ReviewedProvider(base_features(),broken=True))
    module._print_result(result,0.1)
    output=capsys.readouterr().out
    assert '语义审核未完成' in output
    assert '原文确定' not in output

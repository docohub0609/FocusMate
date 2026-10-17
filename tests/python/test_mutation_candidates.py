import json
import time

import pytest

from apps.api.deepseek_provider import DeepSeekConfig, ProviderResponse
from tests.python.test_verifier_candidates import context
from services.verifier_builder.candidates import CandidateFailure
from services.verifier_builder.mutation_candidates import MutationProvider


class Transport:
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def complete(self, **kwargs):
        payload = json.loads(kwargs['user_prompt'])
        self.calls.append(payload)
        doc = {k: payload[k] for k in ('schemaVersion', 'role', 'problemRef', 'problemContentHash', 'featureSetId')}
        if payload['role'] == 'mutationGenerator':
            doc['mutants'] = [{'mutantKey': 'boundary', 'defectTag': 'offByOne', 'source': 'print(0)', 'witnessInput': '1\n2\n', 'defectDescription': 'Drops final item.', 'legalityReason': 'One item is permitted.'}]
        else:
            doc['reviews'] = [{'mutantKey': 'boundary', 'judgment': 'legal', 'reason': 'One item is permitted.', 'featureKeys': ['dataScale.n']}]
        if self.change:
            self.change(doc)
        return ProviderResponse('response-1', 'deepseek-test', json.dumps(doc))


def test_generate_and_independent_review(context):
    transport = Transport()
    provider = MutationProvider(transport)
    deadline = time.monotonic() + 5
    mutants = provider.generate(*context, deadline)
    assert mutants[0].witness_input == '1\n2\n'
    assert mutants[0].prompt_version
    assert provider.assess_witnesses(*context, mutants, deadline) == {'boundary': 'legal'}
    review = transport.calls[1]
    assert review['witnesses'] == [{'mutantKey': 'boundary', 'witnessInput': '1\n2\n'}]
    assert 'source' not in review and 'legalityReason' not in json.dumps(review['witnesses'])
    assert provider.last_witness_reviews[0]['featureKeys'] == ['dataScale.n']


@pytest.mark.parametrize('kind', ['duplicate', 'blank', 'tooMany', 'wrongContext'])
def test_invalid_generation_is_safe(context, kind):
    def change(doc):
        if kind == 'duplicate': doc['mutants'] *= 2
        if kind == 'blank': doc['mutants'][0]['legalityReason'] = ' '
        if kind == 'tooMany': doc['mutants'] *= 4
        if kind == 'wrongContext': doc['featureSetId'] = 'other'
    with pytest.raises(CandidateFailure) as caught:
        MutationProvider(Transport(change)).generate(*context, time.monotonic() + 5)
    assert caught.value.code == 'CANDIDATE_SCHEMA_INVALID'


def test_review_rejects_unknown_reference(context):
    provider = MutationProvider(Transport())
    mutants = provider.generate(*context, time.monotonic() + 5)
    provider.transport.change = lambda doc: doc['reviews'][0].update(featureKeys=['invented'])
    with pytest.raises(CandidateFailure):
        provider.assess_witnesses(*context, mutants, time.monotonic() + 5)


def test_environment_enables_thinking(monkeypatch):
    monkeypatch.setattr(DeepSeekConfig, 'from_environment', lambda: DeepSeekConfig('secret', 'https://api.deepseek.com', 'model'))
    config = MutationProvider.from_environment().transport.config
    assert config.thinking_enabled is True
    assert config.max_tokens == 32768


def test_deadline_prevents_network(context):
    transport = Transport()
    with pytest.raises(CandidateFailure) as caught:
        MutationProvider(transport).generate(*context, time.monotonic() - 1)
    assert caught.value.code == 'BUILD_BUDGET_EXCEEDED'
    assert not transport.calls


def test_repair_only_carries_bounded_public_feedback(context):
    transport = Transport()
    provider = MutationProvider(transport)
    feedback = {'priorMutants': [{'mutantKey': 'boundary', 'source': 'print(0)'}],
                'publicFailures': [{'mutantKey': 'boundary', 'exampleIndex': 0, 'status': 'wrongAnswer', 'actualOutput': '0'}]}
    provider.generate(*context, time.monotonic() + 5, feedback)
    assert transport.calls[0]['repairFeedback'] == feedback
    with pytest.raises(CandidateFailure):
        provider.generate(*context, time.monotonic() + 5, {'privateCases': ['secret']})


def test_review_unknown_is_not_promoted(context):
    provider = MutationProvider(Transport())
    mutants = provider.generate(*context, time.monotonic() + 5)
    provider.transport.change = lambda doc: doc['reviews'][0].update(judgment='unknown', featureKeys=[])
    assert provider.assess_witnesses(*context, mutants, time.monotonic() + 5) == {'boundary': 'unknown'}


def test_feedback_rejects_nested_private_payload(context):
    with pytest.raises(CandidateFailure):
        MutationProvider(Transport()).generate(*context, time.monotonic() + 5,
            {'publicFailures': [{'actualOutput': {'privateCases': ['secret']}}]})


def test_private_request_diagnostics_capture_visible_response_and_schema_path(context):
    provider = MutationProvider(Transport(lambda doc: doc['mutants'][0].pop('witnessInput')))
    with pytest.raises(CandidateFailure) as caught:
        provider.generate(*context, time.monotonic() + 5)
    assert caught.value.code == 'CANDIDATE_SCHEMA_INVALID'
    request = provider.last_requests[0]
    assert request['role'] == 'mutationGenerator'
    assert request['model'] == 'deepseek-test'
    assert request['responseId'] == 'response-1'
    assert json.loads(request['content'])['mutants'][0]['source'] == 'print(0)'
    assert request['validationErrors'][0]['path'] == ['mutants', 0]
    assert 'witnessInput' in request['validationErrors'][0]['reason']
    assert 'reasoning' not in request and 'apiKey' not in request


def test_private_request_diagnostics_capture_metadata_validation(context):
    class BadMetadata(Transport):
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            return ProviderResponse('response with spaces', response.model, response.content)
    provider = MutationProvider(BadMetadata())
    with pytest.raises(CandidateFailure):
        provider.generate(*context, time.monotonic() + 5)
    assert provider.last_requests[0]['responseId'] == 'response with spaces'
    assert provider.last_requests[0]['validationErrors'][0]['reason'] == 'metadata'


def test_truncated_visible_json_is_diagnosed_without_retry(context):
    class Truncated(Transport):
        config = DeepSeekConfig('SECRET-KEY', 'https://api.deepseek.com', 'model', max_tokens=8192, thinking_enabled=True)
        def complete(self, **kwargs):
            response = super().complete(**kwargs)
            return ProviderResponse(response.response_id, response.model, response.content[:-8])
    transport = Truncated()
    provider = MutationProvider(transport)
    with pytest.raises(CandidateFailure) as caught:
        provider.generate(*context, time.monotonic() + 5)
    assert caught.value.code == 'CANDIDATE_SCHEMA_INVALID'
    assert len(transport.calls) == 1
    request = provider.last_requests[0]
    assert request['failureCode'] == 'responseInvalidJson'
    assert request['config']['maxTokens'] == 8192
    assert request['config']['thinkingEnabled'] is True
    assert 'SECRET-KEY' not in json.dumps(request)


def test_provider_length_stop_is_not_accepted_even_if_json_parses(context):
    transport = Transport()
    transport.last_response_metadata = {'finishReason': 'length', 'completionTokens': 8192}
    provider = MutationProvider(transport)
    with pytest.raises(CandidateFailure) as caught:
        provider.generate(*context, time.monotonic()+5)
    assert caught.value.code == 'CANDIDATE_SCHEMA_INVALID'
    assert provider.last_requests[0]['failureCode'] == 'responseTokenLimit'
    assert provider.last_requests[0]['responseMetadata']['finishReason'] == 'length'
    assert len(transport.calls) == 1


def test_empty_truncated_envelope_preserves_diagnostic(context):
    from apps.api.deepseek_provider import ProviderFailure
    class Empty(Transport):
        last_response_metadata = {'finishReason': 'length', 'completionTokens': 8192}
        def complete(self, **kwargs):
            raise ProviderFailure('LLM_OUTPUT_INVALID', 'Invalid envelope', True)
    provider = MutationProvider(Empty())
    with pytest.raises(CandidateFailure):
        provider.generate(*context, time.monotonic()+5)
    assert provider.last_requests[0]['failureCode'] == 'responseTokenLimit'

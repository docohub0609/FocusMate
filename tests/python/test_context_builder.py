from copy import deepcopy
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

import pytest

from focusmate.core.context_builder import build_context, MapContextResolver, hash_content, estimate_tokens
from focusmate.contracts.skill_execution import ProviderResponse, ProviderError


GOAL = {'scope': 'subgoal', 'goalId': 'g1', 'subgoalId': 's1'}


def test_larger_budget_prefers_full_original_over_cached_summary():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    first = build_context(req, MapContextResolver(records), Provider())
    assert first['status'] == 'ready'
    req['previousSummary'] = first['summary']
    req['budget']['contextWindowTokens'] = 100000
    second = build_context(req, MapContextResolver(records), Provider())
    assert second['status'] == 'ready'
    assert 'dialogueSummary' not in second['snapshot']['modelPayload']
    assert len(second['snapshot']['modelPayload']['dialogue']) == len(records)


def fixture(count=6, size=500):
    records = [{'ref': f'm{i}', 'goalContext': GOAL, 'kind': 'dialogue', 'version': '1',
                'sequence': i, 'turnId': str(i // 2),
                'content': {'ref': f'm{i}', 'role': 'user' if i % 2 == 0 else 'assistant',
                            'text': '容量不超过 j。' + '讨论。' * size}} for i in range(count)]
    for r in records:
        r['contentHash'] = hash_content(r['content'])
    req = {'goalContext': GOAL, 'purpose': 'skillExecution', 'policyVersion': 't056-v1',
           'systemPrompt': 'Only use evidence.', 'mandatoryPayload': {'goalContext': GOAL, 'code': 'dp=[0]*5'},
           'dialogueRefs': [r['ref'] for r in records], 'recentMessageCount': 2,
           'maxSummaryMessages': 30,
           'budget': {'contextWindowTokens': 100000, 'reservedOutputTokens': 1000, 'safetyMarginTokens': 100}}
    return req, records


class Provider:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        data = json.loads(kwargs['user_prompt'])
        ref = data['messages'][0]['ref']
        value = self.response or {'items': [{'kind': 'userStatement', 'text': '容量不超过 j。',
                     'quotes': [{'ref': ref, 'text': '容量不超过 j。'}]}]}
        return ProviderResponse(json.dumps(value, ensure_ascii=False), 'fake')


def test_short_history_never_calls_model_and_detaches_inputs():
    req, records = fixture()
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready'
    assert not p.calls
    assert out['snapshot']['modelPayload']['code'] == req['mandatoryPayload']['code']
    records[0]['content']['text'] = 'changed'
    req['mandatoryPayload']['code'] = 'changed'
    assert out['snapshot']['modelPayload']['code'] == 'dp=[0]*5'


def test_hash_stable_excludes_identity_and_time():
    req, records = fixture()
    a = build_context(req, MapContextResolver(records))['snapshot']
    b = build_context(req, MapContextResolver(records))['snapshot']
    assert a['contextSnapshotId'] != b['contextSnapshotId']
    assert a['contentHash'] == b['contentHash']


@pytest.mark.parametrize('change,code', [
    (lambda r: r[0].update(goalContext={**GOAL, 'goalId': 'wrong'}), 'SOURCE_SCOPE_MISMATCH'),
    (lambda r: r[0].update(contentHash='sha256:' + '0'*64), 'SOURCE_HASH_MISMATCH'),
    (lambda r: r[0].update(kind='hiddenTest'), 'SOURCE_KIND_FORBIDDEN'),
])
def test_invalid_sources_do_not_call_model(change, code):
    req, records = fixture()
    change(records)
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['failure']['code'] == code
    assert not p.calls


def test_compacts_old_complete_turns_once_retains_recent_original():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready', out
    assert len(p.calls) == 1
    assert [r['ref'] for r in out['snapshot']['modelPayload']['dialogue']] == ['m4', 'm5']
    assert out['summary']['sourceRefs'] == ['m0', 'm1', 'm2', 'm3']
    assert out['summary']['semanticStatus'] == 'notVerified'


@pytest.mark.parametrize('response,code', [
    ({'items': []}, 'SUMMARY_INVALID'),
    ({'items': [{'kind':'correction','text':'x','quotes':[{'ref':'missing','text':'x'}]}]}, 'SUMMARY_INVALID'),
    ({'items': [{'kind':'correction','text':'x','quotes':[{'ref':'m0','text':'fabricated'}]}]}, 'SUMMARY_INVALID'),
    (ProviderError(), 'SUMMARY_SERVICE_FAILED'),
])
def test_bad_summary_is_failure_not_success(response, code):
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    p = Provider(response)
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'failed'
    assert out['failure']['code'] == code
    assert len(p.calls) == 1
    assert 'snapshot' not in out


def test_review_never_replaces_original_evidence_with_summary():
    req, records = fixture()
    req['purpose'] = 'skillReview'
    req['budget']['contextWindowTokens'] = 12000
    p = Provider()
    assert build_context(req, MapContextResolver(records), p)['failure']['code'] == 'REQUIRED_MATERIAL_TOO_LONG'
    assert not p.calls


def test_mandatory_overflow_names_large_payload_and_no_model_call():
    req, records = fixture()
    req['mandatoryPayload']['code'] = '代码' * 40000
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['failure']['code'] == 'REQUIRED_MATERIAL_TOO_LONG'
    assert out['failure']['materials'][0]['ref'] == 'payload:code'
    assert not p.calls


def test_reuses_summary_only_when_original_source_hashes_match():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    first = build_context(req, MapContextResolver(records), Provider())
    req['previousSummary'] = first['summary']
    p = Provider()
    second = build_context(req, MapContextResolver(records), p)
    assert second['status'] == 'ready'
    assert not p.calls
    records[0]['content']['text'] += '修改'
    records[0]['contentHash'] = hash_content(records[0]['content'])
    out = build_context(req, MapContextResolver(records), p)
    assert out['failure']['code'] == 'SUMMARY_SOURCE_CHANGED'


def test_deadline_before_summary():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    p = Provider()
    out = build_context(req, MapContextResolver(records), p, deadline=1, clock=lambda: 2)
    assert out['failure']['code'] == 'DEADLINE_EXCEEDED'
    assert not p.calls


def test_budget_is_conservative_for_chinese_and_code():
    assert estimate_tokens('中文') >= 2
    assert estimate_tokens('dp[j] = max(dp[j], dp[j-w]+v)') >= 10


def test_schema_contracts_validate_saved_records():
    from focusmate.contracts.schema_validation import validate
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    out = build_context(req, MapContextResolver(records), Provider())
    validate('ContextBuildRequest', req)
    validate('ContextSnapshot', out['snapshot'])
    validate('ConversationSummary', out['summary'])


def test_reject_unknown_request_fields_before_call():
    req, records = fixture()
    req['hiddenTests'] = 'never send'
    p = Provider()
    assert build_context(req, MapContextResolver(records), p)['status'] == 'failed'
    assert not p.calls


def test_complete_turn_is_not_split_and_oversized_recent_is_reported():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    req['maxSummaryMessages'] = 3
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready'
    assert out['summary']['sourceRefs'] == ['m0','m1']


def test_non_contiguous_turn_id_is_rejected():
    req, records = fixture()
    records[4]['turnId'] = '0'
    out = build_context(req, MapContextResolver(records))
    assert out['failure']['code'] == 'INVALID_TURN_GROUP'


def test_summary_endpoint_has_its_own_input_budget_check():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 9000
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['failure']['code'] == 'SUMMARY_INPUT_TOO_LONG'
    assert not p.calls


def test_summary_expansion_is_not_retried():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    item = {'kind': 'hypothesis', 'text': '长' * 1200,
            'quotes': [{'ref': 'm0', 'text': '容量不超过 j。'}]}
    p = Provider({'items': [item] * 10})
    out = build_context(req, MapContextResolver(records), p)
    assert out['failure']['code'] == 'SUMMARY_STILL_TOO_LONG'
    assert len(p.calls) == 1


def test_source_content_reference_cannot_disagree_with_resolver_ref():
    req, records = fixture()
    records[0]['content']['ref'] = 'other'
    records[0]['contentHash'] = hash_content(records[0]['content'])
    out = build_context(req, MapContextResolver(records))
    assert out['failure']['code'] == 'SOURCE_REFERENCE_MISMATCH'


def test_source_and_dialogue_refs_are_deduplicated_in_snapshot():
    from focusmate.contracts.schema_validation import validate
    req, records = fixture()
    req['sourceRefs'] = ['m0']
    out = build_context(req, MapContextResolver(records))
    validate('ContextSnapshot', out['snapshot'])
    assert out['snapshot']['includedRefs'].count('m0') == 1


def test_late_summary_response_cannot_be_accepted():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    ticks = iter([0, 2])
    p = Provider()
    out = build_context(req, MapContextResolver(records), p, deadline=1, clock=lambda: next(ticks))
    assert out['failure']['code'] == 'DEADLINE_EXCEEDED'
    assert 'response' in out['summaryAudit']


def test_missing_source_and_missing_subgoal_fail_without_model():
    req, records = fixture()
    assert build_context(req, MapContextResolver(records[1:]))['failure']['code'] == 'SOURCE_NOT_FOUND'
    req['goalContext'] = {'scope': 'subgoal', 'goalId': 'g1'}
    assert build_context(req, MapContextResolver(records))['status'] == 'failed'


def test_summary_audit_reproduces_successful_and_failed_request():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    for p in (Provider(), Provider(ProviderError())):
        out = build_context(req, MapContextResolver(records), p)
        audit = out['summaryAudit']
        assert audit['systemPrompt'] == p.calls[0]['system_prompt']
        assert audit['userPrompt'] == p.calls[0]['user_prompt']
        assert audit['sourceHashes']['m0'] == records[0]['contentHash']
        assert audit['estimator'] == 'utf8-bytes-divided-by-two-v1'


def test_reuses_earlier_summary_when_new_turn_appended():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    first = build_context(req, MapContextResolver(records), Provider())
    req['previousSummary'] = first['summary']
    for i in (6,7):
        record = {'ref':f'm{i}','goalContext':GOAL,'kind':'dialogue','version':'1',
                  'sequence':i,'turnId':'3','content':{'ref':f'm{i}','text':'现在初始化呢？'}}
        record['contentHash'] = hash_content(record['content'])
        records.append(record)
        req['dialogueRefs'].append(record['ref'])
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready', out
    assert not p.calls
    assert out['summary']['contentHash'] == first['summary']['contentHash']
    assert [r['ref'] for r in out['snapshot']['modelPayload']['dialogue']] == ['m4','m5','m6','m7']


@pytest.mark.parametrize('bad', [None, {'content':'{}'}, ProviderResponse(None)])
def test_invalid_provider_response_returns_failure(bad):
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    class Bad:
        def complete(self, **kwargs):
            return bad
    out = build_context(req, MapContextResolver(records), Bad())
    assert out['failure']['code'] == 'SUMMARY_INVALID_RESPONSE'


@pytest.mark.parametrize('nested', [False, True])
def test_summary_receives_current_request_and_positive_output_allowance(nested):
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    question = {'ref':'question','text':'dp 初始化为零可以吗？'}
    if nested:
        req['mandatoryPayload']['materials'] = {'userRequest': question}
    else:
        req['mandatoryPayload']['userRequest'] = question
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready'
    body = json.loads(p.calls[0]['user_prompt'])
    assert body['currentUserRequest'] == question
    assert 0 < body['summaryBudget']['targetEstimatedTokens'] < req['budget']['contextWindowTokens']
    assert out['summary']['promptVersion'] == 't056-summary-v3'


@pytest.mark.parametrize('change_prompt', [False, True])
def test_cache_refreshes_once_when_question_or_prompt_changes(change_prompt):
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    req['mandatoryPayload']['userRequest'] = {'ref':'q','text':'初始化呢？'}
    first = build_context(req, MapContextResolver(records), Provider())
    previous = deepcopy(first['summary'])
    if change_prompt:
        previous['promptVersion'] = 't056-summary-v1'
        previous['contentHash'] = hash_content({k:v for k,v in previous.items() if k not in ('summaryId','createdAt','contentHash')})
    else:
        req['mandatoryPayload']['userRequest']['text'] = '更新顺序呢？'
    req['previousSummary'] = previous
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready', out
    assert len(p.calls) == 1
    assert out['summary']['focusHash'] == hash_content(req['mandatoryPayload']['userRequest'])
    assert out['supersededSummary'] == previous


def test_summary_can_use_larger_budget_than_compressed_analysis():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 9000
    req['summaryBudget'] = {'contextWindowTokens': 20000, 'reservedOutputTokens':1000, 'safetyMarginTokens':100}
    p = Provider()
    out = build_context(req, MapContextResolver(records), p)
    assert out['status'] == 'ready', out
    assert out['summaryAudit']['budget']['inputTokenLimit'] == 18900
    assert out['snapshot']['budget']['inputTokenLimit'] == 7900


def test_overflow_lists_specific_code_source_even_when_payload_is_nested():
    req, records = fixture()
    content={'snapshotRef':'code-v2','text':'x = 1\n' * 10000}
    req['mandatoryPayload']['materials']={'code':content}
    req['sourceRefs']=['code-v2']
    req['budget']['contextWindowTokens']=1000
    records.append({'ref':'code-v2','goalContext':GOAL,'kind':'code','version':'2',
                    'content':content,'contentHash':hash_content(content)})
    out=build_context(req,MapContextResolver(records))
    assert out['failure']['code']=='REQUIRED_MATERIAL_TOO_LONG'
    assert any(item['ref']=='code-v2' for item in out['failure']['materials'])


def test_model_quotes_generate_source_refs_without_duplicate_model_fields():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    value = {'items': [{'kind': 'userStatement', 'text': '容量不超过 j。',
        'quotes': [{'ref': 'm0', 'text': '容量不超过 j。'},
                   {'ref': 'm2', 'text': '容量不超过 j。'},
                   {'ref': 'm0', 'text': '容量不超过 j。'}]}]}
    provider = Provider(value)
    result = build_context(req, MapContextResolver(records), provider)
    assert result['status'] == 'ready', result
    assert result['summary']['items'][0]['sourceRefs'] == ['m0', 'm2']
    assert 'sourceRefs' not in json.loads(result['summaryAudit']['response'])['items'][0]
    schema = json.loads(provider.calls[0]['user_prompt'])['summarySchema']
    assert 'sourceRefs' not in schema['properties']['items']['items']['properties']
    assert 'sourceRefs' not in value['items'][0]


def test_provider_cannot_supply_derived_refs_even_if_they_match():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    p = Provider({'items': [{'kind': 'userStatement', 'text': 'x',
        'sourceRefs': ['m0'], 'quotes': [{'ref': 'm0', 'text': '容量不超过 j。'}]}]})
    assert build_context(req, MapContextResolver(records), p)['failure']['code'] == 'SUMMARY_INVALID'


def test_cached_summary_rejects_tampered_derived_refs_even_with_recomputed_hash():
    req, records = fixture()
    req['budget']['contextWindowTokens'] = 12000
    previous = build_context(req, MapContextResolver(records), Provider())['summary']
    previous['items'][0]['sourceRefs'] = ['m2']
    previous['contentHash'] = hash_content({k: v for k, v in previous.items()
        if k not in ('summaryId', 'createdAt', 'contentHash')})
    req['previousSummary'] = previous
    p = Provider()
    assert build_context(req, MapContextResolver(records), p)['failure']['code'] == 'SUMMARY_INVALID'
    assert not p.calls

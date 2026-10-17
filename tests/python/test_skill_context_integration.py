"""T056: the executor sends precisely the recorded bounded context."""
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
helpers = runpy.run_path(str(ROOT / 'tests/python/test_skill_execution.py'))


def test_each_actor_and_review_request_has_exact_snapshot():
    provider = helpers['Provider'](('fail', 'pass'))
    report = helpers['module']().execute_skill(helpers['request'](), provider)
    assert report['status'] == 'accepted'
    assert len(report['contextSnapshots']) == 4
    for snap, (sent, payload) in zip(report['contextSnapshots'], provider.calls):
        assert snap['modelPayload'] == payload
        assert snap['systemPrompt'] == sent['system_prompt']
        assert snap['goalContext'] == helpers['request']()['goalContext']


def test_mandatory_code_too_large_never_calls_model():
    req = helpers['request']()
    req['inputs']['code']['text'] = 'x = 1\n' * 10000
    provider = helpers['Provider']()
    report = helpers['module']().execute_skill(req, provider, context_options={
        'budget': {'contextWindowTokens': 1000, 'reservedOutputTokens': 100, 'safetyMarginTokens': 50}})
    assert report['status'] == 'rejected'
    assert report['failure']['reason'] == 'CONTEXT_BUILD_FAILED'
    assert report['contextFailure']
    assert not provider.calls


def test_snapshot_failure_not_candidate_error_and_no_goal_result():
    provider = helpers['Provider']()
    report = helpers['module']().execute_skill(helpers['request'](), provider, context_options={
        'budget': {'contextWindowTokens': -1, 'reservedOutputTokens': 100, 'safetyMarginTokens': 0}})
    assert report['failure']['reason'] == 'CONTEXT_BUILD_FAILED'
    assert 'result' not in report.get('invocation', {})
    assert not provider.calls


def test_compacted_actor_can_reach_original_evidence_review():
    from focusmate.core.context_builder import estimate_tokens
    req = helpers['request']()
    req['dialogue'] = [{'ref': f'd{i}', 'role': 'user' if i % 2 == 0 else 'assistant',
        'text': '请检查访问记录。' + '历史讨论。' * 600} for i in range(8)]
    payload, _, _ = helpers['module']()._prepare(req, ROOT / 'skills')
    limit = estimate_tokens(helpers['module']().ACTOR_PROMPT) + estimate_tokens(payload)
    class WithSummary(helpers['Provider']):
        def complete(self, **kwargs):
            body = json.loads(kwargs['user_prompt'])
            if 'summarySchema' in body:
                return helpers['module']().ProviderResponse(json.dumps({'items': [{
                    'kind': 'userStatement', 'text': '用户要求检查访问记录。',
                    'quotes': [{'ref': 'd0', 'text': '请检查访问记录。'}]}]}), 'fake')
            return super().complete(**kwargs)
    provider = WithSummary()
    report = helpers['module']().execute_skill(req, provider, context_options={
        'budget': {'contextWindowTokens': int(limit * .85) + 1000,
                   'reservedOutputTokens': 1000, 'safetyMarginTokens': 0},
        'reviewBudget': {'contextWindowTokens': limit * 3,
                         'reservedOutputTokens': 1000, 'safetyMarginTokens': 0},
        'recentMessageCount': 2, 'maxSummaryMessages': 4})
    assert report['status'] == 'accepted', report.get('contextFailure')
    assert 'dialogueSummary' in provider.calls[0][1]
    assert provider.calls[1][1]['dialogue'] == req['dialogue']
    assert len(report['summaryRecords']) == 1
    assert report['conversationSummary']['sourceRefs']
    assert report['contextSnapshots'][0]['contextSnapshotId'] in report['invocation']['inputRefs']

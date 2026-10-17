import json
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from focusmate.contracts.skill_execution import ProviderResponse, ProviderError
from focusmate.core.adaptation_interpretation import interpret_effect


def request():
    return {'kind': 'feedback', 'sourceRef': 'm1',
            'materials': [{'ref': 'm1', 'text': 'A1这个提示没帮助'}],
            'actions': [{'actionId': 'A1', 'content': '检查访问标记',
                         'goalContext': {'goalId': 'g', 'scope': 'subgoal', 'subgoalId': 'sg'},
                         'codeSnapshotRef': 'c1',
                         'verificationPlan': {'requiredChecks': [{'capability': 'visitOnce', 'parameters': {}}]}}]}


def answer():
    return {'actionId': 'A1', 'classification': 'notHelpful',
            'evidence': [{'ref': 'm1', 'quote': 'A1这个提示没帮助'}], 'reason': '明确评价A1'}


class Provider:
    def __init__(self, value=None, error=None):
        self.value = answer() if value is None else value
        self.error = error
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ProviderResponse(json.dumps(self.value, ensure_ascii=False), 'fake')


def test_feedback_keeps_quote_and_sends_no_expected_labels():
    p = Provider()
    report = interpret_effect(request(), p)
    assert report['status'] == 'interpreted'
    assert report['interpretation'] == answer()
    assert len(p.calls) == 1
    assert 'expected' not in json.loads(p.calls[0]['user_prompt'])


def test_invalid_input_never_calls_model():
    r = request(); r['expected'] = answer()
    p = Provider()
    assert interpret_effect(r, p)['failure']['stage'] == 'input'
    assert not p.calls


def test_unknown_action_or_invented_quote_rejected():
    for key, value in [('actionId', 'A2'), ('evidence', [{'ref': 'm1', 'quote': '这很有帮助'}])]:
        a = answer(); a[key] = value
        report = interpret_effect(request(), Provider(a))
        assert report['status'] == 'rejected'
        assert 'interpretation' not in report


def test_ambiguous_feedback_is_not_negative():
    r = request(); r['materials'][0]['text'] = '还是不行'
    a = {'actionId': None, 'classification': 'uncertain',
         'evidence': [{'ref': 'm1', 'quote': '还是不行'}], 'reason': '可能指运行失败'}
    assert interpret_effect(r, Provider(a))['interpretation']['classification'] == 'uncertain'


def test_provider_failure_not_negative():
    result = interpret_effect(request(), Provider(error=ProviderError('PROVIDER_UNAVAILABLE')))
    assert result['status'] == 'rejected'
    assert 'interpretation' not in result


def test_deadline_expired_does_not_call_provider():
    p=Provider()
    result=interpret_effect(request(),p,deadline=0)
    assert result['failure']['reason']=='DEADLINE_EXCEEDED'
    assert not p.calls


def test_duplicate_material_refs_rejected_before_call():
    r=request(); r['materials'].append(deepcopy(r['materials'][0])); p=Provider()
    assert interpret_effect(r,p)['failure']['stage']=='input'
    assert not p.calls


def test_verification_support_checks_type_status_goal_and_version():
    r = request(); r['kind'] = 'verification'
    r['verifierResult'] = {'verifierResultId': 'v1', 'schemaVersion': '1.0.0',
        'createdAt': '2026-09-15T10:00:00Z', 'goalContext': deepcopy(r['actions'][0]['goalContext']),
        'verifierType': 'process', 'status': 'passed', 'evidenceRefs': ['m1'],
        'reason': '访问检查通过', 'inputVersionRefs': {'codeSnapshotRef': 'c1'}}
    r['materials'][0]['text']=json.dumps(r['verifierResult'],ensure_ascii=False)
    a=answer(); a['evidence']=[{'ref':'m1','quote':'访问检查通过'}]
    a['classification'] = 'supports'
    assert interpret_effect(r, Provider(a))['status'] == 'interpreted'
    for changes in [{'status': 'failed'}, {'inputVersionRefs': {'codeSnapshotRef': 'old'}},
                    {'goalContext': {'goalId': 'other', 'scope': 'subgoal', 'subgoalId': 'sg'}},
                    {'verifierType': 'finalOj', 'ojResultId': 'oj1',
                     'goalContext': {'goalId': 'g', 'scope': 'goal'}}]:
        bad = deepcopy(r); bad['verifierResult'].update(changes)
        bad['materials'][0]['text']=json.dumps(bad['verifierResult'],ensure_ascii=False)
        report = interpret_effect(bad, Provider(a))
        assert report['status'] == 'rejected'
        assert 'interpretation' not in report

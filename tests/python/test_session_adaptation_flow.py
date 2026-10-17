import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from focusmate.contracts.skill_execution import ProviderResponse
from focusmate.core.session_adaptation_flow import run_adaptation


def bundle():
    context = {'goalId': 'g', 'scope': 'subgoal', 'subgoalId': 'sg'}
    episode = {'episodeId': 'e', 'sessionId': 's', 'goalContext': context,
        'skillRef': {'skillId': 'tree-and-graph', 'skillVersion': 6, 'skillHash': 'sha256:'+'a'*64},
        'invocationId': 'i', 'sectionIds': ['visit-control'], 'actionId': 'A1',
        'actionType': 'hint', 'hintLevel': 1, 'triggerRefs': ['t'], 'codeSnapshotRef': 'c'}
    def source(ref, kind, **kwargs):
        return {'sourceId': ref, 'kind': kind, 'sessionId': 's', 'goalContext': context, **kwargs}
    sources = [source('t','trigger',evidenceType='hypothesis'),source('c','code'),
        source('i','invocation',skillRef=episode['skillRef'],sectionIds=['visit-control'],codeSnapshotRef='c'),
        source('A1','action',invocationId='i',actionType='hint',hintLevel=1,codeSnapshotRef='c')]
    action = {'actionId':'A1', 'goalContext':context,'content':'检查访问记录',
              'codeSnapshotRef':'c', 'verificationPlan':{'requiredChecks':[{'capability':'visit'}]}}
    request = {'kind':'feedback','sourceRef':'f','materials':[{'ref':'f','text':'A1没帮助'}], 'actions':[deepcopy(action)]}
    return {'schemaVersion':'1.0.0','simulatedMaterials':True,
        'episodes':[{'episode':episode,'sources':sources,'action':action}],
        'steps':[{'eventId':'event1','request':request}]}


class Provider:
    calls=0
    def complete(self, **kwargs):
        self.calls += 1
        return ProviderResponse(json.dumps({'actionId':'A1','classification':'notHelpful',
            'evidence':[{'ref':'f','quote':'A1没帮助'}], 'reason':'用户明确拒绝这条帮助'}))


def test_real_flow_updates_once_and_replays_without_model_call():
    p=Provider(); b=bundle()
    result=run_adaptation(b,p)
    assert result['ledger']['contributions']['e']==-1
    assert p.calls==1
    replay=run_adaptation(b,p,previous=result)
    assert replay['ledger']['contributions']['e']==-1
    assert p.calls==1


def test_changed_event_body_and_action_cannot_be_silently_reused():
    p=Provider(); b=bundle(); first=run_adaptation(b,p)
    b['steps'][0]['request']['materials'][0]['text']='changed'
    result=run_adaptation(b,p,previous=first)
    assert result['steps'][-1]['status']=='rejected'
    assert p.calls==1
    b=bundle(); b['steps'][0]['request']['actions'][0]['content']='different assistance'
    result=run_adaptation(b,p)
    assert result['steps'][0]['status']=='rejected'
    assert p.calls==1


def test_invalid_later_step_keeps_previous_success_and_no_more_requests():
    b=bundle(); bad=deepcopy(b['steps'][0]); bad['eventId']='event2'
    bad['codeSources']=[{'sourceId':'bad'}]; b['steps'].append(bad)
    p=Provider(); result=run_adaptation(b,p)
    assert result['ledger']['contributions']['e']==-1
    assert result['steps'][-1]['status']=='rejected'
    assert p.calls==1


def test_missing_later_request_still_returns_prior_snapshot():
    b=bundle(); b['steps'].append({'eventId':'broken'})
    p=Provider(); result=run_adaptation(b,p)
    assert result['ledger']['contributions']['e']==-1
    assert result['steps'][-1]['status']=='rejected'
    assert p.calls==1


def test_other_goal_verifier_cannot_retract_existing_support():
    b=bundle(); b['steps']=[]
    context=b['episodes'][0]['episode']['goalContext']
    def step(event_id, goal, classification, supersedes=None):
        verifier={'verifierResultId':event_id,'schemaVersion':'1.0.0',
            'createdAt':'2026-09-15T10:00:00Z','goalContext':goal,
            'verifierType':'process','status':'passed','evidenceRefs':['p'],
            'reason':'访问检查通过','inputVersionRefs':{'codeSnapshotRef':'c'}}
        request={'kind':'verification','sourceRef':'v',
            'materials':[{'ref':'v','text':json.dumps(verifier,ensure_ascii=False)},
                         {'ref':'p','text':'访问检查通过'}],
            'actions':[b['episodes'][0]['action']], 'verifierResult':verifier}
        s={'eventId':event_id,'request':request}
        if supersedes:s['supersedes']=supersedes
        return s, {'actionId':'A1','classification':classification,'reason':'核对依据',
            'evidence':[{'ref':'v','quote':'访问检查通过'},{'ref':'p','quote':'访问检查通过'}]}
    first,a1=step('v1',context,'supports')
    other,a2=step('v2',{'goalId':'other','scope':'subgoal','subgoalId':'sg'},'notSupported','v1')
    class P:
        values=iter([a1,a2])
        def complete(self,**kwargs):return ProviderResponse(json.dumps(next(self.values),ensure_ascii=False))
    b['steps']=[first,other]; result=run_adaptation(b,P())
    assert result['ledger']['contributions']['e']==1
    assert result['steps'][-1]['status']=='rejected'

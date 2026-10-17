from copy import deepcopy
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from focusmate.core.session_adaptation import SessionAdaptationLedger


def example(episode_id='e', action_id='a'):
    episode = dict(episodeId=episode_id, sessionId='s', goalContext={'goalId':'g','scope':'subgoal','subgoalId':'sg'},
        skillRef={'skillId':'graph','skillVersion':1,'skillHash':'sha256:'+'a'*64}, invocationId='i'+action_id,
        sectionIds=['b','a'], actionId=action_id, actionType='hint', hintLevel=1, triggerRefs=['t'], codeSnapshotRef='c')
    def source(ref, kind, **fields):
        return dict(sourceId=ref, kind=kind, sessionId='s', goalContext=deepcopy(episode['goalContext']), **fields)
    sources = [source('t','trigger',evidenceType='hypothesis'), source('c','code'),
        source('i'+action_id,'invocation',skillRef=episode['skillRef'],sectionIds=['a','b'],codeSnapshotRef='c'),
        source(action_id,'action',invocationId='i'+action_id,actionType='hint',hintLevel=1,codeSnapshotRef='c')]
    return episode, sources, source


def signal(event_id='n', kind='feedback', status='notHelpful', ref='f', **extra):
    return dict(eventId=event_id,episodeId='e',kind=kind,status=status,sourceRef=ref,evidenceRefs=[ref],reason='explicit source',**extra)


def test_repeated_negative_late_support_and_exact_replay():
    e, sources, source = example()
    ledger = SessionAdaptationLedger()
    assert ledger.register_episode(e,sources=sources)
    negative = signal()
    fs = source('f','feedback',actionId='a',status='notHelpful')
    assert ledger.record_signal(negative,sources=[fs])
    assert ledger.contribution('e') == -1
    assert not ledger.record_signal(negative,sources=[fs])
    ledger.record_signal(signal('n2'),sources=[])
    assert ledger.contribution('e') == -1
    ps = source('p','process',actionId='a',invocationId='ia',codeSnapshotRef='c')
    vs = source('v','verifier',actionId='a',invocationId='ia',codeSnapshotRef='c',status='passed',processEvidenceRefs=['p'])
    positive=signal('v1','verification','supports','v',verifierRef='v')
    positive['evidenceRefs']=['p']
    ledger.record_signal(positive,sources=[ps,vs])
    assert ledger.contribution('e') == 0
    snapshot=ledger.snapshot()
    assert SessionAdaptationLedger(snapshot).snapshot() == snapshot
    negative['reason']='changed'
    with pytest.raises(ValueError): ledger.record_signal(negative,sources=[])


def test_correction_history_conflicts_and_isolation():
    e,sources,source=example(); ledger=SessionAdaptationLedger()
    ledger.register_episode(e,sources=sources)
    ledger.record_signal(signal(),sources=[source('f','feedback',actionId='a',status='notHelpful')])
    ledger.record_signal(signal('fix',status='misunderstood',ref='fixsrc',supersedes='n'),sources=[source('fixsrc','feedback',actionId='a',status='misunderstood')])
    assert ledger.contribution('e') == 0
    assert len(ledger.snapshot()['events']) == 2
    with pytest.raises(ValueError): ledger.record_signal(signal('conflict',supersedes='n'),sources=[])
    with pytest.raises(ValueError): ledger.record_signal(signal('self',supersedes='self'),sources=[])
    bad=source('wrong','feedback',actionId='a',status='notHelpful'); bad['sessionId']='other'
    ledger.record_signal(signal('wrong',ref='wrong'),sources=[bad])
    assert ledger.contribution('e') == 0


@pytest.mark.parametrize('change',[{'status':'failed'},{'codeSnapshotRef':'old'},{'processEvidenceRefs':[]},{'actionId':'other'}])
def test_unsupported_verifier_never_awards(change):
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    p=source('p','process',actionId='a',invocationId='ia',codeSnapshotRef='c')
    v=source('v','verifier',actionId='a',invocationId='ia',codeSnapshotRef='c',status='passed',processEvidenceRefs=['p']); v.update(change)
    sig=signal('v1','verification','supports','v',verifierRef='v'); sig['evidenceRefs']=['p']
    ledger.record_signal(sig,sources=[p,v,source('old','code')]); assert ledger.contribution('e') == 0


def test_clamp_ranking_and_immutable_inputs():
    ledger=SessionAdaptationLedger()
    for i in range(4):
        e,sources,source=example('e'+str(i),'a'+str(i)); ledger.register_episode(e,sources=sources)
        sig=signal('n'+str(i),ref='f'+str(i)); sig['episodeId']=e['episodeId']
        ledger.record_signal(sig,sources=[source('f'+str(i),'feedback',actionId=e['actionId'],status='notHelpful')])
    assert ledger.adjustment(e)==-2
    assert ledger.snapshot()['adjustments'][0]['rawSum']==-4
    neutral=deepcopy(e); neutral['hintLevel']=2
    assert ledger.rank_applicable([e,neutral,e])==[neutral,e,e]
    e['sectionIds'].clear()
    assert ledger.snapshot()['episodes'][0]['sectionIds']==['b','a']


def test_missing_episode_link_rejected_and_snapshot_tampering_recomputed():
    e,sources,_=example(); ledger=SessionAdaptationLedger()
    with pytest.raises(ValueError): ledger.register_episode(e,sources=sources[:-1])
    ledger.register_episode(e,sources=sources)
    duplicate=deepcopy(e); duplicate['episodeId']='another'
    with pytest.raises(ValueError): ledger.register_episode(duplicate,sources=[])
    snapshot=ledger.snapshot(); snapshot['contributions']={'e':999}
    assert SessionAdaptationLedger(snapshot).contribution('e')==0


@pytest.mark.parametrize('linked',[True,False])
def test_verified_revision_requires_explicit_code_ancestry(linked):
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    code=source('new','code',**({'previousCodeSnapshotRef':'c'} if linked else {}))
    p=source('p','process',actionId='a',invocationId='ia',codeSnapshotRef='new')
    v=source('v','verifier',actionId='a',invocationId='ia',codeSnapshotRef='new',status='passed',processEvidenceRefs=['p'])
    sig=signal('v1','verification','supports','v',verifierRef='v'); sig['evidenceRefs']=['p']
    ledger.record_signal(sig,sources=[code,p,v])
    assert ledger.contribution('e') == (1 if linked else 0)


def test_positive_can_be_explicitly_retracted_without_mutating_history():
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    p=source('p','process',actionId='a',invocationId='ia',codeSnapshotRef='c')
    v=source('v','verifier',actionId='a',invocationId='ia',codeSnapshotRef='c',status='passed',processEvidenceRefs=['p'])
    sig=signal('v1','verification','supports','v',verifierRef='v'); sig['evidenceRefs']=['p']
    ledger.record_signal(sig,sources=[p,v]); assert ledger.contribution('e') == 1
    ledger.record_signal(signal('v2','verification','notSupported','v',supersedes='v1'),sources=[])
    assert ledger.contribution('e')==0
    assert ledger.snapshot()['events'][0] == sig


@pytest.mark.parametrize('field,value', [('sessionId','other'),('goalContext',{'goalId':'other','scope':'goal'}),
    ('hintLevel',2),('actionType','explain'),('sectionIds',['a']),('skillRef',{'skillId':'other','skillVersion':1,'skillHash':'sha256:'+'a'*64})])
def test_every_combination_dimension_is_isolated(field,value):
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    ledger.record_signal(signal(),sources=[source('f','feedback',actionId='a',status='notHelpful')])
    other=deepcopy(e); other[field]=value
    assert ledger.adjustment(other)==0
    assert ledger.adjustment(e)==-1


def test_source_identity_conflict_is_atomic():
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    original=ledger.snapshot()
    with pytest.raises(ValueError):
        ledger.record_signal(signal(),sources=[source('new','code'),source('c','code',previousCodeSnapshotRef='other')])
    assert ledger.snapshot()==original


def test_correction_with_unrelated_source_cannot_erase_negative():
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    ledger.record_signal(signal(),sources=[source('f','feedback',actionId='a',status='notHelpful')])
    with pytest.raises(ValueError):
        ledger.record_signal(signal('fix',status='misunderstood',ref='other',supersedes='n'),
            sources=[source('other','feedback',actionId='other-action',status='misunderstood')])
    assert ledger.contribution('e')==-1


def test_missing_sources_rejected_and_trace_replay_is_deterministic():
    e,sources,source=example(); ledger=SessionAdaptationLedger(); ledger.register_episode(e,sources=sources)
    with pytest.raises(ValueError,match='missing signal source'):
        ledger.record_signal(signal(),sources=[])
    assert ledger.snapshot()['events']==[]
    ledger.record_signal(signal(),sources=[source('f','feedback',actionId='a',status='notHelpful')])
    ledger.record_signal(signal('n2'),sources=[])
    trace=ledger.snapshot()['trace']
    assert [(x['beforeContribution'],x['afterContribution']) for x in trace]==[(0,-1),(-1,-1)]
    assert trace[0]['resolution']=='negative'
    assert SessionAdaptationLedger(ledger.snapshot()).snapshot()['trace']==trace

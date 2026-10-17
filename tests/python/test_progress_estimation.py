from copy import deepcopy
import json
import pytest

from focusmate.core.context_builder import MapContextResolver, hash_content
from focusmate.contracts.skill_execution import ProviderResponse, ProviderError
from focusmate.core.progress_estimation import estimate_progress


def fixture():
    # Contract is a pinned requirement, never evidence of user completion.
    context = {'goalId': 'g1', 'scope': 'goal'}
    contract = {'goalId':'g1','schemaVersion':'1.0.0','version':1,
        'createdAt':'2026-09-19T00:00:00+00:00','problemRef':{'problemId':'p1','problemVersion':1},
        'objective':'完成0/1背包，每件物品最多使用一次。','subgoals':[{
            'subgoalId':'s1','stage':'coreImplementation','objective':'每个物品最多更新一次',
            'dependsOn':[],'acceptableEvidence':['codeStructureCheck','targetedRunResult','evaluatedUserExplanation']}],
        'navigationPolicy':{'mode':'adaptive','stageOrder':'defaultOnly','allowSkip':True,
            'allowRevisit':True,'offRouteHandling':'temporaryBranch','stalledRouteFallback':'latestVerifiedSubgoal',
            'processStagesRequiredForCompletion':False},
        'finalCriterion':{'verifierType':'finalOj','requiredVerdict':'AC'},
        'coachPolicy':{'completeSolutionAllowed':False},'contractStatus':'frozen'}
    def record(ref, kind, content, version=1):
        return {'ref':ref,'kind':kind,'version':version,'goalContext':context,
                'content':content,'contentHash':hash_content(content),'origin':'developer-fixture'}
    records=[record('g01','goal',contract),
        record('c01','code',{'snapshotRef':'c01','language':'python3','text':'for j in range(w, n+1):\n    dp[j] = max(dp[j], dp[j-w]+v)\n'}),
        record('c02','code',{'snapshotRef':'c02','language':'python3','text':'for j in range(n, w-1, -1):\n    dp[j] = max(dp[j], dp[j-w]+v)\n'},2),
        record('u01','dialogue',{'ref':'u01','text':'我改了方向，但仍不理解为什么不能正序。','role':'user','codeSnapshotRef':'c02'})]
    records[-1].update(sequence=1,turnId='t1')
    req={'schemaVersion':'1.0.0','goalContext':context,'problemRef':contract['problemRef'],
         'contractVersion':1,'contractRef':'g01','currentCodeRef':'c02','baselineRefs':['c01'],
         'sources':[{'ref':r['ref'],'kind':r['kind'],'version':r['version'],'contentHash':r['contentHash'],
                     'sourceType':'external','sourceId':r['ref']} for r in records],
         'budget':{'contextWindowTokens':16000,'reservedOutputTokens':4096,'safetyMarginTokens':1000}}
    return req,records


def finding(kind='progressing', refs=None):
    return {'kind':kind,'subject':'容量更新方向','reason':'当前方向改为倒序，与每个物品最多使用一次的要求相关。',
            'evidenceRefs':refs or ['g01','c01','c02'],'uncertainties':['尚未运行，不证明完整实现正确。']}


class Provider:
    public_config={'model':'fake','maxTokens':1024}
    def __init__(self, value=None, error=None):
        self.value=value or {'findings':[finding()]}; self.error=error; self.calls=[]
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise ProviderError(self.error)
        return ProviderResponse(json.dumps(self.value,ensure_ascii=False),'fake')


def run(req=None, records=None, provider=None):
    if req is None:req,records=fixture()
    provider=provider or Provider()
    return estimate_progress(req,MapContextResolver(records),provider),provider


def repin(req,records):
    for s,r in zip(req['sources'],records):
        r['contentHash']=hash_content(r['content']);s['contentHash']=r['contentHash']


@pytest.mark.parametrize('fault',['duplicate','future_baseline','backend_log','secret'])
def test_extra_input_boundaries(fault):
    req,records=fixture()
    if fault=='duplicate':req['sources'].append(deepcopy(req['sources'][0]))
    if fault=='future_baseline':records[1]['version']=3;req['sources'][1]['version']=3
    if fault=='backend_log':records[-1]['content']['backendLogs']='private'
    if fault=='secret':records[-1]['content']['apiKey']='private'
    if fault!='duplicate':repin(req,records)
    report,p=run(req,records)
    assert report['status']=='failed' and not p.calls


def test_old_run_is_not_current_verification_material():
    req,records=fixture()
    r={'ref':'r01','kind':'runEvidence','version':1,'goalContext':req['goalContext'],
       'content':{'evidenceType':'targetedRunResult','codeSnapshotRef':'c01','completed':True,'outcome':'passed'}}
    r['contentHash']=hash_content(r['content']);records.append(r)
    req['sources'].append({'ref':'r01','kind':'runEvidence','version':1,'contentHash':r['contentHash'],'sourceType':'external','sourceId':'r01'})
    candidate={**finding('verification_candidate',['r01']),'verification':{'criterionId':'s1','evidenceType':'targetedRunResult','materialRefs':['r01']}}
    report,_=run(req,records,Provider({'findings':[candidate]}))
    assert report['failure']['reason']=='OLD_VERIFICATION_EVIDENCE'


def test_deadline_before_request_does_not_call_provider():
    req,records=fixture();p=Provider();times=iter([0,121,121])
    report=estimate_progress(req,MapContextResolver(records),p,clock=lambda:next(times))
    assert report['failure']['reason']=='DEADLINE_EXCEEDED' and not p.calls


@pytest.mark.parametrize('fault',['subgoal','contract_version','session','run_request','message_turn'])
def test_scope_and_evidence_boundaries(fault):
    req,records=fixture()
    if fault=='subgoal':
        req['goalContext']={'goalId':'g1','scope':'subgoal','subgoalId':'missing'}
        for r in records:r['goalContext']=req['goalContext']
    if fault=='contract_version':req['contractVersion']=2
    if fault=='session':req['sessionRef']='now';records[-1]['sessionRef']='other'
    if fault=='run_request':
        records[-1]['kind']='runEvidence';req['sources'][-1]['kind']='runEvidence'
        records[-1]['content']={'codeSnapshotRef':'c02','evidenceType':'runRequested'};repin(req,records)
    if fault=='message_turn':records[-1]['turnId']=''
    report,p=run(req,records)
    assert report['status']=='failed' and not p.calls


def test_empty_provider_model_is_reported_not_raised():
    class Bad(Provider):
        def complete(self,**kwargs):return ProviderResponse('{}','')
    report,_=run(provider=Bad())
    assert report['status']=='failed' and report['failure']['stage']=='provider'


def test_progress_and_difficulty_coexist_with_program_metadata():
    p=Provider({'findings':[finding(),{**finding('blocked_candidate',['u01']),
        'reason':'用户仍不理解更新方向。'}]})
    report,_=run(provider=p)
    assert report['status']=='estimated' and report['verified'] is False
    assert report['evidenceRefs']==['g01','c01','c02','u01']
    assert len(p.calls)==1
    assert report['contextSnapshot']['purpose']=='goalProgressEstimation'


@pytest.mark.parametrize('fault',['hash','version','scope','dialogue_ref','dialogue_order','missing'])
def test_bad_materials_stop_before_model(fault):
    req,records=fixture();r=records[-1]
    if fault=='hash': r['contentHash']='sha256:'+'0'*64
    if fault=='version':r['version']=True
    if fault=='scope':r['goalContext']={'goalId':'other','scope':'goal'}
    if fault=='dialogue_ref':
        r['content']['ref']='other';r['contentHash']=hash_content(r['content']);req['sources'][-1]['contentHash']=r['contentHash']
    if fault=='dialogue_order':r.pop('sequence')
    if fault=='missing':records.pop()
    report,p=run(req,records)
    assert report['status']=='failed' and report['failure']['stage']=='input' and not p.calls


def test_missing_optional_baseline_is_unknown_not_failed():
    req,records=fixture();req['baselineRefs']=[];req['sources'].pop(1);records.pop(1)
    p=Provider({'findings':[{**finding('unknown',['c02']),'reason':'缺少比较起点。'}]})
    report,_=run(req,records,p)
    assert report['status']=='estimated'
    assert 'BASELINE_NOT_PROVIDED' in report['materialUnknowns']


@pytest.mark.parametrize('value',[
    {'findings':[finding(),finding('no_material_change')]},
    {'findings':[finding(refs=['invented'])]},
    {'findings':['progressing']},
    {'findings':[dict(finding(),goalCompleted=True)]},
    {'findings':[dict(finding('verification_candidate'),verification={'criterionId':'missing','evidenceType':'runResult','materialRefs':['c02']})]},
])
def test_invalid_candidate_is_not_accepted(value):
    report,p=run(provider=Provider(value))
    assert report['status']=='failed' and report['failure']['stage']=='output'
    assert 'result' not in report and len(p.calls)==1
    assert report['response']['raw']


def test_explanation_is_only_a_candidate_not_a_verified_explanation():
    f=finding('verification_candidate',['g01','u01'])
    f['verification']={'criterionId':'s1','evidenceType':'evaluatedUserExplanation','materialRefs':['u01']}
    report,_=run(provider=Provider({'findings':[f]}))
    assert report['status']=='estimated' and report['verified'] is False


def test_no_baseline_cannot_be_claimed_no_material_change():
    req,records=fixture();req['baselineRefs']=[]
    report,_=run(req,records,Provider({'findings':[finding('no_material_change')]}))
    assert report['failure']['reason']=='COMPARISON_BASELINE_REQUIRED'


def test_provider_failure_and_budget_do_not_retry_in_core():
    p=Provider(error='PROVIDER_UNAVAILABLE');report,_=run(provider=p)
    assert report['status']=='failed' and len(p.calls)==1
    req,records=fixture();req['budget']['contextWindowTokens']=4500
    report,p=run(req,records)
    assert report['status']=='failed' and not p.calls


def test_input_is_not_modified_and_no_expected_data_sent():
    req,records=fixture();before=deepcopy((req,records));report,p=run(req,records)
    assert (req,records)==before
    assert 'expectedVerdict' not in p.calls[0]['user_prompt']
    assert report['status']=='estimated'

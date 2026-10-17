from copy import deepcopy
import pytest
from test_progress_estimation import fixture
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.diagnosis import prepare_diagnosis, validate_diagnosis


def inputs():
    req, records = fixture()
    req.update(userRequestRef='u01', dialogueRefs=[], historyDiagnosisRefs=[])
    return req, records


def candidate():
    return {'questions':[{'questionKey':'q1','question':'为何不能正序更新',
        'evidenceRefs':['u01','c02'], 'subgoals':[{'subgoalId':'s1','reason':'物品使用次数','evidenceRefs':['g01','u01']}],
        'findings':[{'text':'用户已改方向，但还在询问理由。','evidenceRefs':['u01','c02']}],
        'unknowns':[{'text':'缺少外层物品循环。','evidenceRefs':['c02']}],
        'history':{'relation':'unknown','previousQuestionRefs':[],'reason':'没有历史诊断','evidenceRefs':['u01']},
        'dependsOn':[]}], 'contractConcerns':[]}


def test_valid_unknown_diagnosis_is_not_failure():
    req,records=inputs(); bundle=prepare_diagnosis(req,MapContextResolver(records))
    report=validate_diagnosis(candidate(),bundle)
    assert report['status']=='diagnosed' and report['handoff']=='action_planning_not_integrated'
    assert report['questions'][0]['unknowns']
    assert report['semanticStatus']=='notVerified'


@pytest.mark.parametrize('fault',['hash','missing','scope'])
def test_bad_inputs_rejected(fault):
    req,records=inputs()
    if fault=='hash': records[-1]['content']['text']='被换过'
    if fault=='missing':records.pop()
    if fault=='scope':records[-1]['goalContext']={'goalId':'other','scope':'goal'}
    with pytest.raises(ValueError):prepare_diagnosis(req,MapContextResolver(records))


@pytest.mark.parametrize('fault',['ref','subgoal','cycle','history'])
def test_candidate_associations_rejected(fault):
    req,records=inputs();bundle=prepare_diagnosis(req,MapContextResolver(records));c=candidate()
    if fault=='ref':c['questions'][0]['findings'][0]['evidenceRefs']=['invented']
    if fault=='subgoal':c['questions'][0]['subgoals'][0]['subgoalId']='invented'
    if fault=='cycle':c['questions'][0]['dependsOn']=['q1']
    if fault=='history':c['questions'][0]['history'].update(relation='same',previousQuestionRefs=['old:q1'])
    with pytest.raises(ValueError):validate_diagnosis(c,bundle)


def test_code_not_provided_is_valid_missing_material():
    req,records=inputs();req.pop('currentCodeRef');req['baselineRefs']=[]
    records=[r for r in records if r['kind']!='code'];records[-1]['content'].pop('codeSnapshotRef')
    from focusmate.core.context_builder import hash_content
    records[-1]['contentHash']=hash_content(records[-1]['content'])
    req['sources']=[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in records]
    bundle=prepare_diagnosis(req,MapContextResolver(records))
    assert 'CURRENT_CODE_NOT_PROVIDED' in bundle['missing']


@pytest.mark.parametrize('fault',['session','missing_snapshot','hash','valid_old_code'])
def test_history_snapshot_is_checked_without_rebinding_old_code(fault):
    from focusmate.core.context_builder import hash_content
    req,records=inputs();req['sessionRef']='current-session'
    oldreq,oldrecords=deepcopy(req),deepcopy(records)
    oldreq['sessionRef']='old-session' if fault=='session' else 'current-session'
    old=validate_diagnosis(candidate(),prepare_diagnosis(oldreq,MapContextResolver(oldrecords)))
    if fault=='missing_snapshot':old['inputSnapshot']={}
    if fault=='hash':old['inputSnapshot']['records'][-1]['content']['text']='changed'
    if fault=='valid_old_code':
        next(r for r in records if r['ref']=='c02')['version']=3
    content={'diagnosisReport':old}
    records.append({'ref':'previous','kind':'hypothesis','version':1,'goalContext':req['goalContext'],
                    'content':content,'contentHash':hash_content(content)})
    req['historyDiagnosisRefs']=['previous']
    req['sources']=[{k:r[k] for k in ('ref','kind','version','contentHash')} for r in records]
    if fault=='valid_old_code':
        assert prepare_diagnosis(req,MapContextResolver(records))['records'][-1]['content']['diagnosisReport']['inputSnapshot']['request']==oldreq
    else:
        with pytest.raises(ValueError):prepare_diagnosis(req,MapContextResolver(records))

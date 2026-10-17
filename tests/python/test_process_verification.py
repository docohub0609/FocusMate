from copy import deepcopy
import pytest
from test_progress_estimation import fixture
from focusmate.core.context_builder import hash_content, MapContextResolver


def inputs():
    old, records = fixture()
    records = [r for r in records if r['ref'] in ('g01', 'c02', 'u01')]
    contract = records[0]['content']
    records[0]['content'] = {'contract': contract, 'rubrics': [{'subgoalId': 's1',
        'checks': [{'check': '说明状态含义'}, {'check': '说明更新顺序为何不重复使用物品'}]}]}
    records[0]['contentHash'] = hash_content(records[0]['content'])
    req = {'schemaVersion': '1.0.0', 'goalContext': {'goalId': old['goalContext']['goalId'], 'scope': 'subgoal', 'subgoalId': 's1'},
        'problemRef': old['problemRef'], 'contractVersion': 1, 'contractRef': 'g01', 'actionId': 'a1',
        'currentCodeRef': 'c02', 'materialRefs': ['c02', 'u01'], 'sources': [
            {k:r[k] for k in ('ref','kind','version','contentHash')} for r in records], 'budget': old['budget']}
    action = {'actionId': 'a1', 'goalContext': old['goalContext'], 'contractVersion': 1,
        'subgoalIds': ['s1'], 'followUp': {'kind':'awaitEvidence','criterionIds':['s1'], 'checks':['codeStructure','userExplanation']}}
    return req, records, action


def prepared():
    from focusmate.core.process_verification import prepare_verification
    req, records, action = inputs()
    return prepare_verification(req, MapContextResolver(records), action)


def judgment(bundle, states=('passed','notReady')):
    return {'checks': [{'checkId': c['checkId'], 'status': status, 'reason': '依据当前材料检查此项。',
        'quotes':[{'ref':'u01','text':bundle['records'][-1]['content']['text']}],
        'evidenceType':'evaluatedUserExplanation', 'limitations': []} for c,status in zip(bundle['checks'],states)]}


def test_partial_pass_never_becomes_whole_subgoal_pass():
    from focusmate.core.process_verification import finish_verification
    b=prepared(); report=finish_verification(judgment(b), b)
    assert report['verifierResult']['status']=='notReady'
    assert report['checks'][0]['status']=='passed'
    assert report['verifierResult']['goalContext']['subgoalId']=='s1'


def test_invalid_code_proof_identifies_the_check_for_bounded_repair():
    from focusmate.core.process_verification import finish_verification
    b=prepared();b['subgoal']['acceptableEvidence'].append('codeStructureCheck')
    value=judgment(b)
    value['checks'][0]['evidenceType']='codeStructureCheck'
    with pytest.raises(ValueError,match='CURRENT_CODE_EVIDENCE_REQUIRED') as error:
        finish_verification(value,b)
    assert error.value.check_id==b['checks'][0]['checkId']


def test_structured_run_quotes_are_rendered_from_actual_facts():
    from focusmate.core.process_verification import presented_record, finish_verification
    b=prepared();b['subgoal']['acceptableEvidence'].append('publicSampleRunResult')
    record={'ref':'run','kind':'runEvidence','content':{'codeSnapshotRef':'c02',
        'completed':True,'sampleStatus':'passed','passedCount':2}}
    rendered=presented_record(record)['content']['evidenceText']
    value=judgment(b,('passed','passed'))
    for c in value['checks']:
        c.update(evidenceType='publicSampleRunResult',quotes=[{'ref':'run','text':rendered}])
    assert finish_verification(value,b,extra_records=[record])['verifierResult']['status']=='passed'
    record['content']['passedCount']=1
    with pytest.raises(ValueError,match='INVALID_EVIDENCE_QUOTE'):
        finish_verification(value,b,extra_records=[record])


def test_problem_sample_json_is_rendered_from_saved_facts_not_model_text():
    from focusmate.core.process_verification import presented_record,finish_verification
    b=prepared()
    record={'ref':'problem-samples','kind':'problemFact','content':{'examples':[{'input':'-8 -3 -5\n','output':'-3\n'}]}}
    rendered=presented_record(record)['content']['evidenceText']
    value=judgment(b,('notReady','notReady'))
    for check in value['checks']:check['quotes']=[{'ref':record['ref'],'text':rendered}]
    assert finish_verification(value,b,extra_records=[record])['verifierResult']['status']=='notReady'
    record['content']['examples'][0]['output']='0\n'
    record['content']['evidenceText']=rendered
    with pytest.raises(ValueError,match='INVALID_EVIDENCE_QUOTE'):
        finish_verification(value,b,extra_records=[record])


@pytest.mark.parametrize('kind',['verifiedFact','hypothesis','observation'])
def test_previous_opinion_cannot_prove_current_complexity(kind):
    from focusmate.core.process_verification import finish_verification
    b=prepared();b['subgoal']['acceptableEvidence'].append('complexityCheck')
    record={'ref':'old','kind':kind,'content':{'text':'旧版本为O(n)','codeVersion':1}}
    value=judgment(b,('passed','passed'))
    for c in value['checks']:c.update(evidenceType='complexityCheck',quotes=[{'ref':'old','text':'旧版本为O(n)'}])
    with pytest.raises(ValueError):finish_verification(value,b,extra_records=[record])


@pytest.mark.parametrize('states,want',[(('passed','passed'),'passed'),(('failed','notReady'),'failed'),
    (('inconclusive','notReady'),'inconclusive'),(('notReady','notReady'),'notReady')])
def test_aggregate_has_independent_literal_expectations(states,want):
    from focusmate.core.process_verification import finish_verification
    b=prepared();assert finish_verification(judgment(b,states),b)['verifierResult']['status']==want


@pytest.mark.parametrize('fault',['missing','duplicate','fake_ref','fake_quote','fake_check','unknown_type'])
def test_output_faults_cannot_be_accepted(fault):
    from focusmate.core.process_verification import finish_verification
    b=prepared();value=judgment(b)
    if fault=='missing':value['checks'].pop()
    if fault=='duplicate':value['checks'][1]=deepcopy(value['checks'][0])
    if fault=='fake_ref':value['checks'][0]['quotes'][0]['ref']='invented'
    if fault=='fake_quote':value['checks'][0]['quotes'][0]['text']='从未说过的话'
    if fault=='fake_check':value['checks'][0]['checkId']='invented'
    if fault=='unknown_type':value['checks'][0]['evidenceType']='confidence'
    with pytest.raises(ValueError):finish_verification(value,b)


@pytest.mark.parametrize('fault',['hash','scope','version','subgoal','missing_source','action'])
def test_invalid_source_or_action_is_rejected(fault):
    from focusmate.core.process_verification import prepare_verification
    req,records,action=inputs()
    if fault=='hash':records[-1]['content']['text']='changed'
    if fault=='scope':records[-1]['goalContext']['goalId']='other'
    if fault=='version':req['contractVersion']=9
    if fault=='subgoal':req['goalContext']['subgoalId']='other'
    if fault=='missing_source':records.pop()
    if fault=='action':action['followUp']['criterionIds']=[]
    with pytest.raises(ValueError):prepare_verification(req,MapContextResolver(records),action)


def test_explanation_without_code_is_valid_and_does_not_fabricate_code():
    from focusmate.core.process_verification import prepare_verification, finish_verification
    req,records,action=inputs();req['currentCodeRef']=None;req['materialRefs']=['u01']
    records=[r for r in records if r['ref']!='c02'];req['sources']=[s for s in req['sources'] if s['ref']!='c02']
    records[-1]['content'].pop('codeSnapshotRef',None);records[-1]['contentHash']=hash_content(records[-1]['content'])
    req['sources'][-1]['contentHash']=records[-1]['contentHash']
    b=prepare_verification(req,MapContextResolver(records),action)
    report=finish_verification(judgment(b),b)
    assert 'codeSnapshotRef' not in report['verifierResult']['inputVersionRefs']


@pytest.mark.parametrize('fault',['old','pending','missing_public_status','wrong_public_status'])
def test_run_evidence_must_represent_completed_current_check(fault):
    from focusmate.core.process_verification import finish_verification
    b=prepared();b['subgoal']['acceptableEvidence'].append('publicSampleRunResult')
    content={'codeSnapshotRef':'c02','completed':True,'sampleStatus':'passed','actualOutput':'3'}
    if fault=='old':content['codeSnapshotRef']='c01'
    if fault=='pending':content['completed']=False
    if fault=='missing_public_status':content.pop('sampleStatus')
    if fault=='wrong_public_status':content['sampleStatus']='failed'
    run={'ref':'run','kind':'runEvidence','content':content}
    value=judgment(b,('passed','passed'))
    for c in value['checks']:c.update(evidenceType='publicSampleRunResult',quotes=[{'ref':'run','text':'3'}])
    with pytest.raises(ValueError):finish_verification(value,b,extra_records=[run])


def test_unloaded_long_code_cannot_be_quoted_as_if_read():
    from focusmate.core.process_verification import finish_verification
    b=prepared();r=next(r for r in b['records'] if r['ref']=='c02')
    r['content']['text']='# padding\n'*2000+'hidden_call()\n'
    value=judgment(b)
    value['checks'][0].update(evidenceType='codeStructureCheck',quotes=[{'ref':'c02','text':'hidden_call()'}])
    with pytest.raises(ValueError):finish_verification(value,b)


def test_individual_public_cases_do_not_inherit_whole_batch_failure():
    from focusmate.core.process_verification import finish_verification
    b=prepared();b['subgoal']['acceptableEvidence'].append('publicSampleRunResult')
    run={'ref':'run','kind':'runEvidence','version':1,'content':{'codeSnapshotRef':'c02','completed':True,
        'sampleStatus':'failed','caseResults':[{'testCaseId':'one','outcome':'passed','actualOutput':'3'},
                                             {'testCaseId':'two','outcome':'outputMismatch','actualOutput':'4'}]}}
    value=judgment(b,('passed','failed'))
    for i,c in enumerate(value['checks'],1):
        c.update(evidenceType='publicSampleRunResult',quotes=[{'ref':f'run/case-{i}','text':str(i+2)}])
    report=finish_verification(value,b,extra_records=[run])
    assert report['checks'][0]['status']=='passed' and report['verifierResult']['status']=='failed'

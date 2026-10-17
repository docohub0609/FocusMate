from copy import deepcopy
import pytest
from test_process_verification import prepared
from focusmate.core.context_builder import hash_content
from focusmate.core.process_verification import finish_verification, validate_saved_report


def judgment(b,states):
    return {'checks':[{'checkId':c['checkId'],'status':s,'reason':'检查当前材料。',
        'quotes':[{'ref':'u01','text':'unused'}],'evidenceType':'evaluatedUserExplanation','limitations':[]}
        for c,s in zip(b['checks'],states)]}


def sample_bundle():
    b=prepared()
    record={'ref':'problem-examples','kind':'problemFact','version':1,
        'goalContext':b['request']['goalContext'],'problemRef':b['request']['problemRef'],
        'content':{'examples':[{'input':'1900\n','output':'NO\n'}]}}
    record['contentHash']=hash_content(record['content'])
    b['records'].append(record)
    return b


def choose(b,path):
    from focusmate.core.verification_facts import fact_catalog
    return next(f for f in fact_catalog(b) if f['ref']=='problem-examples' and f['path']==path)


def test_real_failed_sample_quote_is_replaced_by_backend_field_selection():
    b=sample_bundle();value=judgment(b,('notReady','notReady'))
    # Preserved form of the real century-leap-year-2 reply; deliberately still rejected as a quote.
    for c in value['checks']:c['quotes']=[{'ref':'problem-examples','text':'{"input": "1900\\n", "output": "NO\\n"}'}]
    with pytest.raises(ValueError,match='INVALID_EVIDENCE_QUOTE'):finish_verification(value,b)
    facts=[choose(b,'/examples/0/input'),choose(b,'/examples/0/output')]
    for c in value['checks']:c['quotes']=[{'factId':f['factId']} for f in facts]
    report=finish_verification(value,b)
    quotes=report['checks'][0]['quotes']
    assert quotes[0]=={'ref':'problem-examples','text':'{"/examples/0/input":"1900\\n"}','factId':facts[0]['factId']}
    assert report['verifierResult']['status']=='notReady'
    validate_saved_report(report,b,())


@pytest.mark.parametrize('change',['value','version','goal','problem','code','unknown'])
def test_fact_ids_do_not_survive_a_changed_binding(change):
    b=sample_bundle();f=choose(b,'/examples/0/output');value=judgment(b,('notReady','notReady'))
    for c in value['checks']:c['quotes']=[{'factId':f['factId']}]
    if change=='value':b['records'][-1]['content']['examples'][0]['output']='YES\n'
    if change=='version':b['records'][-1]['version']=2
    if change=='goal':b['request']['goalContext']['goalId']='other'
    if change=='problem':b['request']['problemRef']['problemVersion']+=1
    if change=='code':b['request']['currentCodeRef']='c03'
    if change=='unknown':value['checks'][0]['quotes'][0]['factId']='fact-does-not-exist'
    with pytest.raises(ValueError,match='INVALID_FACT_REFERENCE'):finish_verification(value,b)


@pytest.mark.parametrize('field',['ref','text'])
def test_persisted_fact_quote_cannot_be_edited(field):
    b=sample_bundle();value=judgment(b,('notReady','notReady'))
    for c in value['checks']:c['quotes']=[{'factId':choose(b,'/examples/0/output')['factId']}]
    report=finish_verification(value,b)
    report['checks'][0]['quotes'][0][field]='forged'
    with pytest.raises(ValueError,match='FACT_QUOTE_MISMATCH'):validate_saved_report(report,b,())


def test_problem_answer_is_not_a_successful_execution():
    b=sample_bundle();b['subgoal']['acceptableEvidence'].append('publicSampleRunResult')
    value=judgment(b,('passed','passed'))
    for c in value['checks']:c.update(evidenceType='publicSampleRunResult',quotes=[{'factId':choose(b,'/examples/0/output')['factId']}])
    with pytest.raises(ValueError,match='RUN_EVIDENCE_REQUIRED'):finish_verification(value,b)


@pytest.mark.parametrize('value',[0,False,None,''])
def test_fact_values_keep_their_type_and_empty_content(value):
    b=sample_bundle();b['records'][-1]['content']={'value':value}
    f=choose(b,'/value');assert f['value']==value and type(f['value']) is type(value)
    result=judgment(b,('notReady','notReady'))
    for c in result['checks']:c['quotes']=[{'factId':f['factId']}]
    assert finish_verification(result,b)['checks'][0]['quotes'][0]['text']


def test_fact_catalog_does_not_expose_source_or_model_opinions():
    from focusmate.core.verification_facts import fact_catalog
    b=sample_bundle()
    for kind in ('code','verifiedFact','hypothesis'):
        b['records'].append({'ref':kind+'-extra','kind':kind,'content':{'text':'SECRET_SOURCE'}})
    assert all('SECRET_SOURCE' not in str(f) for f in fact_catalog(b))

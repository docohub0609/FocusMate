import json
import pytest
from test_diagnosis_manager import setup,drain
from test_diagnosis import candidate
from focusmate.core.diagnosis_execution import DiagnosisExecutor
from focusmate.core.manager_execution import advance_once
from focusmate.core.tool_gateway import ToolGateway
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from test_manager_execution import Backend


def test_tool_permission_and_real_receipt_are_part_of_next_context(tmp_path):
    call={'callId':'read1','toolName':'read_code_excerpt','arguments':{'codeVersion':2,'startLine':1,'endLine':1}}
    m,p,e=setup(tmp_path,[{'content':'','toolCalls':[call]},{'content':json.dumps(candidate()),'toolCalls':[]}])
    # Test caller, not model, supplies permission before processing starts.
    def grant(s):
        s['allowedTools']=['read_code_excerpt'];s['rounds'][s['currentRoundId']]['allowedTools']=['read_code_excerpt']
    m.store.change('s','test-grant',{'test':'permission'},grant)
    import hashlib
    text=next(x for x in e.bundle['records'] if x['ref']=='c02')['content']['text']
    backend=Backend();backend.load_code=lambda ctx,version:{'codeVersion':version,'sourceCode':text,'contentHash':hashlib.sha256(text.encode()).hexdigest()}
    e=DiagnosisExecutor(p,ToolGateway(backend,SQLiteToolLedger(tmp_path/'tools.db')),backend,bundle=e.bundle,skills=[])
    s,r=drain(m,e)
    assert r['counts']['tool']==1 and r['counts']['model']==2
    assert any(msg.get('tool_call_id')=='read1' for msg in p.calls[-1]['messages'])
    assert r['diagnosis']['inputSnapshot']['actualMaterials'][0]['content']['codeVersion']==2


def test_forbidden_tool_never_executed(tmp_path):
    call={'callId':'run','toolName':'run_public_samples','arguments':{'codeVersion':2}}
    m,p,e=setup(tmp_path,[{'content':'','toolCalls':[call]}]);s,r=drain(m,e)
    assert r['failure']=='TOOL_NOT_ALLOWED' and r['counts']['tool']==0


def test_tool_cannot_execute_different_source_with_same_version(tmp_path):
    m,p,e=setup(tmp_path)
    st={'kind':'tool','roundId':m.read('s')['currentRoundId'],'payload':{'callId':'r','toolName':'run_public_samples','arguments':{'codeVersion':2}}}
    e.backend=Backend()
    assert e.execute(m.read('s'),st)['code']=='TOOL_SOURCE_PIN_MISMATCH'


def test_model_budget_shared_without_nested_retry(tmp_path):
    # Service error consumes the one attempted request and does not loop.
    m,p,e=setup(tmp_path)
    def broken(**kwargs):raise RuntimeError('offline')
    p.complete=broken;s,r=drain(m,e)
    assert r['status']=='failed' and r['counts']['model']==1 and not s['outbox']


def test_history_expected_labels_are_not_model_inputs(tmp_path):
    from tools.diagnosis_cases import cases,material_bundle
    from focusmate.core.diagnosis import prepare_diagnosis
    from focusmate.core.context_builder import MapContextResolver
    c=cases('development')[0];c['expected']='DO_NOT_LEAK_EXPECTATION'
    req,records=material_bundle(c)
    bundle=prepare_diagnosis(req,MapContextResolver(records))
    assert 'DO_NOT_LEAK_EXPECTATION' not in json.dumps(bundle)


@pytest.mark.parametrize('analysis',['stage','progress'])
def test_new_tool_result_reaches_subsequent_analysis(tmp_path,analysis):
    import hashlib
    read={'callId':'read','toolName':'read_code_excerpt','arguments':{'codeVersion':2,'startLine':1,'endLine':1}}
    stage={'callId':'analysis','toolName':'analyze_'+analysis,'arguments':{'reason':'结合新读到的内容判断'}}
    m,p,e=setup(tmp_path,[{'content':'','toolCalls':[read]},{'content':'','toolCalls':[stage]}])
    def grant(s):
        s['allowedTools']=['read_code_excerpt'];s['rounds'][s['currentRoundId']]['allowedTools']=['read_code_excerpt']
    m.store.change('s','grant',{},grant)
    text=next(r for r in e.bundle['records'] if r['ref']=='c02')['content']['text']
    backend=Backend();backend.load_code=lambda ctx,version:{'codeVersion':version,'sourceCode':text,'contentHash':hashlib.sha256(text.encode()).hexdigest()}
    e=DiagnosisExecutor(p,ToolGateway(backend,SQLiteToolLedger(tmp_path/'tools.db')),backend,bundle=e.bundle,skills=[])
    for _ in range(5):advance_once(m,'s',e)
    state=m.read('s');step=m.claim('s','review-regression')
    extra,_,_=e._records(state,step);ref=extra[0]['ref']
    value=({'stage':'unknown','reasons':[],
        'unknowns':[{'text':'只读到一行，仍缺完整范围','evidenceRefs':[ref]}]} if analysis=='stage' else
        {'findings':[{'kind':'unknown','subject':'已读取片段','reason':'只读到一行，不能判断完整范围',
                      'evidenceRefs':[ref],'uncertainties':['缺其他范围']}]})
    p.responses=iter([{'content':json.dumps(value),'toolCalls':[]}])
    result=e.execute(state,step)
    payload=json.loads(p.calls[-1]['messages'][1]['content'])
    assert ref in payload['evidenceCatalog']
    assert result['kind']=='diagnosis_next'
    assert ref in result['payload']['material']['content']['scopeRefs']

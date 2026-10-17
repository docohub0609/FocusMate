import json
from copy import deepcopy
from test_diagnosis import inputs,candidate
from test_manager_execution import Provider, Backend
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.diagnosis import prepare_diagnosis
from focusmate.core.diagnosis_execution import DiagnosisExecutor
from focusmate.core.manager_execution import advance_once
from focusmate.core.session_manager import SessionManager
from focusmate.adapters.manager_storage import SQLiteManagerStore


def setup(tmp_path,responses=None):
    req,records=inputs();bundle=prepare_diagnosis(req,MapContextResolver(records))
    m=SessionManager(SQLiteManagerStore(tmp_path/'manager.db'))
    m.create('s',req['goalContext'],req['problemRef'],contract_version=1,code_version=2,allowed_tools=[],mode='diagnosis')
    m.command('s','ask','ask',{'text':records[-1]['content']['text']})
    p=Provider(responses or [{'content':json.dumps(candidate(),ensure_ascii=False),'toolCalls':[]}])
    e=DiagnosisExecutor(p,None,None,bundle=bundle,skills=[])
    return m,p,e


def drain(m,e):
    for _ in range(15):
        if advance_once(m,'s',e)['status']=='idle':break
    state=m.read('s');return state,state['rounds'][state['currentRoundId']]


def test_diagnosis_saved_not_published_and_restart_reuses(tmp_path):
    m,p,e=setup(tmp_path);s,r=drain(m,e)
    assert r['status']=='finished' and r['diagnosis']['status']=='diagnosed'
    assert not s['outbox'] and m.publish('s') is None
    assert r['counts']['model']==1 and len(p.calls)==1
    m=SessionManager(SQLiteManagerStore(tmp_path/'manager.db'));drain(m,e)
    assert len(p.calls)==1


def test_pause_late_diagnosis_saved_but_not_finished(tmp_path):
    m,p,e=setup(tmp_path);advance_once(m,'s',e)
    step=m.claim('s','w');result=e.execute(m.read('s'),step)
    m.command('s','pause','pause',{});m.complete('s',step['stepId'],step['token'],result)
    assert m.read('s')['status']=='paused' and not m.read('s')['outbox']
    r=m.read('s')['rounds'][step['roundId']]
    assert 'diagnosis' not in r
    m.command('s','resume','resume',{});s,r=drain(m,e)
    assert r['diagnosis']['status']=='diagnosed' and r['counts']['model']==1


def test_bad_output_fails_without_retry_or_diagnosis(tmp_path):
    m,p,e=setup(tmp_path,[{'content':'bad','toolCalls':[]}]);s,r=drain(m,e)
    assert r['status']=='failed' and 'diagnosis' not in r and len(p.calls)==1


def test_no_code_manager_session_is_permitted(tmp_path):
    m=SessionManager(SQLiteManagerStore(tmp_path/'manager.db'))
    m.create('s',{'goalId':'g','scope':'goal'},{'problemId':'p','problemVersion':1},contract_version=1,code_version=None,allowed_tools=[],mode='diagnosis')
    assert m.read('s')['codeVersion'] is None


def test_new_message_reinterpreted_without_refunding_budget(tmp_path):
    m,p,e=setup(tmp_path);advance_once(m,'s',e)
    old=m.claim('s','worker');answer=e.execute(m.read('s'),old)
    m.command('s','new','message',{'text':'现在只问返回值在哪里。'})
    m.complete('s',old['stepId'],old['token'],answer)
    c=candidate();c['questions'][0]['question']='返回值在哪里'
    p.responses=iter([{'content':json.dumps(c),'toolCalls':[]}])
    s,r=drain(m,e)
    assert r['status']=='finished' and r['counts']['model']==2
    assert '现在只问返回值在哪里' in p.calls[-1]['messages'][1]['content']


def test_version_change_keeps_late_diagnosis_only_as_audit(tmp_path):
    m,p,e=setup(tmp_path);advance_once(m,'s',e);st=m.claim('s','w');answer=e.execute(m.read('s'),st)
    m.command('s','v3','code_changed',{'codeVersion':3});m.complete('s',st['stepId'],st['token'],answer)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='superseded' and 'diagnosis' not in r and not s['outbox']


def test_skill_selection_is_local_step_and_pin_is_checked(tmp_path):
    from focusmate.core.skill_sections import load_index
    from pathlib import Path
    desc=json.loads(Path('skills/dynamic-programming/descriptor.json').read_text(encoding='utf-8'))
    skill=load_index('skills/dynamic-programming',desc['version'],desc['hash'])
    call={'callId':'select','toolName':'read_skill','arguments':{'skillId':skill.skill_id,'version':skill.version,'hash':skill.hash,'sectionIds':[skill.sections[0].section_id]}}
    m,p,e=setup(tmp_path,[{'content':'','toolCalls':[call]},{'content':json.dumps(candidate()),'toolCalls':[]}])
    e=DiagnosisExecutor(p,None,None,bundle=e.bundle,skills=[skill]);s,r=drain(m,e)
    assert r['status']=='finished' and r['counts']['model']==2 and r['counts']['tool']==0
    assert r['diagnosis']['inputSnapshot']['selectedSkill']['semanticStatus']=='instructionsOnly'


def test_stage_analysis_counted_in_same_round(tmp_path):
    m,p,e=setup(tmp_path,[{'content':'','toolCalls':[{'callId':'stage','toolName':'analyze_stage','arguments':{'reason':'用户询问整体阶段'}}]},
        {'content':json.dumps({'stage':'unknown','reasons':[],'unknowns':[{'text':'缺少外层代码','evidenceRefs':['c02']}]}),'toolCalls':[]},
        {'content':json.dumps(candidate()),'toolCalls':[]}])
    s,r=drain(m,e)
    assert r['status']=='finished' and r['counts']['model']==3
    assert r['diagnosis']['inputSnapshot']['actualMaterials'][0]['content']['semanticStatus']=='notVerified'


def test_oversized_required_context_returns_scope_gap_without_model(tmp_path):
    m,p,e=setup(tmp_path)
    e.bundle['request']['budget']={'contextWindowTokens':20,'reservedOutputTokens':10,'safetyMarginTokens':1}
    s,r=drain(m,e)
    assert r['diagnosis']['inputSnapshot']['contextFailure']['code']=='REQUIRED_MATERIAL_TOO_LONG'
    assert not p.calls

import json
from copy import deepcopy
import pytest
from test_skill_execution import request, candidate, Provider as ReviewProvider
from test_tool_dialogue import context, Provider, Gateway


def run(provider, reviewer=None, gateway=None, **kwargs):
    from focusmate.core.skill_tool_execution import execute_skill_with_tools
    req=request(); req['sections']=['visit-control']
    ctx=context();ctx['goalContext']=req['goalContext']
    return execute_skill_with_tools(req,ctx,provider,reviewer or ReviewProvider(),gateway or Gateway(),enabled=True,**kwargs)


def answer(value=None):
    return {'content':json.dumps(value or candidate()),'toolCalls':[]}


def test_direct_skill_candidate_still_reviewed():
    r=run(Provider([answer()]))
    assert r['status']=='accepted'
    assert r['counts']=={'analysis':1,'tools':0,'review':1,'correction':0,'modelTotal':2}


def test_tool_result_visible_to_review_and_repair_without_rerun():
    class RefGateway(Gateway):
        def invoke(self,ctx,call):
            return {**super().invoke(ctx,call),'ref':'evidence-a'}
    c=candidate();c['findings'][0]['evidenceRefs']=['evidence-a']
    p=Provider([{'content':'','toolCalls':[{'callId':'a','toolName':'read_code_excerpt','arguments':{}}]},answer(c)])
    reviewer=ReviewProvider(('fail','pass'));g=RefGateway()
    r=run(p,reviewer,g)
    assert r['status']=='accepted'
    assert len(g.calls)==1
    assert reviewer.calls[0][1]['toolResults'][0]['ref']=='evidence-a'
    assert 'evidence-a' in reviewer.calls[0][1]['allowedMaterialRefs']
    assert r['counts']['correction']==1
    assert len({c['deadline'] for c in p.requests} | {c[0]['deadline'] for c in reviewer.calls})==1


@pytest.mark.parametrize('verdict,reason',[('uncertain','REVIEW_UNCERTAIN'),('fail','REVIEW_FAILED')])
def test_uncertain_or_second_failure_rejected(verdict,reason):
    r=run(Provider([answer()]),ReviewProvider((verdict,verdict)))
    assert r['failure']['reason']==reason
    assert r['counts']['correction']<=1


def test_fabricated_reference_rejected_before_review():
    c=candidate();c['findings'][0]['evidenceRefs']=['fake-tool']
    reviewer=ReviewProvider();r=run(Provider([answer(c)]),reviewer)
    assert r['failure']['reason']=='INVALID_CANDIDATE'
    assert not reviewer.calls


def test_optin_required_and_hash_checked_before_model():
    from focusmate.core.skill_tool_execution import execute_skill_with_tools
    p=Provider([]);req=request();ctx=context()
    assert execute_skill_with_tools(req,ctx,p,ReviewProvider(),Gateway())['failure']['reason']=='INVALID_INPUT'
    req['skillRef']['skillHash']='sha256:'+'0'*64;req['sections']=['visit-control'];ctx['goalContext']=req['goalContext']
    assert execute_skill_with_tools(req,ctx,p,ReviewProvider(),Gateway(),enabled=True)['failure']['reason']=='INVALID_INPUT'
    assert not p.requests

@pytest.mark.parametrize('name,args,error',[('run_public_samples',{'codeVersion':2},'NOT_ALLOWED'),('read_code_excerpt',{'codeVersion':'bad'},'INVALID_ARGUMENTS')])
def test_permission_and_bad_parameters_are_returned_as_evidence(tmp_path,name,args,error):
    from test_tool_gateway import Backend,ToolGateway,SQLiteToolLedger
    g=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'calls.db'))
    p=Provider([{'content':'','toolCalls':[{'callId':'a','toolName':name,'arguments':args}]},answer()])
    r=run(p,gateway=g)
    assert r['toolDialogue']['toolResults'][0]['errorCode']==error
    assert r['status']=='accepted'
    assert g.backend.runs==0


def test_wrong_result_scope_stops_before_review():
    class Wrong(Gateway):
        def invoke(self,ctx,call):
            return {**super().invoke(ctx,call),'loopId':'wrong'}
    r=run(Provider([{'content':'','toolCalls':[{'callId':'a','toolName':'read_code_excerpt','arguments':{}}]}]),gateway=Wrong())
    assert r['failure']['reason']=='TOOL_RESULT_MISMATCH'
    assert r['counts']['review']==0


def test_missing_review_check_is_not_success():
    class Incomplete(ReviewProvider):
        def complete(self,**kwargs):
            from focusmate.contracts.skill_execution import ProviderResponse
            return ProviderResponse('{"checks":{}}')
    assert run(Provider([answer()]),Incomplete())['failure']['reason']=='INVALID_REVIEW'


def test_deadline_exhausted_after_tool_phase_never_reviews():
    now=[0.0]
    class Late(Provider):
        def complete(self,**kwargs):
            value=super().complete(**kwargs);now[0]=121;return value
    reviewer=ReviewProvider();r=run(Late([answer()]),reviewer,clock=lambda:now[0])
    assert r['failure']['reason']=='DEADLINE_EXCEEDED'
    assert not reviewer.calls


def test_public_run_then_analysis_and_review(tmp_path):
    from test_tool_gateway import Backend,ToolGateway,SQLiteToolLedger
    from focusmate.core.skill_tool_execution import execute_skill_with_tools
    class Public(Backend):
        def run_public(self,ctx,code):
            self.runs+=1
            return {'status':'succeeded','codeVersion':2,'goalContext':ctx['goalContext'],'problemRef':ctx['problemRef'],
                    'sampleStatus':'failed','caseResults':[{'outcome':'wrongAnswer'}]}
    req=request();req['sections']=['visit-control'];ctx=context();ctx['goalContext']=req['goalContext'];ctx['allowedTools']=['run_public_samples']
    g=ToolGateway(Public(),SQLiteToolLedger(tmp_path/'calls.db'))
    p=Provider([{'content':'','toolCalls':[{'callId':'a','toolName':'run_public_samples','arguments':{'codeVersion':2}}]},answer()])
    reviewer=ReviewProvider(('fail','pass'))
    r=execute_skill_with_tools(req,ctx,p,reviewer,g,enabled=True)
    assert r['status']=='accepted'
    assert reviewer.calls[0][1]['toolResults'][0]['data']['sampleStatus']=='failed'
    assert g.backend.runs==1

@pytest.mark.parametrize('declared',['run_public_samples','shell_exec'])
def test_tool_declaration_is_checked_and_never_grants_permission(tmp_path,declared):
    import shutil
    from pathlib import Path
    from focusmate.contracts.skill_files import fingerprint,read_files
    from focusmate.core.skill_tool_execution import execute_skill_with_tools
    from test_tool_gateway import Backend,ToolGateway,SQLiteToolLedger
    from test_skill_execution import ROOT
    package=tmp_path/'skills'/'tree-and-graph';shutil.copytree(ROOT/'skills/tree-and-graph',package)
    descriptor=json.loads((package/'descriptor.json').read_text(encoding='utf-8'));descriptor['requiredTools']=[declared]
    (package/'descriptor.json').write_text(json.dumps(descriptor),encoding='utf-8')
    descriptor['hash']=fingerprint(read_files(package),descriptor)[0]
    (package/'descriptor.json').write_text(json.dumps(descriptor),encoding='utf-8')
    req=request();req['sections']=['visit-control'];req['skillRef']['skillHash']=descriptor['hash']
    ctx=context();ctx['goalContext']=req['goalContext'];ctx['allowedTools']=[]
    p=Provider([{'content':'','toolCalls':[{'callId':'a','toolName':'run_public_samples','arguments':{'codeVersion':2}}]},answer()])
    g=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'calls.db'))
    r=execute_skill_with_tools(req,ctx,p,ReviewProvider(),g,enabled=True,skills_root=tmp_path/'skills')
    if declared=='shell_exec':
        assert r['failure']['reason']=='INVALID_INPUT'
        assert not p.requests
    else:
        assert r['toolDialogue']['toolResults'][0]['errorCode']=='NOT_ALLOWED'
        assert not p.requests[0]['tools']
    assert g.backend.runs==0


def test_tool_budget_cannot_be_renewed_during_analysis(tmp_path):
    from test_tool_gateway import Backend,ToolGateway,SQLiteToolLedger
    g=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'calls.db'))
    p=Provider([{'content':'','toolCalls':[{'callId':str(i),'toolName':'read_code_excerpt',
         'arguments':{'codeVersion':2,'startLine':1,'endLine':1}}]} for i in range(4)])
    reviewer=ReviewProvider();r=run(p,reviewer,g,max_tool_requests=1)
    assert r['failure']['reason']=='TOOL_LIMIT_EXCEEDED'
    assert len(r['toolDialogue']['toolResults'])==2
    assert r['toolDialogue']['toolResults'][1]['errorCode']=='TOOL_LIMIT_EXCEEDED'
    assert not reviewer.calls


def test_non_json_final_answer_is_candidate_failure():
    r=run(Provider([{'content':'plain prose','toolCalls':[]}]))
    assert r['failure']['reason']=='INVALID_CANDIDATE'
    assert r['counts']['review']==0

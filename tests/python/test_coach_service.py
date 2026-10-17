import pytest
from tests.python.test_editor_workspace import editor


def setup_service(tmp_path, *, workflow_version=1,repair_policy_version=1):
    from apps.api.coach_service import CoachService
    e=editor(tmp_path);g=e.workspace('problem-submit',1,create=True)['goal']['goalId']
    service=CoachService(e.path)
    service.manager.workflow_version=workflow_version
    service.manager.repair_policy_version=repair_policy_version
    return e,g,service


def test_workspace_is_idempotent_and_no_model_is_needed(tmp_path):
    e,g,s=setup_service(tmp_path)
    first=s.workspace(g,create=True)
    assert s.workspace(g,create=True)==first
    assert first['workGoals']==[]
    assert first['contract']['schemaVersion']=='2.0.0'
    assert first['session']['status']=='waiting_user'
    state=s.manager.read(first['session']['sessionId'])
    assert state['rounds']=={}


def test_command_pins_code_and_retries_original_request(tmp_path):
    e,g,s=setup_service(tmp_path)
    session=s.workspace(g,create=True)['session']['sessionId']
    cmd={'operationId':'ask','kind':'ask','codeVersion':None,'payload':{'text':'题意是什么？'}}
    first=s.command(session,cmd)
    e.save_draft(g,{'sourceCode':'print(1)','expectedRevision':0,'operationId':'save'})
    assert s.command(session,cmd)==first
    with pytest.raises(ValueError,match='CONFLICT'):
        s.command(session,{**cmd,'codeVersion':1})
    with pytest.raises(ValueError,match='CODE'):
        s.command(session,{**cmd,'operationId':'other'})
    assert s.read(session)['messages'][0]['text']=='题意是什么？'


def test_business_materials_are_authoritative_and_empty_code_invalidates(tmp_path):
    e,g,s=setup_service(tmp_path);session=s.workspace(g,create=True)['session']['sessionId']
    e.save_draft(g,{'sourceCode':'print(1)','expectedRevision':0,'operationId':'save'})
    s.command(session,{'operationId':'a','kind':'ask','codeVersion':1,'payload':{'text':'解释'}})
    records=s.materials(session)['records']
    assert any(x['kind']=='code' and x['content']['text']=='print(1)' for x in records)
    e.save_draft(g,{'sourceCode':'','expectedRevision':1,'operationId':'clear'})
    s.synchronize(session)
    state=s.manager.read(session)
    assert state['codeVersion'] is None
    assert state['rounds'][state['currentRoundId']]['status']=='superseded'


def test_permission_query_exposes_exact_pinned_request(tmp_path):
    e,g,s=setup_service(tmp_path);session=s.workspace(g,create=True)['session']['sessionId']
    e.save_draft(g,{'sourceCode':'print(1)','expectedRevision':0,'operationId':'save'})
    s.command(session,{'operationId':'ask','kind':'ask','codeVersion':1,'payload':{'text':'检查'}})
    st=s.manager.claim(session,'w',materials=s.materials(session))
    call={'callId':'run','toolName':'run_public_samples','arguments':{'codeVersion':1}}
    s.complete(session,st['stepId'],st['token'],{'kind':'toolRequest','call':call})
    permission=s.read(session)['pendingPermission']
    assert permission['toolName']=='run_public_samples' and permission['arguments']=={'codeVersion':1}
    cmd={'operationId':'grant','kind':'grant_permission','codeVersion':1,
        'payload':{'tools':['run_public_samples'],'stepId':'different-step'}}
    with pytest.raises(ValueError,match='MISMATCH'):s.command(session,cmd)
    cmd['payload']['stepId']=permission['stepId'];s.command(session,cmd)
    assert s.read(session)['pendingPermission'] is None

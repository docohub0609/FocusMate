"""Real saved content and explicit synthetic transport errors; no live model."""
import json
from pathlib import Path
import pytest
from apps.api.deepseek_provider import DeepSeekConfig
from focusmate.adapters.coach_chat import DeepSeekCoachProvider, StrictDeepSeekCoachProvider
from tests.python.test_coach_service import setup_service
from tests.python.test_dynamic_coach_loop import diagnosis
from services.coach_worker.worker import CoachWorker


class Response:
    def __init__(self, message, finish): self.message,self.finish=message,finish
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,limit):
        return json.dumps({'id':'response-replay','model':'test-model','usage':{'completion_tokens':12},
            'choices':[{'finish_reason':self.finish,'message':{**self.message,'reasoning_content':'NEVER_KEEP'}}]}).encode()


@pytest.mark.parametrize('strict',[False,True])
@pytest.mark.parametrize('forged_reference',[False,True])
def test_failure_original_and_position_reach_actual_repair_request(tmp_path,strict,forged_reference):
    # Compatibility replay of an already-started legacy correction protocol.
    _,goal,service=setup_service(tmp_path,repair_policy_version=0)
    session=service.workspace(goal,create=True)['session']['sessionId']
    service.command(session,{'operationId':'a','kind':'ask','codeVersion':None,'payload':{'text':'解释'}})
    saved=json.loads((Path(__file__).parents[1]/'fixtures/coach/malformed-diagnosis.json').read_text(encoding='utf-8'))['content']
    bad='{"step":{"kind":"response","value":{}}}}' if strict else saved
    repaired=diagnosis()
    if forged_reference:repaired['questions'][0]['evidenceRefs']=['invented-source']
    good=json.dumps({'value':repaired,'candidateGoals':[]})
    if strict: good=json.dumps({'step':{'kind':'response','value':json.loads(good)}})
    replies=iter([bad,good]);requests=[]
    def opener(request,timeout):
        requests.append(json.loads(request.data))
        content=next(replies)
        return Response({'tool_calls':[{'id':'f','function':{'name':'return_coach_step','arguments':content}}]},'tool_calls') if strict else Response({'content':content},'stop')
    cls=StrictDeepSeekCoachProvider if strict else DeepSeekCoachProvider
    provider=cls(DeepSeekConfig('DO_NOT_LOG_KEY','https://api.deepseek.com','test'),opener=opener)
    worker=CoachWorker(service,provider)
    assert worker.process_once() and worker.process_once()
    repair=json.loads(requests[1]['messages'][-1]['content'])
    assert repair['previousRejectedResponse']==bad
    if not strict:
        assert repair['repairTask']=='jsonSyntaxOnly'
        assert 'materials' not in repair and 'previousHelp' not in repair
        assert not requests[1].get('tools')
    detail=repair['responseDiagnostic']
    assert detail['channel']==('toolArguments' if strict else 'content')
    assert detail['position']==len(bad)-1 and detail['line']==1
    assert detail['truncated'] is False
    assert repair['rejectedResponseInfo']['truncated'] is False
    assert repair['rejectedResponseInfo']['originalLength']==len(bad)
    state=service.manager.read(session)
    first=min((x for x in state['steps'].values() if x['result'] and x['result'].get('kind')=='failure'),key=lambda x:x['finishedAt'])
    assert first['result']['responseAudit']['responseId']=='response-replay'
    assert first['result']['responseAudit']['usage']['completion_tokens']==12
    assert 'NEVER_KEEP' not in json.dumps(state) and 'DO_NOT_LOG_KEY' not in json.dumps(state)
    assert state['rounds'][state['currentRoundId']]['counts']['model']==2
    if forged_reference:
        assert state['rounds'][state['currentRoundId']]['status']=='failed'
    assert not state['outbox']  # Repair has only restored a valid diagnosis, not approved help.
    public=service.read(session)
    assert bad not in json.dumps(public,ensure_ascii=False)
    assert 'responseDiagnostic' not in json.dumps(public)


def test_long_original_is_explicitly_bounded_not_silently_complete():
    from focusmate.contracts.model_response import response_diagnostic
    detail=response_diagnostic('x'*70000,ValueError('bad'),channel='content')
    assert detail['truncated'] and detail['originalLength']==70000
    assert len(detail['text'])==65536 and detail['sha256']


def test_duplicate_keys_are_not_repaired_by_discarding_a_value():
    from focusmate.contracts.model_response import response_diagnostic
    from focusmate.contracts.skill_files import strict_json
    bad='{"status":"passed","status":"failed"}'
    with pytest.raises(ValueError) as error:strict_json(bad)
    detail=response_diagnostic(bad,error.value,channel='content')
    assert detail['text']==bad and detail['position'] is None


def test_mismatched_history_closer_has_precise_hint_without_changing_original():
    from focusmate.contracts.model_response import response_diagnostic
    from focusmate.contracts.skill_files import strict_json
    bad='{"history":{"reason":"ignore bracket ] and escaped \\\" quote","evidenceRefs":[]}],"dependsOn":[]}'
    with pytest.raises(ValueError) as error: strict_json(bad)
    detail=response_diagnostic(bad,error.value,channel='content')
    assert detail['text']==bad
    hint=detail['syntaxHint']
    assert hint['expectedCloser']=='}' and hint['actualCloser']==']'
    assert hint['position']==bad.index('}],')+1
    assert hint['openedAt']==0


def test_no_bracket_hint_for_missing_comma_or_duplicate_keys():
    from focusmate.contracts.model_response import response_diagnostic
    from focusmate.contracts.skill_files import strict_json
    for bad in ('{"a":1 "b":2}', '{"a":1,"a":2}'):
        with pytest.raises(ValueError) as error: strict_json(bad)
        assert response_diagnostic(bad,error.value,channel='content').get('syntaxHint') is None


@pytest.mark.parametrize('error_type,truncated',[('ValueError',False),('JSONDecodeError',True)])
def test_semantic_or_truncated_failure_keeps_full_materials(tmp_path,error_type,truncated):
    from tests.python.test_dynamic_coach_manager import manager
    from tests.python.test_dynamic_coach_loop import materials
    from focusmate.core.dynamic_coach_execution import request_payload
    m=manager(tmp_path);m.command('s','a','ask',{'text':'解释'})
    st=m.claim('s','w',materials=materials(m))
    st['payload'].update(repairReason='MODEL_RESPONSE_INVALID',previousRejectedResponse='{"a":1',
        responseDiagnostic={'errorType':error_type,'channel':'content','truncated':truncated})
    payload=request_payload(m.read('s'),st)
    assert 'materials' in payload and 'repairTask' not in payload

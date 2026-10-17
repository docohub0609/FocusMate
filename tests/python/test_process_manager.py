import json
from copy import deepcopy
import pytest
from test_action_manager import setup
from test_action_policy import answer
from test_action_review import review
from test_diagnosis_manager import drain
from test_process_verification import inputs
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.manager_execution import advance_once


class VerifierProvider:
    public_config={'model':'fake','maxTokens':1024}
    def __init__(self):self.calls=[]
    def complete(self,**kw):
        self.calls.append(kw);payload=json.loads(kw['messages'][1]['content'])
        quote=next(r['content']['text'] for r in payload['materials'] if r['ref']=='u01')
        return {'content':json.dumps({'checks':[{'checkId':c['checkId'],'status':'inconclusive',
            'reason':'缺完整循环','quotes':[{'ref':'u01','text':quote}],
            'evidenceType':'evaluatedUserExplanation','limitations':['只提供局部代码']} for c in payload['requirements']]},ensure_ascii=False),
            'toolCalls':[],'model':'fake'}


def start(tmp_path):
    from focusmate.core.process_execution import ProcessExecutor
    from focusmate.core.process_verification import prepare_verification
    m,_,actor=setup(tmp_path,[answer(),review()]);drain(m,actor);published=m.publish('s')
    req,records,_=inputs();req['actionId']=published['actionId']
    b=prepare_verification(req,MapContextResolver(records),published['learningAction'])
    p=VerifierProvider();e=ProcessExecutor(p,None,None,bundle=b)
    m.command('s','verify','verification',{'actionId':req['actionId'],'subgoalId':'s1',
        'materialRefs':req['materialRefs'],'codeVersion':2})
    return m,p,e


def test_formal_result_saved_once_and_restart_does_not_rejudge(tmp_path):
    m,p,e=start(tmp_path);s,r=drain(m,e)
    assert r['status']=='finished' and r['verification']['verifierResult']['status']=='inconclusive'
    assert r['counts']['model']==1
    report=m.verification_report('s',r['verification']['verifierResult']['verifierResultId'])
    assert report['actionId']==s['outbox'][-1]['actionId']
    drain(m,e);assert len(p.calls)==1 and len(m.read('s')['outbox'])==1


def pending(m,e):
    advance_once(m,'s',e);step=m.claim('s','verifier');assert step['kind']=='verification_model'
    return step,e.execute(m.read('s'),step)


def test_pause_retains_report_until_resume(tmp_path):
    m,p,e=start(tmp_path);st,result=pending(m,e)
    m.command('s','pause','pause',{});m.complete('s',st['stepId'],st['token'],result)
    assert m.read('s')['status']=='paused'
    with pytest.raises(ValueError):m.verification_report('s',result['report']['verifierResult']['verifierResultId'])
    m.command('s','resume','resume',{});s,r=drain(m,e)
    assert r['status']=='finished' and len(p.calls)==1


@pytest.mark.parametrize('kind,payload',[('code_changed',{'codeVersion':3}),('message',{'text':'请先别检查这个目标'})])
def test_obsolete_verification_never_becomes_accepted(tmp_path,kind,payload):
    m,p,e=start(tmp_path);st,result=pending(m,e)
    m.command('s','change',kind,payload);m.complete('s',st['stepId'],st['token'],result)
    assert 'verification' not in m.read('s')['rounds'][st['roundId']]
    with pytest.raises(ValueError):m.verification_report('s',result['report']['verifierResult']['verifierResultId'])


def test_wrong_subgoal_rejected_before_any_verifier_request(tmp_path):
    m,p,e=start(tmp_path)
    with pytest.raises(ValueError):m.command('s','bad','verification',{'actionId':e.bundle['request']['actionId'],
        'subgoalId':'invented','materialRefs':['u01'],'codeVersion':2})
    assert not p.calls


def test_new_message_during_verification_starts_a_fresh_diagnosis_round(tmp_path):
    m,p,e=start(tmp_path);old=m.read('s')['currentRoundId']
    m.command('s','new-question','message',{'text':'请解释另一处代码'})
    state=m.read('s');new=state['currentRoundId']
    assert new!=old and state['rounds'][old]['status']=='superseded'
    assert any(s['roundId']==new and s['kind']=='diagnosis_prepare' and s['status']=='pending' for s in state['steps'].values())


def test_report_summary_cannot_override_item_judgments(tmp_path):
    m,p,e=start(tmp_path);st,result=pending(m,e)
    result['report']['verifierResult']['status']='passed'
    with pytest.raises(ValueError):m.complete('s',st['stepId'],st['token'],result)
    assert not m.read('s')['rounds'][st['roundId']].get('verificationAccepted')


def test_permission_change_preserves_verification_work_and_budget(tmp_path):
    m,p,e=start(tmp_path);old=m.read('s')['rounds'][m.read('s')['currentRoundId']]
    advance_once(m,'s',e)
    m.command('s','permission','request_permission',{'tools':['read_code_excerpt']})
    m.command('s','grant','grant_permission',{'tools':['read_code_excerpt']})
    state=m.read('s');r=state['rounds'][state['currentRoundId']]
    assert r.get('phase')=='verification' and r['verificationRequest']==old['verificationRequest']
    assert r['deadline']==old['deadline'] and r['counts']==old['counts']
    assert r['inputBundle']['materials']['processVerification']==e.bundle
    _,r=drain(m,e);assert r['verificationAccepted']


def test_explanation_only_request_in_session_with_saved_code(tmp_path):
    m,p,e=start(tmp_path)
    req=e.bundle['request'];req['currentCodeRef']=None
    req['materialRefs'].remove('c02');req['sources']=[s for s in req['sources'] if s['ref']!='c02']
    e.bundle['records']=[r for r in e.bundle['records'] if r['ref']!='c02']
    # Recreate explicit verification command before any work; its guard version stays 2.
    m.command('s','stop','revoke_execution',{})
    m.command('s','explanation','verification',{'actionId':req['actionId'],'subgoalId':'s1','materialRefs':req['materialRefs'],'codeVersion':2})
    e.materials={'processVerification':deepcopy(e.bundle)}
    _,r=drain(m,e)
    assert r['verificationAccepted'] and 'codeVersion' not in r['verification']['verifierResult']['inputVersionRefs']


def test_tool_returns_to_verifier_with_actual_nested_run_result(tmp_path):
    m,p,e=start(tmp_path)
    state=m.read('s');rid=state['currentRoundId']
    call={'callId':'r1','toolName':'run_public_samples','arguments':{'codeVersion':2}}
    data={'status':'succeeded','sampleStatus':'passed','caseResults':[{'outcome':'passed','actualOutput':'3'}],'codeVersion':2}
    # A real gateway envelope has the result inside data, not at its top level.
    state['steps']['t']={'roundId':rid,'kind':'tool','status':'done','payload':call,
        'result':{'result':{'status':'succeeded','data':data,'goalContext':state['goalContext'],'codeVersion':2}}}
    record,_=e._evidence(state,{'roundId':rid})
    assert record[0]['content']['sampleStatus']=='passed'
    assert record[0]['content']['completed'] is True

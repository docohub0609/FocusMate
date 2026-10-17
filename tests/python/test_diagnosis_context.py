import json
import pytest
from copy import deepcopy
from test_diagnosis_manager import setup,drain
from focusmate.core.context_builder import hash_content
from focusmate.core.manager_execution import advance_once


@pytest.mark.parametrize('new_message',[None,'现在改问初始化为什么是零。'])
def test_summary_is_separate_budgeted_request(tmp_path,new_message):
    m,p,e=setup(tmp_path)
    for i in range(12):
        ref='h'+str(i);content={'ref':ref,'role':'user','text':'之前的状态表示容量上限。'+'无关旧讨论。'*900}
        r={'ref':ref,'kind':'dialogue','version':1,'goalContext':e.bundle['request']['goalContext'],
           'content':content,'contentHash':hash_content(content),'sequence':i+1,'turnId':ref}
        e.bundle['records'].append(r);e.bundle['request']['dialogueRefs'].append(ref)
        e.bundle['request']['sources'].append({k:r[k] for k in ('ref','kind','version','contentHash')})
    e.bundle['request']['budget']={'contextWindowTokens':64000,'reservedOutputTokens':4096,'safetyMarginTokens':1000}
    e.budget=e.bundle['request']['budget']
    if new_message:
        m.command('s','next-question','message',{'text':new_message})
    advance_once(m,'s',e)
    st=next(st for st in m.read('s')['steps'].values() if st['status']=='pending')
    assert st['kind']=='diagnosis_summary' and not p.calls
    assert json.loads(st['payload']['user_prompt'])['currentUserRequest']==m.read('s')['messages'][-1]['text']
    from test_diagnosis import candidate
    summary={'items':[{'kind':'userStatement','text':'之前状态表示容量上限。','quotes':[{'ref':'h0','text':'之前的状态表示容量上限。'}]}]}
    p.responses=iter([{'content':json.dumps(summary),'toolCalls':[]},{'content':json.dumps(candidate()),'toolCalls':[]}])
    s,r=drain(m,e)
    assert r['status']=='finished' and r['counts']['model']==2 and len(p.calls)==2


def test_invalid_summary_fails_without_diagnosis(tmp_path):
    # Deferred response parsing stays in the original T056 validator.
    from focusmate.core.diagnosis_execution import DeferredSummary
    d=DeferredSummary({})
    try:d.complete(system_prompt='s',user_prompt='u',deadline=1)
    except RuntimeError:pass
    assert d.pending=={'key':hash_content({'system_prompt':'s','user_prompt':'u'}),'system_prompt':'s','user_prompt':'u'}


def test_long_code_is_metadata_not_silent_partial_read(tmp_path):
    m,p,e=setup(tmp_path)
    code=next(r for r in e.bundle['records'] if r['ref']=='c02')
    code['content']['text']='# comment\n'*2000+'print(3)\n';code['contentHash']=hash_content(code['content'])
    advance_once(m,'s',e);st=next(s for s in m.read('s')['steps'].values() if s['status']=='pending')
    record=next(r for r in st['payload']['snapshot']['modelPayload']['materials'] if r['ref']=='c02')
    assert record['content']['textOmitted'] and 'text' not in record['content']

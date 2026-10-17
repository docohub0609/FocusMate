from datetime import datetime, timezone
import hashlib
import pytest

from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.session_manager import SessionManager
from focusmate.core.manager_execution import ManagerExecutor, advance_once
from focusmate.core.tool_gateway import ToolGateway


class Backend:
    def load_code(self,ctx,version):
        code='print(1)\n'
        return {'codeVersion':version,'sourceCode':code,'contentHash':hashlib.sha256(code.encode()).hexdigest()}


class Provider:
    public_config={'maxTokens':100,'provider':'Fake'}
    def __init__(self, responses):self.responses=iter(responses);self.calls=[]
    def complete(self,**kwargs):self.calls.append(kwargs);return next(self.responses)


def build(tmp_path, responses, fixture=False):
    m=SessionManager(SQLiteManagerStore(tmp_path/'manager.db'))
    m.create('s',{'goalId':'g','scope':'goal'},{'problemId':'p','problemVersion':1},contract_version=1,code_version=3,allowed_tools=['read_code_excerpt'])
    m.command('s','ask','ask',{'text':'解释第一行'})
    provider=Provider(responses);backend=Backend()
    executor=ManagerExecutor(provider,ToolGateway(backend,SQLiteToolLedger(tmp_path/'tools.db')),backend,fixture_roles=fixture)
    return m,provider,executor


def test_model_tool_model_are_individual_durable_steps(tmp_path):
    call={'callId':'a','toolName':'read_code_excerpt','arguments':{'codeVersion':3,'startLine':1,'endLine':1}}
    m,p,e=build(tmp_path,[{'content':'','toolCalls':[call]},{'content':'这一行打印1。','toolCalls':[]}],True)
    advance_once(m,'s',e); assert len(p.calls)==1
    assert len(m.read('s')['steps'])==2
    advance_once(m,'s',e); assert len(p.calls)==1
    advance_once(m,'s',e); assert len(p.calls)==2
    assert any(msg.get('tool_call_id')=='a' for msg in p.calls[1]['messages'])
    advance_once(m,'s',e)
    assert m.publish('s')['source']=='developerFixture'
    assert all('contextSnapshot' in st['result'] for st in m.read('s')['steps'].values() if st['kind']=='model')


def test_real_candidate_not_published_without_integrated_checker(tmp_path):
    m,p,e=build(tmp_path,[{'content':'解释','toolCalls':[]}])
    advance_once(m,'s',e);advance_once(m,'s',e)
    assert m.publish('s') is None
    r=m.read('s')['rounds'][m.read('s')['currentRoundId']]
    assert r['failure']=='CHECKER_NOT_INTEGRATED' and r['candidate']=='解释'


def test_input_budget_error_makes_no_model_request(tmp_path):
    m,p,e=build(tmp_path,[])
    e.budget={'contextWindowTokens':100,'reservedOutputTokens':50,'safetyMarginTokens':10}
    advance_once(m,'s',e)
    assert not p.calls
    assert m.read('s')['rounds'][m.read('s')['currentRoundId']]['failure']=='CONTEXT_BUILD_FAILED'


def test_reopening_worker_cannot_silently_replace_materials(tmp_path):
    m,p,e=build(tmp_path,[{'content':'解释','toolCalls':[]}])
    e.materials={'goalRequirement':'原要求'}
    advance_once(m,'s',e)
    e.materials={'goalRequirement':'换掉要求'}
    with pytest.raises(Exception,match='MATERIALS_CHANGED'):
        advance_once(m,'s',e)

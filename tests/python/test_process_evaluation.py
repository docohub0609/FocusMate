import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from process_cases import development_cases,materials
from process_evaluation import run_case,save


class ReadProvider:
    public_config={'model':'fake','maxTokens':1024}
    def __init__(self):self.calls=[]
    def complete(self,**kw):
        self.calls.append(kw)
        data=json.loads(kw['messages'][1]['content'])
        assert 'expected' not in data and 'name' not in data
        if len(self.calls)==1:
            return {'content':'','toolCalls':[{'callId':'read1','toolName':'read_code_excerpt',
                'arguments':{'codeVersion':1,'startLine':1,'endLine':2}}]}
        assert kw['messages'][-1]['tool_call_id']=='read1'
        evidence=next(r for r in data['materials'] if r['ref']=='tool:read1')
        assert 'x=1' in json.dumps(evidence)
        return {'content':json.dumps({'checks':[{'checkId':c['checkId'],'status':'passed',
            'reason':'两行代码将x增加到2。','quotes':[{'ref':'tool:read1','text':'x=1'}],
            'evidenceType':'codeStructureCheck','limitations':['只检查这两行']} for c in data['requirements']]}),'toolCalls':[]}


def test_real_read_gateway_continuation_uses_shared_budget_and_paired_receipt(tmp_path):
    case={'requirements':['代码把x从1增加到2'],'explanation':'请检查','code':'x=1\nx+=1\n',
          'tools':['read_code_excerpt'],'expected':['passed']}
    p=ReadProvider();result=run_case(case,p,tmp_path/'run')
    assert result['verdict']=='passed' and result['calls']=={'model':2,'tool':1,'execution':0}
    assert len(p.calls)==2
    saved=json.loads((tmp_path/'run/result.json').read_text(encoding='utf-8'))
    assert saved['businessUnchanged']


def test_expectations_and_labels_never_enter_model_materials():
    case=development_cases()[0];a=materials(case)
    case.update(expected=['forbidden-secret'],name='expected-failure-secret')
    assert materials(case)==a


def test_result_history_is_never_overwritten(tmp_path):
    p=tmp_path/'result.json';save(p,{'first':True})
    with pytest.raises(FileExistsError):save(p,{'first':False})
    assert json.loads(p.read_text())=={'first':True}

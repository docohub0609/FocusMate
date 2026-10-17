"""Code evidence selects saved lines; the model does not copy indentation."""
from copy import deepcopy
import pytest
from tests.python.test_process_verification import prepared, judgment
from focusmate.core.context_builder import hash_content
from focusmate.core.coach_condition_verification import finish_condition_verification


def case():
    b=prepared()
    code=next(x for x in b['records'] if x['ref']=='c02')
    code['content']['text']='def solve(n):\n    dp=[False]*(n+1)\n    dp[0]=True\n    return dp\n'
    code['contentHash']=hash_content(code['content'])
    b['subgoal']['acceptableEvidence'].append('codeStructureCheck')
    value=judgment(b,('passed','passed'))
    for item in value['checks']:
        item.update(conditionStatus='valid',evidenceType='codeStructureCheck',
                    quotes=[{'codeRef':'c02','startLine':2,'endLine':3}])
    return b,value


def test_saved_code_range_preserves_indentation_and_version():
    b,value=case();original=deepcopy(value)
    report=finish_condition_verification(value,b)
    assert report['checks'][0]['quotes']==[{'ref':'c02','text':'    dp=[False]*(n+1)\n    dp[0]=True\n'}]
    assert report['verifierResult']['inputVersionRefs']['codeSnapshotRef']=='c02'
    assert value==original


@pytest.mark.parametrize('quote',[
    {'codeRef':'missing','startLine':2,'endLine':3},
    {'codeRef':'u01','startLine':1,'endLine':1},
    {'codeRef':'c02','startLine':3,'endLine':2},
    {'codeRef':'c02','startLine':1,'endLine':99},
])
def test_invalid_ranges_cannot_become_evidence(quote):
    b,value=case();value['checks'][0]['quotes']=[quote]
    with pytest.raises(ValueError,match='INVALID_CODE_RANGE'):
        finish_condition_verification(value,b)


def test_plain_quote_with_missing_indent_is_still_rejected():
    b,value=case()
    for item in value['checks']:
        item['quotes']=[{'ref':'c02','text':'dp=[False]*(n+1)\ndp[0]=True'}]
    with pytest.raises(ValueError,match='INVALID_EVIDENCE_QUOTE'):
        finish_condition_verification(value,b)


def test_old_code_and_unloaded_source_cannot_be_selected():
    b,value=case();b['request']['currentCodeRef']='other'
    with pytest.raises(ValueError,match='INVALID_CODE_RANGE'):
        finish_condition_verification(value,b)
    b,value=case()
    next(x for x in b['records'] if x['ref']=='c02')['content']['text']='x=0\n'*3000
    with pytest.raises(ValueError,match='INVALID_CODE_RANGE'):
        finish_condition_verification(value,b)


def test_v3_request_and_executor_accept_line_selection(tmp_path):
    from tests.python.test_dynamic_coach_manager import manager
    from tests.python.test_coach_v3_flow import establish
    from tests.python.test_coach_compact_flow import run_step
    from jsonschema import Draft202012Validator
    m=manager(tmp_path,workflow_version=3);key=establish(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    def reply(payload):
        assert payload['codeLineCatalog'][0]['lines'][2]['text']=='    print(value)\n'
        value={'checks':[{'checkId':x['checkId'],'conditionStatus':'valid','status':'passed',
            'reason':'模拟检查循环包含末元素。','limitations':['未运行'],
            'evidenceType':'codeStructureCheck',
            'quotes':[{'codeRef':'code:1','startLine':2,'endLine':3}]} for x in payload['requirements']]}
        Draft202012Validator(payload['outputSchema']).validate(value)
        return value
    run_step(m,reply)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==1
    assert s['checks'][r['reportRefs'][0]]['checks'][0]['quotes'][0]['text']=='for value in numbers:\n    print(value)\n'

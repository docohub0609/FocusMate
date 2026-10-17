import json
from copy import deepcopy
import pytest
from test_action_manager import setup
from test_action_policy import answer
from test_action_review import review
from test_manager_execution import Provider
from test_diagnosis_manager import drain


def test_review_payload_removes_only_generation_instructions():
    from focusmate.core.action_quality import review_materials, review_instructions
    source={'materials':[{'ref':'code','text':'x=1'}],'dialogue':[{'text':'previous hint'}],
            'publishedActions':[{'content':'old hint'}],'diagnosis':{'questions':[]},
            'selectedSkill':{'content':'scope'},'missing':['outer code'],
            'outputSchema':{'actor':True},'availableTools':[{'name':'run'}]}
    frozen=deepcopy(source);result=review_materials(source)
    assert source==frozen
    assert set(result)==set(source)-{'outputSchema','availableTools'}
    for key in result:assert result[key]==source[key]
    assert 'previousIssues' not in review_instructions([])
    assert 'previousIssues' in review_instructions([{'issueId':'x'}])


def test_independent_reviewer_receives_no_tools_and_same_evidence(tmp_path):
    from focusmate.core.action_quality import FocusedActionExecutor
    m,p,old=setup(tmp_path,[answer()]);rp=Provider([{'content':json.dumps(review()),'toolCalls':[]}])
    e=FocusedActionExecutor(p,None,None,bundle=old.bundle,skills=[],diagnosis=old.seed,review_provider=rp)
    state,r=drain(m,e)
    assert r['status']=='ready_to_publish' and len(p.calls)==len(rp.calls)==1
    assert r['counts']['model']==2 and rp.calls[0]['tools']==[]
    source=json.loads(p.calls[0]['messages'][1]['content']);got=json.loads(rp.calls[0]['messages'][1]['content'])
    assert got['materials']==source['materials']
    assert 'outputSchema' not in got and 'availableTools' not in got
    assert 'issueChecks' not in got['reviewSchema']['properties']


def test_repair_has_own_prompt_rechecks_all_issues_and_can_retract(tmp_path):
    from focusmate.core.action_quality import FocusedActionExecutor, REPAIR_PROMPT
    from focusmate.core.action_planning import review_issues
    failed=review('fail');issues=review_issues(failed,answer());second=review()
    second['issueChecks']=[{'issueId':i['issueId'],'status':'initialReviewIncorrect',
                           'reason':'原代码支持原回答。','materialRefs':['c02']} for i in issues]
    m,p,old=setup(tmp_path,[answer(),answer()])
    rp=Provider([{'content':json.dumps(v),'toolCalls':[]} for v in [failed,second]])
    e=FocusedActionExecutor(p,None,None,bundle=old.bundle,skills=[],diagnosis=old.seed,review_provider=rp)
    state,r=drain(m,e)
    assert r['status']=='ready_to_publish' and r['counts']['model']==4
    assert p.calls[1]['messages'][0]['content']==REPAIR_PROMPT
    assert json.loads(p.calls[1]['messages'][1]['content'])['previousIssues']==issues
    assert json.loads(rp.calls[1]['messages'][1]['content'])['previousIssues']==issues
    assert 'issueChecks' in json.loads(rp.calls[1]['messages'][1]['content'])['reviewSchema']['required']


def test_reviewer_own_output_budget_checked_before_call(tmp_path):
    from focusmate.core.action_quality import FocusedActionExecutor
    m,p,old=setup(tmp_path,[answer()]);rp=Provider([])
    rp.public_config={'maxTokens':old.bundle['request']['budget']['reservedOutputTokens']+1}
    e=FocusedActionExecutor(p,None,None,bundle=old.bundle,skills=[],diagnosis=old.seed,review_provider=rp)
    _,r=drain(m,e)
    assert r['failure']=='OUTPUT_RESERVE_TOO_SMALL' and not rp.calls

from copy import deepcopy
import pytest
from test_action_policy import answer
from focusmate.core.action_planning import REVIEW_KEYS, validate_action_review, review_transition


def review(verdict='pass'):
    return {'checks':{k:{'verdict':verdict,'reason':'对照当前材料核对。',
                        'candidateQuote':answer()['content'],'materialRefs':['c02']} for k in REVIEW_KEYS}}


def test_complete_review_and_single_repair_budget():
    assert review_transition(review(),answer(),['c02'],repaired=False,remaining_model_calls=0)=='accept'
    assert review_transition(review('fail'),answer(),['c02'],repaired=False,remaining_model_calls=2)=='repair'
    assert review_transition(review('fail'),answer(),['c02'],repaired=False,remaining_model_calls=1)=='reject'
    assert review_transition(review('fail'),answer(),['c02'],repaired=True,remaining_model_calls=7)=='reject'


def test_uncertain_is_neither_acceptance_nor_repair():
    r=review('fail');r['checks']['fact']['verdict']='uncertain'
    assert review_transition(r,answer(),['c02'],repaired=False,remaining_model_calls=7)=='uncertain'


def test_permission_review_can_quote_candidate_request_quote():
    c=answer();c['requestQuotes']=[{'ref':'u01','text':'请只解释原因。'}]
    r=review();r['checks']['permission']['candidateQuote']='请只解释原因。'
    assert validate_action_review(r,c,['c02'])=='passed'


@pytest.mark.parametrize('fault',['missing','unknownRef','inventedQuote','noErrorLocation'])
def test_incomplete_review_cannot_trigger_correction(fault):
    r=review('fail')
    if fault=='missing':del r['checks']['fact']
    if fault=='unknownRef':r['checks']['fact']['materialRefs']=['invented']
    if fault=='inventedQuote':r['checks']['fact']['candidateQuote']='模型没有说过'
    if fault=='noErrorLocation':r['checks']['fact']['candidateQuote']=''
    with pytest.raises(ValueError):review_transition(r,answer(),['c02'],repaired=False,remaining_model_calls=7)


def test_issue_list_is_program_owned_and_preserves_original_evidence():
    from focusmate.core.action_planning import review_issues
    r=review();r['checks']['fact']['verdict']='fail'
    issues=review_issues(r,answer())
    assert len(issues)==1 and issues[0]['sourceCheck']=='fact'
    assert issues[0]['candidateQuote']==answer()['content']
    assert issues[0]['materialRefs']==['c02']
    assert issues==review_issues(r,answer())


@pytest.mark.parametrize('fault',['missing','duplicate','invented','badReference'])
def test_second_review_must_cover_exact_prior_issues(fault):
    issues=[{'issueId':'issue-a'}]
    item={'issueId':'issue-a','status':'resolved','reason':'正文已改正。','materialRefs':['c02']}
    r=review();r['issueChecks']=[item]
    if fault=='missing':r['issueChecks']=[]
    if fault=='duplicate':r['issueChecks'].append(deepcopy(item))
    if fault=='invented':item['issueId']='issue-b'
    if fault=='badReference':item['materialRefs']=['fake']
    with pytest.raises(ValueError):
        review_transition(r,answer(),['c02'],repaired=True,remaining_model_calls=0,previous_issues=issues)


@pytest.mark.parametrize('status,expected',[('resolved','accept'),('initialReviewIncorrect','accept'),('stillPresent','reject'),('uncertain','uncertain')])
def test_prior_issue_outcomes_affect_acceptance(status,expected):
    r=review();r['issueChecks']=[{'issueId':'issue-a','status':status,'reason':'对照源码检查正文。','materialRefs':['c02']}]
    assert review_transition(r,answer(),['c02'],repaired=True,remaining_model_calls=0,
                             previous_issues=[{'issueId':'issue-a'}])==expected

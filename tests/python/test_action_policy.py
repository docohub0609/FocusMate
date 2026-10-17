from copy import deepcopy
import pytest
from focusmate.contracts.schema_validation import validate
from focusmate.core.action_planning import REQUEST_POLICY, validate_action_candidate
from test_diagnosis import inputs,candidate
from focusmate.core.diagnosis import prepare_diagnosis,validate_diagnosis
from focusmate.core.context_builder import MapContextResolver,hash_content


def data(full=False):
    req,records=inputs()
    if full:
        records[0]['content']['coachPolicy']=deepcopy(REQUEST_POLICY)
        records[-1]['content']['text']='按我的思路给完整代码。'
        for r,s in zip(records,req['sources']):
            r['contentHash']=hash_content(r['content']);s['contentHash']=r['contentHash']
    b=prepare_diagnosis(req,MapContextResolver(records));d=validate_diagnosis(candidate(),b)
    return b,d


def answer():
    return {'questionKeys':['q1'],'subgoalIds':['s1'],'actionType':'hint','hintLevel':2,
        'answerScope':'guided','reason':'用户在询问更新含义。','evidenceRefs':['u01','c02'],
        'requestQuotes':[],'content':'先看当前更新读取的是哪一轮状态。','diagnosisCorrections':[],
        'followUp':{'kind':'awaitEvidence','reason':'用户可以解释更新依据。','criterionIds':['s1'],
                    'checks':['userExplanation'],'expectedUserAction':'说明读取哪一轮状态。'}}


def test_default_and_request_policy_are_both_valid():
    b,d=data(True);validate('GoalContract',b['records'][0]['content'])
    c=answer();c.update(answerScope='fullSolution',requestQuotes=[{'ref':'u01','text':'按我的思路给完整代码。'}])
    assert validate_action_candidate(c,b,d)==c


@pytest.mark.parametrize('fault',['strict','noQuote','oldQuote','badTarget','badRef','fakeCheck'])
def test_illegal_actions_rejected(fault):
    b,d=data(fault!='strict');c=answer()
    c.update(answerScope='fullSolution',requestQuotes=[{'ref':'u01','text':'按我的思路给完整代码。'}])
    if fault=='noQuote':c['requestQuotes']=[]
    if fault=='oldQuote':c['requestQuotes'][0]['text']='不存在的原话'
    if fault=='badTarget':c['subgoalIds']=['fake']
    if fault=='badRef':c['evidenceRefs']=['fake']
    if fault=='fakeCheck':c['followUp']['checks']=['execute_anything']
    with pytest.raises(ValueError):validate_action_candidate(c,b,d)


def test_clarification_does_not_invent_verification():
    b,d=data();c=answer();c['actionType']='clarificationRequest'
    c['followUp']={'kind':'awaitInformation','reason':'需要具体输入','criterionIds':[],
                   'checks':[],'expectedUserAction':'提供触发错误的输入'}
    assert validate_action_candidate(c,b,d)['followUp']['checks']==[]


def test_no_followup_does_not_require_an_invented_user_task():
    b,d=data();c=answer()
    c['followUp']={'kind':'none','reason':'本次问题已答清楚。','criterionIds':[],
                   'checks':[],'expectedUserAction':''}
    assert validate_action_candidate(c,b,d)['followUp']['expectedUserAction']==''

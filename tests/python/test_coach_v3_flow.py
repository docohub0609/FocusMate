"""Deterministic replies test workflow ownership, not model quality."""
import pytest
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_coach_compact_flow import compact, run_step, checked


def help_reply(key=None, create=False):
    value=compact(create=create)
    if key:value['targets']=[{'key':key,'followUp':False}]
    return value


def establish(m):
    m.command('s','code','code_changed',{'codeVersion':1})
    m.command('s','ask','ask',{'text':'为什么漏掉末元素？改后帮我检查。'})
    run_step(m,help_reply('new:last',True))
    return next(iter(m.read('s')['workGoals']))


def result(status, condition='valid'):
    def reply(payload):
        value=checked(status)(payload)
        for item in value['checks']:item['conditionStatus']=condition
        return value
    return reply


def test_help_is_one_call_and_not_claimed_reviewed(tmp_path):
    m=manager(tmp_path,workflow_version=3);establish(m)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==1
    assert s['outbox'][0]['learningAction']['semanticStatus']=='modelOpinionNotVerified'
    assert s['decisions'][-1]['review'] is None


def test_check_pass_is_one_call_and_retains_condition_assessment(tmp_path):
    m=manager(tmp_path,workflow_version=3);key=establish(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,result('passed'))
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==1
    assert s['checks'][r['reportRefs'][0]]['checks'][0]['conditionStatus']=='valid'
    assert s['workGoals'][key]['status']=='verified' and not s['completed']


@pytest.mark.parametrize('condition',['invalid','uncertain'])
def test_unsuitable_condition_cannot_pass(tmp_path,condition):
    m=manager(tmp_path,workflow_version=3);key=establish(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,result('passed',condition))
    report=next(iter(m.read('s')['checks'].values()))
    assert report['checks'][0]['status']=='inconclusive'
    assert report['checks'][0]['conditionStatus']==condition
    assert m.read('s')['workGoals'][key]['status']=='active'


def test_failed_check_then_help_uses_two_calls(tmp_path):
    m=manager(tmp_path,workflow_version=3);key=establish(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,result('failed'));run_step(m,help_reply(key))
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['status']=='finished' and r['counts']['model']==2
    assert len(s['checks'])==1 and len(s['outbox'])==2


def test_legacy_round_keeps_review_when_manager_is_upgraded(tmp_path):
    m=manager(tmp_path,workflow_version=2)
    m.command('s','ask','ask',{'text':'解释循环'})
    m.workflow_version=3
    run_step(m,compact())
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['workflowVersion']==2 and r['phase']=='review'
    assert not s['outbox']


def test_full_answer_still_waits_for_independent_review(tmp_path,monkeypatch):
    import tests.python.test_coach_compact_flow as helper
    original=helper.materials
    def permitted(m):
        data=original(m)
        contract=data['records'][0]
        from focusmate.core.action_planning import REQUEST_POLICY
        contract['content']['coachPolicy']=REQUEST_POLICY
        from focusmate.core.context_builder import hash_content
        contract['contentHash']=hash_content(contract['content'])
        return data
    monkeypatch.setattr(helper,'materials',permitted)
    m=manager(tmp_path,workflow_version=3)
    m.command('s','ask','ask',{'text':'请给完整代码'})
    value=compact();value['help'].update(answerScope='fullSolution',requestQuotes=[{'ref':'message:1','text':'请给完整代码'}])
    run_step(m,value)
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert r['phase']=='review' and not s['outbox']


def test_single_target_table_preserves_followup_subset():
    from focusmate.core.coach_compact_wire import normalize_compact_plan
    from jsonschema import ValidationError
    value=compact()
    value['targets']=[{'key':'one','followUp':False},{'key':'two','followUp':True}]
    value['help']['followUp']={'kind':'awaitEvidence','reason':'等修改','expectedUserAction':'改循环','checks':['codeStructure']}
    action=normalize_compact_plan(value,{},workflow_version=3)['action']
    assert action['subgoalIds']==['one','two'] and action['followUp']['criterionIds']==['two']
    value['targets'].append({'key':'one','followUp':True})
    with pytest.raises(ValidationError,match='DUPLICATE_TARGET_KEY'):
        normalize_compact_plan(value,{},workflow_version=3)


def test_invalid_condition_is_reported_without_changing_goal(tmp_path):
    m=manager(tmp_path,workflow_version=3);key=establish(m)
    before=m.read('s')['workGoals'][key]['versions']
    m.command('s','check','check',{'subgoalIds':[key]})
    run_step(m,result('inconclusive','invalid'))
    s=m.read('s');r=s['rounds'][s['currentRoundId']]
    assert s['workGoals'][key]['versions']==before and r['phase']=='plan'
    assert len(s['checks'])==1 and r['checkBatchDone']


def test_explicit_followup_selection_does_not_need_duplicate_metadata(tmp_path):
    m=manager(tmp_path,workflow_version=3)
    m.command('s','ask','ask',{'text':'给局部提示，留作修改后检查，不要执行。'})
    value=compact(create=True)
    value['targets']=[{'key':'new:last','followUp':True}]
    run_step(m,value)
    s=m.read('s');action=s['outbox'][0]['learningAction']
    assert action['followUp']['kind']=='awaitEvidence'
    assert action['followUp']['criterionIds']==action['subgoalIds']
    assert action['followUp']['checks']==['codeStructure']
    assert s['rounds'][s['currentRoundId']]['counts']=={'model':1,'tool':0,'execution':0}

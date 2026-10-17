from apps.api.coach_presentation import source_ranges


def test_range_requires_exact_unambiguous_saved_code():
    assert source_ranges('a=1\nprint(a)\n','看这里：\n```python\nprint(a)\n```')==[
        {'startLine':2,'endLine':2,'text':'print(a)'}]
    assert source_ranges('print(a)\nprint(a)\n','```python\nprint(a)\n```')==[]
    assert source_ranges('print(a)\n','```python\nprint(b)\n```')==[]


def test_failed_following_plan_exposes_saved_checks_without_raw_reply():
    from apps.api.coach_presentation import round_failure
    r={'status':'failed','phase':'plan','failure':'MODEL_RESPONSE_INVALID',
       'reportRefs':['report-1'],'counts':{'model':7},'responseText':'PRIVATE RAW RESPONSE'}
    result=round_failure(r,7)
    assert result['stage']=='plan' and result['savedReportCount']==1
    assert result['remainingModelCalls']==0
    assert '后续建议' in result['message'] and 'PRIVATE' not in str(result)


def test_unchecked_summary_uses_pinned_goal_version_and_safe_reason():
    from apps.api.coach_presentation import check_summary, action_content
    r={'roundId':'r1','batchId':'b1','codeVersion':2,'status':'finished','reportRefs':[],
       'uncheckedTargets':[{'subgoalId':'g1','version':1,'reason':'MODEL_BUDGET_RESERVED'}]}
    goals={'g1':{'versions':[{'version':1,'objective':'修正列循环范围'},
                             {'version':2,'objective':'已经换成别的问题'}]}}
    summary=check_summary(r,goals,{})
    assert summary['status']=='incomplete' and summary['codeVersion']==2
    assert summary['reportIds']==[] and summary['savedReportCount']==0
    assert summary['targets'][0]['objective']=='修正列循环范围'
    assert summary['targets'][0]['workGoalVersion']==1
    assert '额度不足' in summary['targets'][0]['reason']
    content=action_content('从代码结构看，问题已经解决。',summary)
    assert content.index('正式检查未完成') < content.index('从代码结构看')
    assert '修正列循环范围' in content and '不代表这些工作目标已验证通过' in content


def test_check_summary_distinguishes_saved_nonpassing_reports_and_unchecked_targets():
    from apps.api.coach_presentation import check_summary
    r={'roundId':'r','batchId':'b','codeVersion':3,'status':'finished','reportRefs':['p1','p2'],
       'uncheckedTargets':[{'subgoalId':'g3','version':1,'reason':'DEPENDENCY_NOT_VERIFIED',
                            'dependencies':['g2'],'privateDebug':'PRIVATE RAW'}]}
    goals={f'g{i}':{'versions':[{'version':1,'objective':f'问题{i}'}]} for i in range(1,4)}
    checks={f'p{i}':{'workGoalVersion':1,'verifierResult':{'goalContext':{'subgoalId':f'g{i}'},
            'status':status}} for i,status in ((1,'failed'),(2,'notReady'))}
    summary=check_summary(r,goals,checks)
    assert summary['status']=='incomplete' and summary['savedReportCount']==2
    assert summary['uncheckedTargetCount']==1
    assert {t['subgoalId']:t['reportStatuses'] for t in summary['targets']}=={
        'g1':['failed'],'g2':['notReady'],'g3':[]}
    assert 'PRIVATE' not in str(summary)
    assert summary['targets'][-1]['reason']=='依赖的工作目标尚未验证通过，本目标未生成正式检查报告。'
    r['uncheckedTargets']=[]
    completed=check_summary(r,goals,checks)
    assert completed['status']=='completed'
    assert '不代表所有目标都已通过' in completed['message']


def test_pending_or_stopped_check_is_not_silently_presented_as_completed():
    from apps.api.coach_presentation import check_summary
    r={'roundId':'r','batchId':'b','codeVersion':1,'status':'processing','reportRefs':[],'checkQueue':[]}
    goals={'g':{'versions':[{'version':1,'objective':'边界问题'}]}}
    steps={'s':{'roundId':'r','kind':'verification','payload':{'target':{'subgoalId':'g','version':1}}}}
    assert check_summary(r,goals,{},steps)['status']=='inProgress'
    r.update(status='failed',failure='MODEL_RESPONSE_INVALID')
    summary=check_summary(r,goals,{},steps)
    assert summary['status']=='incomplete' and summary['uncheckedTargetCount']==1
    assert summary['targets'][0]['reasonCode']=='MODEL_RESPONSE_INVALID'
    assert '格式' in summary['targets'][0]['reason']


def test_report_only_projects_formal_facts_and_rechecked_user_authored_quotes():
    from apps.api.coach_presentation import safe_report
    report = {'reportId':'p','batchId':'b','workGoalVersion':1,
        'verifierResult':{'goalContext':{'subgoalId':'g'},'status':'passed',
            'inputVersionRefs':{'codeVersion':2},'evidenceRefs':['code:2'],
            'reason':'UNREVIEWED FULL ANSWER','createdAt':'2026-10-14T00:00:00Z'},
        'requirements':[{'checkId':'c','requirement':'最后一个元素也被处理'}],
        'checks':[{'checkId':'c','status':'passed','reason':'UNREVIEWED FULL ANSWER',
            'limitations':['UNREVIEWED LIMITATION'],'quotes':[
                {'ref':'code:2','text':'print(values[-1])'},
                {'ref':'code:2','text':'invented complete answer'},
                {'ref':'process:p1','text':'OLD UNREVIEWED ANSWER'},
                {'ref':'message:1','text':'帮我检查末元素'}]}]}
    state={'workGoals':{'g':{'versions':[{'version':1,'objective':'末元素漏算'}]}},
           'messages':[{'sequence':1,'text':'帮我检查末元素'}]}
    projected=safe_report(report,state=state,source_for_version=lambda v:'print(values[-1])' if v==2 else None)
    assert projected['result']['status']=='passed'
    assert projected['checks'][0]['quotes'][0]['text']=='print(values[-1])'
    assert projected['checks'][0]['quotes'][3]['text']=='帮我检查末元素'
    assert projected['checks'][0]['quotes'][1]['text'] is None
    assert projected['checks'][0]['quotes'][2]['text'] is None
    assert 'UNREVIEWED' not in str(projected) and 'invented complete answer' not in str(projected)

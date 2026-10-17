"""Safe user-facing summaries, always tied to the saved round and code."""
import re
from copy import deepcopy


def round_failure(round_state, model_limit):
    if not round_state or round_state.get('status') not in ('failed','expired','interrupted'):return None
    phase=round_state.get('phase')
    names={'diagnosis':'理解问题','plan':'生成后续建议','review':'审核建议','verification':'检查工作目标','tool':'获取工具结果','skill':'读取检查方法','repair':'自动修复'}
    if phase=='plan' and not round_state.get('reportRefs'):names['plan']='准备本次提示'
    code=round_state.get('failure')
    reason={'BUDGET_EXHAUSTED':'本轮调用额度不足','DEADLINE_EXCEEDED':'本轮时间已用完',
            'MODEL_RESPONSE_INVALID':'AI回复格式未通过校验','UNNECESSARY_VERIFICATION':'AI把补充信息与目标验证混在了一起，修正后仍不符合要求','INVALID_REVIEW_REFERENCE':'审核引用了当前材料中不存在的编号',
            'REVIEW_NOT_PASSED':'目标或建议未通过审核','STEP_OWNER_LOST':'后台处理已中断',
            'PROVIDER_PAYMENT_REQUIRED':'模型服务报告账户额度不足','PROVIDER_UNAVAILABLE':'模型服务暂不可用'}.get(code,'本步骤未形成可接收的结果')
    from .repair_presentation import safe_repair
    repair=safe_repair(round_state.get('repairRecord'))
    if repair and repair['status'] in ('failed','abstained','interrupted','exhausted'):
        reason=repair['reason'].rstrip('。')
    saved=len(round_state.get('reportRefs',[]))
    return {'stage':phase,'message':names.get(phase,'后台处理')+'未完成：'+reason+'。'+
            ('已保存的检查报告仍可查看。' if saved else '本轮尚未保存检查报告。'),
            'savedReportCount':saved,'remainingModelCalls':max(0,model_limit-round_state.get('counts',{}).get('model',0))}


def check_summary(round_state, work_goals, reports, steps=None):
    """Finished computation is distinct from producing reports for all targets."""
    if not round_state or not round_state.get('batchId'):
        return None
    targets = {}

    def target(subgoal_id, version):
        key = (subgoal_id, version)
        if key not in targets:
            goal_version = next((v for v in work_goals.get(subgoal_id, {}).get('versions', [])
                                 if v['version'] == version), {})
            targets[key] = {'subgoalId': subgoal_id, 'workGoalVersion': version,
                            'objective': goal_version.get('objective'), 'status': 'pending',
                            'reportIds': [], 'reportStatuses': [], 'reasonCode': None, 'reason': None}
        return targets[key]

    saved = []
    for ref in round_state.get('reportRefs', []):
        report = reports.get(ref)
        if not report:
            continue
        result = report['verifierResult']
        item = target(result['goalContext']['subgoalId'], report['workGoalVersion'])
        item['status'] = 'reported'
        item['reportIds'].append(ref)
        status = result.get('status')
        item['reportStatuses'].append(status if status in ('passed', 'failed', 'notReady', 'inconclusive') else 'unknown')
        saved.append(ref)

    for step in (steps or {}).values():
        if step.get('roundId') == round_state['roundId'] and step.get('kind') == 'verification':
            expected = step['payload']['target']
            target(expected['subgoalId'], expected['version'])
    for expected in round_state.get('checkQueue', []):
        target(expected['subgoalId'], expected['version'])

    reasons = {
        'MODEL_BUDGET_RESERVED': '本轮模型调用额度不足，未生成该目标的正式检查报告。',
        'BUDGET_EXHAUSTED': '本轮调用额度已用完，未生成该目标的正式检查报告。',
        'DEPENDENCY_NOT_VERIFIED': '依赖的工作目标尚未验证通过，本目标未生成正式检查报告。',
        'MODEL_RESPONSE_INVALID': 'AI回复格式未通过校验，未生成该目标的正式检查报告。',
        'DEADLINE_EXCEEDED': '本轮时间已用完，未生成该目标的正式检查报告。',
        'STEP_OWNER_LOST': '后台处理已中断，未生成该目标的正式检查报告。',
        'USER_CANCELLED': '本轮处理已取消，未生成该目标的正式检查报告。',
        'CODE_CHANGED': '代码已换版，本轮未生成该目标的正式检查报告。',
        'NEW_MESSAGE': '收到新的用户消息，本轮未生成该目标的正式检查报告。',
    }
    for expected in round_state.get('uncheckedTargets', []):
        item = target(expected['subgoalId'], expected['version'])
        item.update(status='unchecked', reasonCode=expected.get('reason'))

    terminal = round_state.get('status') in ('finished', 'failed', 'expired', 'interrupted', 'cancelled', 'superseded')
    for item in targets.values():
        if terminal and item['status'] == 'pending':
            item.update(status='unchecked', reasonCode=round_state.get('failure'))
        if item['status'] == 'unchecked':
            item['reason'] = reasons.get(item['reasonCode'], '本目标尚未形成正式检查报告。')
    unchecked = sum(item['status'] == 'unchecked' for item in targets.values())
    if not terminal:
        status, message = 'inProgress', '本轮仍在处理，请分别查看已保存的检查报告与待检查目标。'
    elif unchecked or not targets or len(saved) != len(round_state.get('reportRefs', [])):
        status = 'incomplete'
        message = '本次正式检查未完成。' + (f'已保存 {len(saved)} 份检查报告，其余目标未完成检查。' if saved else '本轮未生成正式检查报告。')
    else:
        status, message = 'completed', '本轮检查报告已保存；这不代表所有目标都已通过，请查看各报告的结论。'
    return {'batchId': round_state['batchId'], 'codeVersion': round_state.get('codeVersion'),
            'status': status, 'message': message, 'savedReportCount': len(saved), 'reportIds': saved,
            'uncheckedTargetCount': unchecked, 'targets': list(targets.values())}


def action_content(content, summary):
    """Annotate presentation without rewriting the persisted model action."""
    if not summary or summary['status'] != 'incomplete':
        return content
    reasons = '\n'.join('－' + (item['objective'] or '该工作目标') + '：' + item['reason']
                        for item in summary['targets'] if item['status'] == 'unchecked')
    notice = summary['message'] + ('\n' + reasons if reasons else '')
    return notice + '\n以下是教练根据现有材料给出的分析，不代表这些工作目标已验证通过。\n\n' + content


def source_ranges(source,text):
    ranges=[]
    for match in re.finditer(r'```(?:python|py)?\s*\n(.*?)\n```',text,re.S):
        excerpt=match.group(1)
        if not excerpt or source.count(excerpt)!=1:continue
        offset=source.index(excerpt)
        start=source[:offset].count('\n')+1
        ranges.append({'startLine':start,'endLine':start+excerpt.count('\n'),'text':excerpt})
    return ranges


CHECK_NOTICES = {
    'passed': '本项检查通过；结论只对应这次保存的代码与材料。',
    'failed': '本项检查未通过，请结合教练后续提示修改。',
    'notReady': '本项缺少可用依据，暂未判断代码是否正确。',
    'inconclusive': '现有材料尚不能支持明确结论。',
}


def goal_title(version):
    """Only abbreviate saved wording; never infer a new goal in a query."""
    text = version.get('title') or version.get('objective', '当前问题')
    return text if len(text) <= 24 else text[:23] + '…'


def safe_report(report, *, state, source_for_version):
    """A Verifier verdict is publishable; its unreviewed prose is not advice.

    References may point at other model reports. Those references remain visible,
    but their text is withheld. Only exact user-authored code/messages are quoted.
    """
    result = report['verifierResult']
    key = result['goalContext']['subgoalId']
    version = next((v for v in state['workGoals'].get(key, {}).get('versions', [])
                    if v['version'] == report['workGoalVersion']), {})
    visible_checks = []
    for check in report['checks']:
        quotes = []
        for quote in check.get('quotes', []):
            ref, text = quote.get('ref', ''), quote.get('text', '')
            original = None
            if re.fullmatch(r'code:[1-9][0-9]*', ref):
                original = source_for_version(int(ref.split(':')[1]))
            elif re.fullmatch(r'message:[1-9][0-9]*', ref):
                sequence = int(ref.split(':')[1])
                original = next((m['text'] for m in state['messages'] if m['sequence'] == sequence), None)
            verified = bool(text and original is not None and text in original)
            quotes.append({'ref': ref, 'text': text if verified else None,
                           'label': '用户代码' if ref.startswith('code:') else '用户原话' if ref.startswith('message:') else '已核对的材料引用'})
        visible_checks.append({'checkId': check['checkId'], 'status': check['status'],
            'conditionStatus': check.get('conditionStatus'),
            'notice': ('这条检查条件需要重新确认，暂未判断你的代码是否正确。' if check.get('conditionStatus') in ('invalid','uncertain') else CHECK_NOTICES.get(check['status'], '尚无可展示的检查结论。')),
            'evidenceType': check.get('evidenceType'), 'quotes': quotes})
    return {'kind': 'report', 'reportId': report['reportId'], 'batchId': report['batchId'],
        'goalContext': deepcopy(result['goalContext']),
        'roundId': report.get('roundId'), 'subgoalId': key, 'workGoalVersion': report['workGoalVersion'],
        'objective': version.get('objective', '历史问题'), 'title': goal_title(version),
        'codeVersion': result['inputVersionRefs'].get('codeVersion'),
        'result': {k: deepcopy(result[k]) for k in ('status', 'goalContext', 'inputVersionRefs', 'evidenceRefs') if k in result},
        'notice': CHECK_NOTICES.get(result['status'], '尚无可展示的检查结论。'),
        'checks': visible_checks, 'requirements': deepcopy(report['requirements']),
        'historySequence': report.get('historySequence'),
        'occurredAt': report.get('occurredAt', result['createdAt'])}


def goal_projection(state, goals):
    projected = deepcopy(goals)
    for goal in projected:
        goal['title'] = goal_title(goal)
        reports = [state['checks'][ref] for ref in state['workGoals'][goal['subgoalId']]['verificationRefs']
                   if ref in state['checks'] and state['checks'][ref]['workGoalVersion'] == goal['version']]
        report = reports[-1] if reports else None
        goal['latestCheck'] = ({'reportId': report['reportId'], 'status': report['verifierResult']['status'],
            'codeVersion': report['verifierResult']['inputVersionRefs'].get('codeVersion')}
            if report else None)
    return projected

"""Publish repair progress without exposing candidates or diagnostic materials."""

REPAIR_STATUSES = frozenset((
    'pending', 'running', 'submitted', 'validating', 'succeeded', 'failed',
    'abstained', 'interrupted', 'exhausted',
))

REPAIR_REASONS = {
    'MODEL_RESPONSE_INVALID': '修正后的回复格式仍未通过检查。',
    'INVALID_EVIDENCE_QUOTE': '修正后的代码引用仍与保存的代码不一致。',
    'INVALID_FOLLOWUP_TARGET': '修正后的建议仍未正确关联当前问题。',
    'INVALID_REVIEW_REFERENCE': '修正后的审核仍引用了不存在的材料。',
    'REVIEW_NOT_PASSED': '修正后的内容仍未通过原有审核。',
    'CANDIDATE_SCHEMA_INVALID': '修正后的测试生成器格式仍不符合要求。',
    'CANDIDATE_COMPILE_FAILED': '修正后的测试生成器仍有语法错误。',
    'GENERATOR_CASES_INVALID': '修正后的测试输入仍不符合要求。',
    'GENERATOR_NON_DETERMINISTIC': '修正后在相同条件下生成的输入仍不一致。',
    'GENERATOR_COVERAGE_INSUFFICIENT': '修正后的测试输入仍缺少必要的覆盖。',
    'SMALL_REFERENCE_TIMEOUT': '修正后的小数据仍未能在时限内完成参考检查。',
    'REFERENCE_EXECUTION_FAILED': '修正后的小数据仍未得到可比较的参考答案。',
    'REFERENCE_PUBLIC_MISMATCH': '参考程序与公开样例不一致，不能靠改测试输入消除。',
    'REFERENCE_DISAGREEMENT': '两个参考程序的答案不一致，不能靠改测试输入消除。',
    'GENERATOR_INPUT_LEGALITY_UNCONFIRMED': '修正后的测试输入仍未确认符合题目条件。',
    'REPAIR_SCOPE_VIOLATION': '修复请求超出了允许修改的内容，未被采用。',
    'REPAIR_CANDIDATE_INVALID': '修复返回的内容格式不符合要求，本次不再自动修复。',
    'REPAIR_REPLY_INVALID': '修复未返回可检查的内容，本次不再自动修复。',
    'REPAIR_TOOL_DENIED': '修复请求超出了允许使用的工具或材料，未被执行。',
    'REPAIR_PROVIDER_FAILED': '修复时模型服务调用失败，原任务结果仍保留。',
    'REPAIR_BUDGET_EXHAUSTED': '本次剩余调用额度或时间不足，自动修复已停止。',
    'BUDGET_EXHAUSTED': '本次调用额度不足，自动修复已停止。',
    'DEADLINE_EXCEEDED': '本次时间已用完，自动修复已停止。',
    'BUILD_BUDGET_EXCEEDED': '本次时间已用完，自动修复已停止。',
    'REPAIR_INTERRUPTED': '自动修复已中断，未发布状态不明的结果。',
    'REPAIR_EXECUTION_UNKNOWN': '自动修复已中断，未发布状态不明的结果。',
    'REPAIR_REVIEW_BUDGET_RESERVED': '剩余调用额度需留给必要审核，本次自动修复已停止。',
    'STEP_OWNER_LOST': '后台处理已中断，未发布旧修复结果。',
    'CODE_CHANGED': '代码已更新，旧修复结果未被采用。',
    'PROVIDER_UNAVAILABLE': '模型服务暂不可用，自动修复未完成。',
    'PROVIDER_PAYMENT_REQUIRED': '模型服务报告账户额度不足，自动修复未完成。',
}


def safe_repair(record):
    """Only fixed status/reason text crosses the existing query boundary."""
    if not isinstance(record, dict) or record.get('status') not in REPAIR_STATUSES:
        return None
    status = record['status']
    reasons = {
        'pending': '已保留原结果，正在准备本次自动修复。',
        'running': '正在修正本次生成内容，之后会重新检查。',
        'submitted': '已收到修正内容，正在重新检查。',
        'validating': '正在用原有规则检查修正内容。',
        'succeeded': '修正内容已通过原有检查，继续处理原任务。',
        'failed': '修正内容未通过检查，本次不再自动修复。',
        'abstained': '没有找到允许范围内的修复办法，已停止自动修复。',
        'interrupted': '自动修复已中断，未发布状态不明的结果。',
        'exhausted': '本次剩余调用额度或时间不足，自动修复已停止。',
    }
    reason = reasons[status]
    if status in ('failed', 'interrupted', 'exhausted'):
        # Reason fields can contain model text or private inputs. Use exact known
        # codes only, with the final validation result ahead of the original fault.
        failure = record.get('failure') or {}
        validation = record.get('validation') or {}
        code = (validation.get('code') or validation.get('reason')) if isinstance(validation, dict) else None
        code = code or record.get('reason') or (failure.get('code') if isinstance(failure, dict) else None)
        reason = REPAIR_REASONS.get(code, reason) if isinstance(code, str) else reason
    return {'status': status, 'reason': reason}

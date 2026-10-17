import { ApiError } from './data';
import type { CodeRange } from './PythonEditor';
import { repairing, type RepairStatus } from './repairData';

export type SavedCode = { sourceCode: string; codeVersion: number | null; contentHash: string; revision: number };
export type CodeFocus = { codeVersion: number; codeHash: string; codeRanges: CodeRange[] };
export type CodeReference = { codeVersion: number | null; codeHash: string | null; codeRanges: CodeRange[] };
export type Summary = { message: string; status: string; targets: { subgoalId: string; objective: string; status: string; reason?: string }[] };
export type WorkGoal = { subgoalId: string; version: number; objective: string; title?: string; checks: string[]; reason: string; status: string;
  latestCheck?: { reportId: string; status: string; codeVersion: number | null } | null };
export type CoachAction = {
  kind: 'action'; actionId: string; codeVersion: number | null; content: string;
  codeHash: string | null; codeRanges: CodeRange[]; subgoalIds?: string[];
  historySequence: number; checkSummary?: Summary | null;
  followUp?: { kind: string; expectedUserAction: string; criterionIds: string[] } | null;
};
export type CoachMessage = { kind: 'message'; historySequence: number; text: string; codeVersion: number | null; replyTo?: string };
export type CoachReport = { kind: 'report'; reportId: string; batchId: string; historySequence: number;
  subgoalId: string; workGoalVersion: number; codeVersion: number | null; title: string; objective: string; notice: string;
  codeHash: string | null; codeRanges: CodeRange[];
  result: { status: string; inputVersionRefs: Record<string, string | number>; evidenceRefs: string[] };
  checks: { checkId: string; status: string; notice: string; quotes: { ref: string; text: string | null; label: string }[] }[];
  requirements: { checkId: string; requirement: string }[];
};
export type GoalHistory = { id: string; subgoalId: string; currentStatus: string; currentVersion: number; version: Omit<WorkGoal, 'status' | 'subgoalId'> };
export type CoachSession = {
  sessionId: string; codeVersion: number | null; status: string; currentWorkGoalIds: string[]; pendingFollowUp: boolean;
  workGoals: WorkGoal[]; workGoalHistory: GoalHistory[]; goalHistoryNextCursor: string | null;
  items: (CoachAction | CoachMessage | CoachReport)[]; nextCursor: number | null;
  pendingPermission: { stepId: string; roundId: string; codeVersion: number; toolName: string; arguments: Record<string, unknown> } | null;
  round: { roundId: string; status: string; phase: string; batchId?: string; failure?: string;
    repair?: RepairStatus | null;
    failureInfo?: { message: string } | null; checkSummary?: Summary | null;
    retryInfo?: { canRetry: boolean; canRestart: boolean; reason: string | null } } | null;
};
export type WorkGoalDetail = { status: string; currentVersion: number; version: Omit<WorkGoal, 'status' | 'subgoalId'>;
  items: (CoachAction | CoachReport)[]; nextCursor: number | null };
export type Command = { operationId: string; kind: string; codeVersion: number | null; payload: Record<string, unknown> };
export const computing = (state?: CoachSession) => state?.status !== 'paused' && (!!state?.pendingFollowUp || ['queued', 'processing'].includes(state?.round?.status || ''));
export const resultLabels: Record<string, string> = { passed: '本次检查通过', failed: '本次检查未通过', notReady: '缺少检查材料', inconclusive: '尚不能确定' };
export function coachStatus(state: CoachSession) {
  if (state.status === 'paused') return '辅导已暂停';
  if (['queued', 'processing'].includes(state.round?.status || '') && repairing(state.round?.repair)) return '正在自动修复';
  if (state.round?.status === 'processing') return ({ plan: '正在准备下一步提示', review: '正在核对建议',
    verification: '正在检查这个问题', diagnosis: '正在查看问题和代码', tool: '正在获取检查依据', skill: '正在读取检查方法' } as Record<string,string>)[state.round.phase] || '正在查看问题和代码';
  return ({ queued: '已登记，等待教练', processing: '教练正在处理', waiting_permission: '等待你的执行许可',
    finished: '等待你继续', failed: '本轮未能完成', expired: '本轮已超时', interrupted: '本轮已中断',
    cancelled: '本轮已取消', superseded: '材料已更新，旧轮次已结束' } as Record<string,string>)[state.round?.status || ''] || '随时可以提问';
}
export function coachError(error: unknown) {
  if (!(error instanceof ApiError) || error.status >= 500) return '连接或服务暂时不可用；原请求已保留，请找回发送结果。';
  return ({ CODE_VERSION_CHANGED: '代码版本已经变化，本次请求未执行。请读取最新状态后重新操作。',
    PERMISSION_MISMATCH: '待授权的步骤已改变，本次许可未生效。', COACH_BINDING_CHANGED: '题目合同已变化，请重新打开工作区。',
    NOT_RETRYABLE: '当前轮次无需重新处理，请读取最新状态。', IDEMPOTENCY_CONFLICT: '原操作编号与内容不一致，请保留内容并读取记录。',
    RETRY_INPUT_CHANGED: '代码或问题已变化，请根据当前内容重新提问。',
    RETRY_EXPIRED: '原轮次的时间已用完。已有结果保留，你可以明确重新发起。',
    RETRY_BUDGET_EXHAUSTED: '原轮次的调用额度已用完。已有结果保留，你可以明确重新发起。',
    EXECUTION_OUTCOME_UNKNOWN: '上次代码执行的状态尚未确认，请先检查服务；不会自动重复运行。',
    CODE_DRAFT_CONFLICT: '代码保存发生冲突，请先在编辑区选择要保留的代码，问题仍保留在这里。',
  } as Record<string,string>)[error.code] || '请求未被接受，请核对当前状态后重新操作。';
}

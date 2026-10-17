import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from './data';
import type { SavedCode } from './coachData';

type Completion = { status: string; codeVersion: number | null; resultId: string | null; report: { status: string; reason: string } | null };
export function GoalCompletion({ goalId, saved, dirty, executionSignal }: {
  goalId: string; saved: SavedCode; dirty: boolean; executionSignal: string;
}) {
  const query = useQuery({ queryKey: [goalId, 'completion'], queryFn: () => api<Completion>(`/goals/${goalId}/completion`),
    refetchOnWindowFocus: true, refetchOnReconnect: true,
    refetchInterval: q => q.state.data?.status === 'pending' ? 2000 : false });
  useEffect(() => { void query.refetch(); }, [saved.revision, executionSignal]);
  const result = query.data;
  return <div className="goal-completion" aria-label="整题核验" role="region">
    {query.isError && <p role="alert">暂时无法读取整题核验状态，保留上次记录。<button className="quiet" onClick={() => query.refetch()}>重新读取核验</button></p>}
    {result?.status === 'completed' ? <p><strong>✓ 代码版本 {result.codeVersion} 已通过整题核验</strong>
      {dirty || result.codeVersion !== saved.codeVersion ? ' · 当前草稿有变化，尚未验证。' : ' · 依据已保存的可信判题结果。'}</p>
      : result?.status === 'pending' ? <p role="status">判题结果已收到，等待后台完成整题核验…</p>
      : <p className="muted">整题尚未通过最终核验；教练建议和局部检查分别展示。
        {result?.report && ' 最近一次核验未确认完成，请核对提交的代码版本与测试材料。'}</p>}
  </div>;
}

import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from './data';
import { PythonEditor } from './PythonEditor';
import { repairing } from './repairData';
import { coachError, coachStatus, computing, resultLabels, type CoachAction, type CoachReport, type CoachSession,
  type Command, type CodeFocus, type CodeReference, type SavedCode, type WorkGoalDetail } from './coachData';

type Props = { goalId: string; source: string; saved: SavedCode; blocked: boolean; executionSignal: string;
  save: (captured: string) => Promise<SavedCode>; onFocus: (focus: CodeFocus | null) => void; onClose: () => void };
type Local = { text: string; replyTo?: string; pending?: { sessionId: string; command: Command } };

export function CoachPanel(props: Props) {
  const { goalId, source, saved, save, onFocus, onClose, blocked, executionSignal } = props;
  const client = useQueryClient(), key = `coach:${goalId}`;
  const [local, setLocal] = useState<Local>(() => { try { return JSON.parse(sessionStorage.getItem(key) || 'null') || { text: '' }; } catch { return { text: '' }; } });
  const localRef = useRef(local), lock = useRef(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [storageOk, setStorageOk] = useState(true);
  function remember(next: Local) {
    localRef.current = next; setLocal(next);
    try { sessionStorage.setItem(key, JSON.stringify(next)); setStorageOk(true); return true; }
    catch { setStorageOk(false); return false; }
  }
  const workspace = useQuery({ queryKey: [goalId, 'coach-workspace'], queryFn: async () => {
    try { return await api<{ session: { sessionId: string } }>(`/goals/${goalId}/coach-workspace`); }
    catch (e) { if (e instanceof ApiError && e.status === 404) return api<{ session: { sessionId: string } }>(`/goals/${goalId}/coach-workspace`, {}); throw e; }
  }, refetchOnWindowFocus: true, refetchOnReconnect: true });
  const sid = workspace.data?.session.sessionId;
  const session = useQuery({ queryKey: [sid, 'coach-session'], enabled: !!sid,
    queryFn: () => api<CoachSession>(`/coach-sessions/${sid}`), refetchOnWindowFocus: true, refetchOnReconnect: true,
    refetchInterval: q => computing(q.state.data) ? 2000 : false });
  const state = session.data;
  useEffect(() => { if (sid) void session.refetch(); }, [sid, saved.revision, executionSignal]);
  useEffect(() => {
    function leaving(e: BeforeUnloadEvent) { if (localRef.current.pending || localRef.current.text) { e.preventDefault(); e.returnValue = ''; } }
    window.addEventListener('beforeunload', leaving); return () => window.removeEventListener('beforeunload', leaving);
  }, []);
  async function send(kind: string, payload: Record<string, unknown> = {}, retryPending = false) {
    if (lock.current || !sid || !state) return;
    lock.current = true; setBusy(true); setError('');
    try {
      let operation = localRef.current.pending;
      if (operation && !retryPending) return;
      if (!operation) {
        let version = state.codeVersion;
        if (['ask', 'message', 'check'].includes(kind)) {
          if (blocked) throw new ApiError('CODE_DRAFT_CONFLICT');
          version = (await save(source)).codeVersion;
        }
        operation = { sessionId: sid, command: { operationId: crypto.randomUUID(), kind, payload, codeVersion: version } };
        if (!remember({ ...localRef.current, pending: operation })) throw new Error('storage');
      }
      if (!remember(localRef.current)) throw new Error('storage');
      await api(`/coach-sessions/${operation.sessionId}/commands`, operation.command);
      const sent = operation.command;
      // A reply typed while a slow request is in flight is not erased.
      const sameText = localRef.current.text === sent.payload.text;
      remember({ ...localRef.current, pending: undefined, ...(sameText ? { text: '', replyTo: undefined } : {}) });
      await client.invalidateQueries({ queryKey: [sid, 'coach-session'] });
      await client.invalidateQueries({ queryKey: [sid, 'work-goal'] });
    } catch (e) {
      setError(coachError(e));
      if (e instanceof ApiError && e.status >= 400 && e.status < 500)
        remember({ ...localRef.current, pending: undefined });
      await session.refetch();
    } finally { lock.current = false; setBusy(false); }
  }
  const [older, setOlder] = useState<CoachSession['items']>([]), [cursor, setCursor] = useState<number | null | undefined>();
  const [olderGoals, setOlderGoals] = useState<CoachSession['workGoalHistory']>([]), [goalCursor, setGoalCursor] = useState<string | null | undefined>();
  const [loadingHistory, setLoadingHistory] = useState(false);
  useEffect(() => { setOlder([]); setOlderGoals([]); setCursor(undefined); setGoalCursor(undefined); }, [sid]);
  async function loadHistory(goals: boolean) {
    if (loadingHistory) return;
    setLoadingHistory(true);
    try {
      const next = goals ? goalCursor ?? state?.goalHistoryNextCursor : cursor ?? state?.nextCursor;
      if (next == null) return;
      const page = await api<CoachSession>(`/coach-sessions/${sid}?${goals ? 'goalCursor' : 'cursor'}=${encodeURIComponent(next)}`);
      if (goals) { setOlderGoals(old => [...old, ...page.workGoalHistory]); setGoalCursor(page.goalHistoryNextCursor); }
      else { setOlder(old => [...old, ...page.items]); setCursor(page.nextCursor); }
    } catch (e) { setError(coachError(e)); } finally { setLoadingHistory(false); }
  }
  const [selected, setSelected] = useState<{ id: string; version: number } | null>(null);
  const [oldCode, setOldCode] = useState<{ version: number; source: string; focus: CodeFocus } | null>(null);
  const [locating, setLocating] = useState(false);
  const focusSequence = useRef(0), live = useRef({ source, saved }); live.current = { source, saved };
  useEffect(() => { focusSequence.current++; onFocus(null); }, [source]);
  async function locate(action: CodeReference) {
    if (!action.codeVersion || !action.codeHash || !action.codeRanges.length) return;
    const sequence = ++focusSequence.current;
    const focus = { codeVersion: action.codeVersion, codeHash: action.codeHash, codeRanges: action.codeRanges };
    const now = live.current;
    if (now.source === now.saved.sourceCode && focus.codeVersion === now.saved.codeVersion && focus.codeHash === now.saved.contentHash) {
      setOldCode(null); onFocus(focus); return;
    }
    onFocus(null); setLocating(true);
    try {
      const snapshot = await api<{ sourceCode: string; contentHash: string }>(`/goals/${goalId}/code-snapshots/${focus.codeVersion}`);
      if (sequence === focusSequence.current && snapshot.contentHash === focus.codeHash) setOldCode({ version: focus.codeVersion, source: snapshot.sourceCode, focus });
    } catch (e) { setError(coachError(e)); } finally { setLocating(false); }
  }
  const items = [...new Map([...older, ...(state?.items || [])].map(item => [item.historySequence, item])).values()].sort((a,b) => a.historySequence - b.historySequence);
  const goals = state?.workGoals.filter(g => g.status === 'verified' || state.currentWorkGoalIds.includes(g.subgoalId)) || [];
  const currentGoals = goals.filter(g => state?.currentWorkGoalIds.includes(g.subgoalId));
  const retired = [...new Map([...olderGoals, ...(state?.workGoalHistory || [])].map(g => [g.id, g])).values()]
    .filter(g => g.version.version !== g.currentVersion || !goals.some(current => current.subgoalId === g.subgoalId));
  const latestAction = [...items].reverse().find((item): item is CoachAction => item.kind === 'action' && item.codeVersion === saved.codeVersion
    && !!item.subgoalIds?.some(id => state?.currentWorkGoalIds.includes(id)));
  const disabled = busy || !!local.pending || !state || !storageOk;
  const permission = state?.pendingPermission;
  const dirty = source !== saved.sourceCode;
  const processing = !!state?.round && ['queued','processing','waiting_permission'].includes(state.round.status);
  const checkDisabled = disabled || blocked || state?.status === 'paused' || processing;
  const latestQuestion = items.map(item => item.kind).lastIndexOf('message');
  const earlierItems = latestQuestion > 0 ? items.slice(0, latestQuestion) : [];
  const recentItems = latestQuestion > 0 ? items.slice(latestQuestion) : items;
  const latestQuestionSequence = latestQuestion >= 0 ? items[latestQuestion].historySequence : null;
  useEffect(() => { bodyRef.current?.scrollTo({ top: 0 }); }, [latestQuestionSequence]);
  function renderItem(item: CoachSession['items'][number]) {
    return <article className={`coach-message ${item.kind}`} key={item.historySequence}>
      <small>{item.kind === 'message' ? '你' : item.kind === 'report' ? '局部问题检查' : '教练建议'} · {item.codeVersion === null ? '未附代码' : `代码版本 ${item.codeVersion}`}
        {item.codeVersion !== saved.codeVersion || dirty ? ' · 当前草稿可能已有变化' : ''}</small>
      {item.kind === 'report' ? <ReportCard report={item} onLocate={locate} /> : <CoachText text={item.kind === 'message' ? item.text : item.content} />}
      {item.kind === 'action' && <div className="coach-inline-actions"><button className="quiet" disabled={disabled} onClick={() => remember({ ...localRef.current, replyTo: item.actionId })}>回复这条建议</button>
        {!!item.codeRanges.length && <button className="quiet" onClick={() => locate(item)}>查看关联代码</button>}</div>}
    </article>;
  }
  return <aside className="panel coach-panel" aria-label="刷题教练">
    <div className="coach-heading"><div><span className="eyebrow">一起把问题弄明白</span><h2>🦉 刷题教练</h2></div><button className="quiet" onClick={onClose} aria-label="收起教练">收起</button></div>
    <p className="muted">代码在编辑器里改，问题在这里说。收起后仍会继续处理。</p>
    <div className="coach-body" ref={bodyRef}>
    {!state && !workspace.isError && !session.isError && <p role="status">正在恢复教练记录…</p>}
    {(workspace.isError || session.isError) && <p role="alert">暂时无法读取，保留上次已知状态。<button onClick={() => { void workspace.refetch(); void session.refetch(); }}>重新读取教练</button></p>}
    {state && <><div className="coach-status" role="status">{coachStatus(state)}</div>
      {computing(state) && repairing(state.round?.repair) && <p className="muted">{state.round?.repair?.reason}</p>}
      {state.pendingFollowUp && state.status !== 'paused' && <p className="muted">等待运行结果及后台跟进，处理完成后会自动更新。</p>}
      {goals.length > 0 && <><ol className="goal-track" aria-label="工作目标进展">{goals.map(g => <li key={g.subgoalId} className={g.status === 'verified' ? 'verified' : 'current'}>
        <button className="goal-node" aria-label={`${g.status === 'verified' ? '已核验' : '当前处理'}：${g.objective}`} aria-pressed={selected?.id === g.subgoalId && selected.version === g.version}
          onClick={() => { onFocus(null); setSelected({ id: g.subgoalId, version: g.version }); }}><span className="goal-dot" aria-hidden="true">{g.status === 'verified' ? '✓' : '•'}</span><span title={g.objective}>{g.title || g.objective}</span></button>
        {g.latestCheck?.status === 'passed' && <small>代码 {g.latestCheck.codeVersion ?? '无'} 已检查{dirty || g.latestCheck.codeVersion !== saved.codeVersion ? ' · 当前代码未据此检查' : ''}</small>}
      </li>)}</ol></>}
      {!!currentGoals.length && <section className="coach-current" aria-label="当前问题"><span className="eyebrow">正在解决</span>
        {currentGoals.map(g => <div key={g.subgoalId}><h3>{g.title || g.objective}</h3>
          {g.latestCheck?.status === 'passed' && g.latestCheck.codeVersion === saved.codeVersion && !dirty
            ? <p className="muted">这一步已有通过记录。你可以继续写，或问下一个问题。</p>
            : <button className="secondary" disabled={checkDisabled} onClick={() => send('check', { subgoalIds: [g.subgoalId] })}>检查这一步</button>}
        </div>)}
        {latestAction?.followUp?.expectedUserAction && latestAction.followUp.kind !== 'none'
          && currentGoals.some(g => dirty || g.latestCheck?.status !== 'passed' || g.latestCheck.codeVersion !== saved.codeVersion)
          && <p className="coach-next">下一步：{latestAction.followUp.expectedUserAction}</p>}
        {dirty && <p className="muted">检查时会先保存当前代码；旧结论仍对应原版本。</p>}
      </section>}
      <details className="coach-history"><summary>以前的问题与版本</summary>{retired.map(g => <button className="history-item secondary" key={g.id}
        onClick={() => { onFocus(null); setSelected({ id: g.subgoalId, version: g.version.version }); }}>{g.version.objective} · 目标版本 {g.version.version}
        {g.version.version < g.currentVersion ? ' · 历史版本' : g.currentStatus === 'replaced' ? ' · 已替换' : g.currentStatus === 'withdrawn' ? ' · 已撤回' : ''}</button>)}
        {!retired.length && <p className="muted">暂无记录；普通问答不需要建立工作目标。</p>}
        {(goalCursor === undefined ? state.goalHistoryNextCursor : goalCursor) && <button disabled={loadingHistory} onClick={() => loadHistory(true)}>更早的问题</button>}
      </details>
      {selected && <GoalDetail key={`${sid}:${selected.id}:${selected.version}`} sid={sid!} selected={selected} changed={`${state.round?.status}:${state.round?.phase}:${state.round?.checkSummary?.status}:${saved.revision}`}
        onLocate={locate} onClose={() => { setSelected(null); onFocus(null); }} onCheck={() => send('check', { subgoalIds: [selected.id] })}
        disabled={checkDisabled || !state.workGoals.some(g => g.subgoalId === selected.id && g.version === selected.version && ['active','verified'].includes(g.status))}
        historical={!state.currentWorkGoalIds.includes(selected.id)} />}
      {locating && <p role="status">正在读取对应代码…</p>}
      {oldCode && <details open><summary>代码版本 {oldCode.version}（只读，不替换当前草稿）</summary><button className="quiet" onClick={() => setOldCode(null)}>收起历史代码</button>
        <PythonEditor key={oldCode.version} value={oldCode.source} readOnly label="教练引用的历史代码" ranges={oldCode.focus.codeRanges} /></details>}
      <div className="coach-messages" aria-label="教练对话">
        {(earlierItems.length > 0 || (cursor === undefined ? state.nextCursor : cursor) != null) && <details className="coach-previous"><summary>之前的对话</summary>
          {(cursor === undefined ? state.nextCursor : cursor) != null && <button className="quiet" disabled={loadingHistory} onClick={() => loadHistory(false)}>更早的对话</button>}
          {earlierItems.map(renderItem)}</details>}
        {!items.length && <p className="coach-empty">卡在哪一步？可以说说你的思路，或直接问某一行代码。</p>}
        {recentItems.map(renderItem)}
      </div>
      {state.round?.failureInfo && <div className="coach-failure" role="alert"><strong>这一步暂时没能完成</strong><p>{state.round.failureInfo.message}</p>
        {state.round.retryInfo?.reason === 'EXECUTION_OUTCOME_UNKNOWN' && <p>上次代码执行的状态尚未确认，需要先检查服务；不会自动重复运行。</p>}
        {state.round.retryInfo?.canRetry && <button className="secondary" disabled={disabled || dirty || state.status === 'paused'} onClick={() => send('retry')}>继续未完成的步骤</button>}
        {state.round.retryInfo?.canRestart && !state.round.retryInfo.canRetry && <><p>已完成的记录会保留。重新发起会建立新一轮处理。</p><button className="secondary" disabled={disabled || dirty || state.status === 'paused'} onClick={() => send('restart')}>重新发起</button></>}
      </div>}
      {state.round?.checkSummary?.status === 'incomplete' && <p className="notice">{state.round.checkSummary.message}</p>}
      {permission && <div className="notice"><strong>教练申请执行一次代码</strong><p>代码版本 {permission.codeVersion} · {permission.toolName === 'run_public_samples' ? '运行公开样例' : '运行局部代码'}</p>
        <details><summary>查看本次执行内容</summary><pre>{JSON.stringify(permission.arguments, null, 2)}</pre></details>
        {dirty && <p>草稿已有修改，请先保存并重新提问；旧步骤不能用于新代码。</p>}
        <button disabled={disabled || dirty || permission.codeVersion !== saved.codeVersion || state.status === 'paused'} onClick={() => send('grant_permission', { stepId: permission.stepId, tools: [permission.toolName] })}>仅允许这一步</button>
        <button className="secondary" disabled={disabled} onClick={() => send('revoke_execution')}>不允许执行</button></div>}
      <div className="coach-controls"><button className="quiet" disabled={disabled} onClick={() => send(state.status === 'paused' ? 'resume' : 'pause')}>{state.status === 'paused' ? '恢复辅导' : '暂停辅导'}</button>
        {state.round && ['queued','processing','waiting_permission'].includes(state.round.status) && <button className="quiet" disabled={disabled} onClick={() => send('stop_run')}>取消本轮</button>}
      </div>
    </>}
    {error && <p role="alert">{error}</p>}
    {!storageOk && <p role="alert">浏览器暂存不可用，请复制问题备份后再发送。<button onClick={() => remember(localRef.current)}>重新暂存问题</button></p>}
    {local.pending && <div className="notice"><p>上次发送尚未确认，保留了原内容和操作编号。</p><button disabled={busy || !state} onClick={() => send('', {}, true)}>找回发送结果</button></div>}
    </div>
    <form className="coach-compose" onSubmit={e => { e.preventDefault(); if (localRef.current.text.trim()) void send(local.replyTo ? 'message' : 'ask', { text: localRef.current.text, ...(local.replyTo ? { replyTo: local.replyTo } : {}) }); }}>
      {local.replyTo && <p className="muted">正在回复选中的建议 <button type="button" className="quiet" onClick={() => remember({ ...localRef.current, replyTo: undefined })}>取消关联</button></p>}
      <label htmlFor="coach-question">想请教的问题</label><textarea id="coach-question" rows={3} value={local.text} onChange={e => remember({ ...localRef.current, text: e.target.value })} placeholder="问一个问题，或告诉教练“改好了，看看边界”" />
      <button type="submit" disabled={disabled || blocked || !local.text.trim() || state?.status === 'paused'}>{busy ? '正在登记…' : '发送给教练'}</button>
      {blocked && <p className="muted">先解决编辑区的保存冲突，问题会保留。</p>}
    </form>
  </aside>;
}

function CoachText({ text }: { text: string }) {
  // Only present fenced code separately. React escapes all model/user text; no HTML execution.
  const pieces = text.split(/(```[^\n]*\n[\s\S]*?\n```)/g);
  return <div className="coach-text">{pieces.map((piece, i) => piece.startsWith('```')
    ? <pre key={i}><code>{piece.slice(piece.indexOf('\n') + 1, -4)}</code></pre>
    : <span className="plain-text" key={i}>{piece}</span>)}</div>;
}

function GoalDetail({ sid, selected, changed, onLocate, onClose, onCheck, disabled, historical }: {
  sid: string; selected: { id: string; version: number }; changed: string;
  onLocate: (action: CodeReference) => void; onClose: () => void; onCheck: () => void; disabled: boolean; historical: boolean;
}) {
  const path = `/coach-sessions/${sid}/work-goals/${selected.id}?version=${selected.version}`;
  const query = useQuery({ queryKey: [sid, 'work-goal', selected.id, selected.version], queryFn: () => api<WorkGoalDetail>(path) });
  const [older, setOlder] = useState<WorkGoalDetail['items']>([]), [cursor, setCursor] = useState<number | null | undefined>();
  const [loading, setLoading] = useState(false), [error, setError] = useState('');
  useEffect(() => { void query.refetch(); }, [changed]);
  async function more() {
    if (loading) return; setLoading(true);
    try { const page = await api<WorkGoalDetail>(path + '&cursor=' + (cursor ?? query.data?.nextCursor)); setOlder(old => [...old, ...page.items]); setCursor(page.nextCursor); }
    catch { setError('暂时无法读取更早的检查，请重试。'); } finally { setLoading(false); }
  }
  const data = query.data;
  const records = [...new Map([...older, ...(data?.items || [])].map(x => [x.kind === 'report' ? x.reportId : x.actionId, x])).values()];
  const reports = records.filter((x): x is CoachReport => x.kind === 'report');
  return <section className="goal-detail" aria-label="工作目标详情"><button className="quiet" onClick={onClose}>收起详情</button>
    {(query.isError || error) && <p role="alert">{error || '详情暂时不可用。'}<button onClick={() => query.refetch()}>重新读取详情</button></p>}
    {data && <><h3>{data.version.objective}</h3><small>目标版本 {data.version.version} · {data.currentVersion !== data.version.version ? '历史要求' : '当前要求'}</small>
      {historical && <p className="muted">正在查看以前的问题，当前关注的问题没有改变。</p>}
      <details><summary>检查范围</summary><p>{data.version.reason}</p><ul>{data.version.checks.map((text,i) => <li key={i}>{text}</li>)}</ul></details>
      {!reports.length && <p className="muted">尚无本版本的正式检查报告；建议本身不代表已经通过。</p>}
      {reports.map(report => <div className="coach-report" key={report.reportId}><small>代码版本 {report.result.inputVersionRefs.codeVersion ?? '未附代码'}</small><ReportCard report={report} onLocate={onLocate} /></div>)}
      {records.filter((x): x is CoachAction => x.kind === 'action').map(action => <details key={action.actionId}><summary>关联建议 · 代码版本 {action.codeVersion ?? '无'}</summary>
        <CoachText text={action.content} />{!!action.codeRanges.length && <button className="secondary" onClick={() => onLocate(action)}>定位这份代码</button>}</details>)}
      {(cursor === undefined ? data.nextCursor : cursor) != null && <button disabled={loading} onClick={more}>更早的依据</button>}
      <button disabled={disabled} onClick={onCheck}>{historical ? '继续检查这个问题' : '检查这个问题'}</button></>}
  </section>;
}

function ReportCard({ report, onLocate }: { report: CoachReport; onLocate: (item: CodeReference) => void }) {
  return <div className={`coach-check ${report.result.status}`}><strong>{resultLabels[report.result.status] || '结论未知'}</strong>
    <p>{report.title || report.objective}</p><p className="muted">{report.notice}</p>
    <details><summary>查看检查依据</summary>{report.checks.map(check => <div className="coach-check-item" key={check.checkId}>
      <p>{report.requirements.find(r => r.checkId === check.checkId)?.requirement}</p><strong>{resultLabels[check.status]}</strong><p>{check.notice}</p>
      {check.quotes.map((quote,i) => <div key={i}><small>{quote.label} · {quote.ref}</small>{quote.text && <blockquote>{quote.text}</blockquote>}</div>)}
      {!check.quotes.length && <p className="muted">这项尚无可展示的依据。</p>}
    </div>)}</details>
    {!!report.codeRanges?.length && <button className="quiet" onClick={() => onLocate(report)}>定位检查的代码</button>}
  </div>;
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type RecordData } from './data';
import { PythonEditor } from './PythonEditor';
import { CoachPanel } from './CoachPanel';
import { GoalCompletion } from './GoalCompletion';
import type { CodeFocus } from './coachData';

type Context = { goalId: string; scope: 'goal' };
type Ref = { problemId: string; problemVersion: number };
type Draft = { goalContext: Context; sourceCode: string; revision: number; codeVersion: number | null; contentHash: string };
type Workspace = { goal: { goalId: string; problemRef: Ref }; draft: Draft; problem: RecordData };
type SaveOperation = { sourceCode: string; expectedRevision: number; operationId: string };
type ActionOperation = { path: string; body: Record<string, unknown>; kind: 'run' | 'submission' };
type Cached = { source: string; draft: Draft; pendingSave?: SaveOperation; pendingAction?: ActionOperation; stdin?: string };
type Selected = { kind: 'run' | 'submission'; id: string };
type Execution = { id: string; kind: 'run' | 'submission'; codeVersion: number; status: string; createdAt: string; verdict?: string; runMode?: string };
type Page = { items: Execution[]; nextCursor: string | null };
type Case = { index: number; stdin: string; expectedOutput: string | null; stdout: string; stderr: string; outcome: string;
  inputTruncated: boolean; expectedTruncated: boolean; stdoutTruncated: boolean; stderrTruncated: boolean; wallTimeMs: number | null;
  executionTimeMs?: number | null; memoryLimitBytes?: number | null; peakMemoryBytes?: number | null };
type Failure = { stage: string; errorType: string; elapsedMs?: number; taskElapsedMs?: number; retryCount: number };
type Run = { runId: string; codeVersion: number; status: string; result: { cases: Case[]; failureCode?: string; failure?: Failure | null } | null };
type OJ = { verdict: string; testSummary: { passed: number; total: number }; resourceSummary: { totalWallTimeMs: number; totalExecutionTimeMs?: number | null; peakMemoryBytes: number | null }; stderrCategory: string | null;
  assurance?: 'limited' };
type SubmissionResult = { submission: { submissionId: string; codeVersion: number; status: string; statusReason?: string }; result: OJ | null; failure?: Failure | null };
function FailureDetails({ failure }: { failure?: Failure | null }) {
  if (!failure) return <p>本次没有记录具体失败原因，无法判断是哪一步出错。</p>;
  const stages: Record<string,string> = {connect:'连接执行服务',create:'准备运行环境',attach:'建立输出连接',start:'启动程序',inspect:'查询执行状态',kill:'停止程序',remove:'清理运行环境',load:'读取任务材料',execute:'处理执行任务',saveResult:'保存执行结果',runTask:'处理运行任务'};
  return <div role="alert"><p>{stages[failure.stage] || '处理任务'}时发生系统故障，不能据此判断代码对错。</p>
    <details><summary>查看故障记录</summary><p>异常类型：{failure.errorType} · 耗时：{failure.taskElapsedMs ?? failure.elapsedMs ?? '未记录'} ms · 安全查询重试：{failure.retryCount} 次</p></details></div>;
}
const active = (status: string) => ['queued', 'running', 'preparing', 'awaitingAssets'].includes(status);
const labels: Record<string, string> = {
  queued: '等待执行', preparing: '准备执行', running: '正在执行', awaitingAssets: '等待测试材料',
  finished: '执行结束', failed: '系统未能完成', interrupted: '运行已中断', expired: '执行已中断',
  AC: 'AC · 通过当前测试', WA: 'WA · 输出不符', TLE: 'TLE · 超过时间限制', RE: 'RE · 运行错误', CE: 'CE · 语法错误',
  executed: '执行完成', passed: '样例通过', outputMismatch: '输出不符', compileError: '语法错误', runtimeError: '运行错误',
  timeLimitExceeded: '超过时间限制', infrastructureError: '运行服务不可用', notExecuted: '本次预算内未执行', outputLimitExceeded: '输出超过限制', memoryLimitExceeded: '内存超过限制',
  syntaxError: 'Python 语法不完整或不合法，可通过运行查看具体错误。', runtimeErrorDetail: '程序执行时发生异常，可运行公开样例或自定义输入定位。',
};
function describe(error: unknown) {
  if (!(error instanceof ApiError)) return '连接中断，操作结果尚未确认；原请求已保留，可重试。';
  return ({ CODE_DRAFT_CONFLICT: '另一个页面已保存代码。你的草稿已保留，请对照后台内容。',
    IDEMPOTENCY_CONFLICT: '这个操作编号已对应另一份内容，请保留代码并重新加载记录。',
    VERIFIER_ASSET_REJECTED: '测试材料尚不可用于正式判题，请返回测试准备页面处理。',
    PROBLEM_VERSION_NOT_FROZEN: '请先确认题目条件，再开始写代码。',
    INVALID_RUN: '请检查运行输入和代码版本。', REQUEST_BODY_TOO_LARGE: '代码或输入过长，超过本次请求的大小限制。',
  } as Record<string, string>)[error.code] || '操作未完成，请保留代码并重试。';
}

export function CodePage() {
  const { id, version } = useParams();
  const prefix = `/problems/${id}/versions/${version}`;
  const workspace = useQuery({ queryKey: [prefix, 'editor'], queryFn: () => api<Workspace>(prefix + '/editor-workspace', {}),
    staleTime: 0, gcTime: 0, refetchOnMount: 'always', refetchOnWindowFocus: false, refetchOnReconnect: false });
  const ready = workspace.isSuccess && workspace.isFetchedAfterMount;
  return <><Link to={prefix + '/tests'}>← 题目与测试准备</Link>
    {!ready && !workspace.isError && <p>正在恢复编程工作区…</p>}
    {workspace.isError && <p role="alert">{describe(workspace.error)} <button onClick={() => workspace.refetch()}>重新读取</button></p>}
    {ready && <WorkspaceView key={workspace.data.goal.goalId} workspace={workspace.data} prefix={prefix} />}</>;
}

function WorkspaceView({ workspace, prefix }: { workspace: Workspace; prefix: string }) {
  const goalId = workspace.goal.goalId, client = useQueryClient();
  // A copied tab inherits initial session data, but subsequent writes are isolated.
  const localKey = `editor:${goalId}`;
  const [cached] = useState<Cached | null>(() => { try { return JSON.parse(sessionStorage.getItem(localKey) || 'null'); } catch { return null; } });
  const [source, setSource] = useState(cached?.source ?? workspace.draft.sourceCode);
  const sourceRef = useRef(source), base = useRef(cached?.draft ?? workspace.draft);
  const pendingSave = useRef(cached?.pendingSave), pendingAction = useRef(cached?.pendingAction);
  const [stdin, setStdin] = useState(cached?.stdin ?? ''), stdinRef = useRef(stdin);
  const [mode, setMode] = useState<'publicExample' | 'customInput'>('publicExample');
  const staleDraft = cached && cached.draft.revision !== workspace.draft.revision && !cached.pendingSave;
  const [state, setState] = useState(cached?.pendingSave ? 'error' : staleDraft ? 'conflict' : source === base.current.sourceCode ? 'saved' : 'local');
  const [message, setMessage] = useState(''), [storageOk, setStorageOk] = useState(true);
  const [busy, setBusy] = useState(false), actionBusy = useRef(false), mounted = useRef(true);
  const saving = useRef<Promise<Draft> | null>(null), dirtySince = useRef<number | null>(null);
  const [conflict, setConflict] = useState<Draft | null>(staleDraft ? workspace.draft : null), [selected, setSelected] = useState<Selected | null>(null);
  const [oldCode, setOldCode] = useState<{ version: number; source: string } | null>(null);
  const [coachOpen, setCoachOpen] = useState(() => { try { return sessionStorage.getItem(`coach-open:${goalId}`) === 'true'; } catch { return false; } });
  const [coachMounted, setCoachMounted] = useState(coachOpen);
  const [codeFocus, setCodeFocus] = useState<CodeFocus | null>(null);
  function toggleCoach(open: boolean) {
    setCoachOpen(open); if (open) setCoachMounted(true);
    try { sessionStorage.setItem(`coach-open:${goalId}`, String(open)); } catch { /* Optional layout preference. */ }
  }
  const persist = useCallback(() => {
    let ok = false;
    try {
      sessionStorage.setItem(localKey, JSON.stringify({ source: sourceRef.current, draft: base.current,
        pendingSave: pendingSave.current, pendingAction: pendingAction.current, stdin: stdinRef.current }));
      ok = true;
    } catch { /* Preserve in-memory input and block requests without a recovery record. */ }
    if (mounted.current) setStorageOk(ok);
    return ok;
  }, [localKey]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { persist(); }, [persist]);
  function edit(value: string) {
    setCodeFocus(null);
    sourceRef.current = value; setSource(value); persist();
    if (dirtySince.current === null) dirtySince.current = Date.now();
    setState(current => ['conflict', 'error'].includes(current) ? current : 'local');
  }
  const save = useCallback(async (captured: string): Promise<Draft> => {
    while (saving.current) await saving.current;
    const task = (async () => {
      if (mounted.current) setState('saving');
      try {
        // First resolve an ambiguous earlier save with its original immutable request.
        for (;;) {
          if (!pendingSave.current && captured === base.current.sourceCode) break;
          if (!pendingSave.current) pendingSave.current = { sourceCode: captured, expectedRevision: base.current.revision, operationId: crypto.randomUUID() };
          if (!persist()) throw new Error('browser storage unavailable');
          const result = await api<Draft>(`/goals/${goalId}/code-draft`, pendingSave.current, 'PUT');
          base.current = result; pendingSave.current = undefined; persist();
          if (captured === result.sourceCode) break;
        }
        dirtySince.current = sourceRef.current === base.current.sourceCode ? null : Date.now();
        if (mounted.current) { setState(sourceRef.current === base.current.sourceCode ? 'saved' : 'local'); setMessage(''); }
        return base.current;
      } catch (error) {
        if (mounted.current) {
          setMessage(describe(error));
          setState(error instanceof ApiError && error.code === 'CODE_DRAFT_CONFLICT' ? 'conflict' : 'error');
          if (error instanceof ApiError && error.code === 'CODE_DRAFT_CONFLICT') {
            const latest = await api<Draft>(`/goals/${goalId}/code-draft`); if (mounted.current) setConflict(latest);
          }
        }
        if (error instanceof ApiError && [400,413,415,422].includes(error.status)) { pendingSave.current = undefined; persist(); }
        throw error;
      }
    })();
    saving.current = task;
    try { return await task; } finally { if (saving.current === task) saving.current = null; }
  }, [goalId, persist]);
  useEffect(() => {
    if (state !== 'local') return;
    const remaining = Math.max(0, 30000 - (Date.now() - (dirtySince.current ?? Date.now())));
    const timer = setTimeout(() => { void save(sourceRef.current).catch(() => {}); }, Math.min(2000, remaining));
    return () => clearTimeout(timer);
  }, [source, state, save]);
  useEffect(() => {
    function leaving(e: BeforeUnloadEvent) { if (sourceRef.current !== base.current.sourceCode || pendingSave.current || pendingAction.current) { e.preventDefault(); e.returnValue = ''; } }
    window.addEventListener('beforeunload', leaving); return () => window.removeEventListener('beforeunload', leaving);
  }, []);
  const history = useInfiniteQuery({ queryKey: [goalId, 'executions'], initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api<Page>(`/goals/${goalId}/executions${pageParam ? '?cursor=' + encodeURIComponent(pageParam) : ''}`),
    getNextPageParam: page => page.nextCursor,
    refetchInterval: query => query.state.data?.pages.some(page => page.items.some(item => active(item.status))) ? 2000 : false });
  useEffect(() => { const first = history.data?.pages[0]?.items[0]; if (!selected && first) setSelected(first); }, [history.data, selected]);
  const detail = useQuery<Run | SubmissionResult>({ queryKey: [goalId, 'execution', selected?.kind, selected?.id], enabled: !!selected,
    queryFn: () => selected!.kind === 'run' ? api<Run>(`/runs/${selected!.id}`) : api<SubmissionResult>(`/submissions/${selected!.id}/result`),
    refetchInterval: query => { const data = query.state.data; return data && active('submission' in data ? data.submission.status : data.status) ? 2000 : false; } });
  async function act(kind: 'run' | 'submission') {
    if (actionBusy.current) return;
    actionBusy.current = true; setBusy(true); setMessage('');
    const captured = sourceRef.current, capturedInput = stdinRef.current, capturedMode = mode;
    try {
      if (!pendingAction.current) {
        const saved = await save(captured);
        if (saved.codeVersion === null) throw new Error('empty');
        const shared = { goalContext: saved.goalContext, problemRef: workspace.goal.problemRef, codeVersion: saved.codeVersion };
        pendingAction.current = kind === 'run'
          ? { kind, path: '/runs', body: { ...shared, operationId: crypto.randomUUID(), runMode: capturedMode, ...(capturedMode === 'customInput' ? { stdin: capturedInput } : {}) } }
          : { kind, path: '/submissions', body: { ...shared, language: 'python3', idempotencyKey: crypto.randomUUID() } };
      }
      if (!persist()) throw new Error('storage');
      const operation = pendingAction.current;
      const result = await api<{ runId?: string; submissionId?: string }>(operation.path, operation.body);
      pendingAction.current = undefined; persist();
      if (mounted.current) { setSelected({ kind: operation.kind, id: (result.runId || result.submissionId)! }); setOldCode(null); }
      await client.invalidateQueries({ queryKey: [goalId, 'executions'] });
    } catch (error) {
      if (mounted.current) setMessage(describe(error));
      if (error instanceof ApiError && [400,413,415,422].includes(error.status)) { pendingAction.current = undefined; persist(); }
    } finally { actionBusy.current = false; if (mounted.current) setBusy(false); }
  }
  async function chooseConflict(useMine: boolean) {
    if (!conflict) return;
    const chosen = conflict; base.current = chosen; pendingSave.current = undefined;
    setConflict(null); setMessage('');
    if (useMine) { setState('local'); persist(); await save(sourceRef.current).catch(() => {}); }
    else { sourceRef.current = chosen.sourceCode; setSource(chosen.sourceCode); persist(); setState('saved'); }
  }
  const data = detail.data, task = data && ('submission' in data ? data.submission : data);
  const problem = workspace.problem.problemPackage;
  const disabled = busy || !!conflict || state === 'conflict' || !source.trim() || !!pendingAction.current;
  const executionSignal = JSON.stringify(history.data?.pages[0]?.items.map(item => [item.id, item.status, item.verdict]) || []);
  const visibleRanges = codeFocus && source === base.current.sourceCode && codeFocus.codeVersion === base.current.codeVersion && codeFocus.codeHash === base.current.contentHash ? codeFocus.codeRanges : undefined;
  return <>
    <h1>{problem.title || 'Python 编程工作区'}</h1>
    <p>Python 3 · 标准输入输出 <span className="badge">题目版本 {workspace.goal.problemRef.problemVersion}</span></p>
    <GoalCompletion goalId={goalId} saved={base.current} dirty={source !== base.current.sourceCode} executionSignal={executionSignal} />
    <div className="coach-toolbar"><button className={coachOpen ? 'secondary' : 'primary'} aria-expanded={coachOpen} aria-controls="coach-sidebar" onClick={() => toggleCoach(!coachOpen)}>{coachOpen ? '返回代码 / 收起教练' : '🦉 向教练求助'}</button></div>
    <div className={`practice-layout${coachOpen ? ' coach-open' : ''}`}>
    <div className="coding-layout">
      <section className="panel problem-pane"><h2>题目</h2><div className="plain-text">{problem.statement}</div>
        <h3>输入</h3><div className="plain-text">{problem.inputSpec}</div><h3>输出</h3><div className="plain-text">{problem.outputSpec}</div>
        <h3>条件</h3><div className="plain-text">{problem.constraints}</div>
        {problem.examples.map((example, i) => <details key={i}><summary>公开样例 {i + 1}</summary><pre>{example.input}</pre><pre>{example.output}</pre></details>)}
      </section>
      <section className="panel code-pane"><div className="section-heading"><h2>我的代码</h2>
        <span role="status" data-testid="save-status">{({ saved: '已保存', saving: '正在保存', local: '仅浏览器暂存', error: '保存未确认', conflict: '保存冲突' } as Record<string,string>)[state]}</span></div>
        <PythonEditor value={source} onChange={edit} ranges={visibleRanges} />
        {visibleRanges && <p className="muted">已标出这条建议引用的代码；修改后高亮会清除。<button className="quiet" onClick={() => setCodeFocus(null)}>清除高亮</button></p>}
        <p className="muted">临时草稿保留在当前标签页，关闭前请确认已保存。</p>
        {!storageOk && <p role="alert">浏览器暂存不可用，请先复制代码备份。本次运行或提交不会继续。</p>}
        {state === 'error' && <button onClick={() => save(sourceRef.current).catch(() => {})}>重试保存</button>}
        {conflict && <div className="notice" role="alert"><p>后台已保存另一份代码。请对照后选择：</p><pre>{conflict.sourceCode || '（空稿）'}</pre>
          <button onClick={() => chooseConflict(false)}>采用后台内容</button><button onClick={() => chooseConflict(true)}>确认用我的内容保存</button></div>}
        <div className="run-controls"><label>运行输入<select value={mode} onChange={e => setMode(e.target.value as typeof mode)}><option value="publicExample">全部公开样例</option><option value="customInput">自定义输入</option></select></label>
          {mode === 'customInput' && <label>标准输入<textarea value={stdin} onChange={e => { stdinRef.current=e.target.value; setStdin(e.target.value); persist(); }} placeholder="原样传给程序的输入内容" /></label>}
          <div className="actions"><button disabled={disabled} onClick={() => act('run')}>运行</button><button disabled={disabled} className="primary" onClick={() => act('submission')}>正式提交</button></div>
          {pendingAction.current && !busy && <button onClick={() => act(pendingAction.current!.kind)}>找回上次运行或提交</button>}
          <p className="muted">运行只检查本次输入；正式提交使用后台测试材料。语法不完整也可以运行查看错误。</p>
        </div>
      </section>
    </div>
    <div id="coach-sidebar" hidden={!coachOpen}>{coachMounted && <CoachPanel goalId={goalId} source={source} saved={base.current}
      blocked={!!conflict || state === 'conflict'} save={save} executionSignal={executionSignal} onFocus={setCodeFocus} onClose={() => toggleCoach(false)} />}</div>
    </div>
    {message && <p role="alert">{message} <Link to={prefix + '/tests'}>查看测试准备</Link></p>}
    <section className="panel" aria-label="执行结果"><h2>执行结果</h2>
      {!selected && <p>运行或正式提交后，结果会显示在这里。</p>}
      {detail.isError && <p role="alert">暂时无法查询，下面保留上次已知结果。<button onClick={() => detail.refetch()}>重新查询</button></p>}
      {task && <><p><strong>{labels[task.status] || task.status}</strong> · 代码版本 {task.codeVersion}
        {(source !== base.current.sourceCode || task.codeVersion !== base.current.codeVersion) && ' · 当前代码已有变化，这不是当前草稿的结果'}</p>
        <button onClick={async () => { try { const snapshot=await api<{sourceCode:string}>(`/goals/${goalId}/code-snapshots/${task.codeVersion}`); setOldCode({version:task.codeVersion,source:snapshot.sourceCode}); } catch(e) {setMessage(describe(e));} }}>查看本次代码</button>
        {task.status === 'awaitingAssets' && <p>这份提交已保存，材料就绪后自动进入判题。<Link to={prefix + '/tests'}>查看准备状态</Link></p>}
        {'statusReason' in task && task.statusReason === 'verifierAssetRejected' && <p role="alert">测试材料已被拒绝，不能用于判题。<Link to={prefix + '/tests'}>查看原因并处理</Link></p>}
        {task.status === 'queued' && <p>等待本机执行服务领取。若长时间未开始，请检查执行服务是否启动。</p>}
        {['failed','interrupted','expired'].includes(task.status) && <p>本次没有形成可信的代码正确性结论。可检查服务后主动重新运行或提交。</p>}
        {task.status === 'failed' && !('statusReason' in task && task.statusReason === 'verifierAssetRejected') && <FailureDetails failure={data && 'submission' in data ? data.failure : data && 'runId' in data ? data.result?.failure : null} />}
      </>}
      {data && 'submission' in data && data.result && <div className="oj-result"><h3>{labels[data.result.verdict]}</h3>
        {data.result.assurance === 'limited' && <p role="alert">本次使用有限练习材料：测试挑战检查尚未完整通过，不能用于确认学习目标完成。</p>}
        <p>通过 {data.result.testSummary.passed} / {data.result.testSummary.total} 项测试 · 执行用时（合计） {data.result.resourceSummary.totalExecutionTimeMs != null ? `${data.result.resourceSummary.totalExecutionTimeMs} ms` : '未记录'} · 峰值内存 {data.result.resourceSummary.peakMemoryBytes != null && data.result.resourceSummary.peakMemoryBytes > 0 ? `${(data.result.resourceSummary.peakMemoryBytes / 1048576).toFixed(1)} MiB` : '未测得'}</p>
        {data.result.stderrCategory && <p>{data.result.stderrCategory === 'syntaxError' ? labels.syntaxError : data.result.stderrCategory === 'runtimeError' ? labels.runtimeErrorDetail : '触及运行资源或输出限制。'}</p>}
        <p>这个结果对应本次代码与测试材料，不代表学习目标已经完成。</p></div>}
      {data && 'runId' in data && data.result?.cases.map(item => <details key={item.index} open className="case-result"><summary>输入 {item.index} · {labels[item.outcome]}</summary>
        <div className="case-columns"><div><h3>输入{item.inputTruncated && '（截断）'}</h3><pre>{item.stdin || '（空输入）'}</pre></div>
          {item.expectedOutput !== null && <div><h3>预期输出{item.expectedTruncated && '（截断）'}</h3><pre>{item.expectedOutput || '（空输出）'}</pre></div>}
          <div><h3>实际输出{item.stdoutTruncated && '（截断）'}</h3><pre>{item.stdout || '（无输出）'}</pre></div></div>
        {item.stderr && <><h3>错误信息{item.stderrTruncated && '（截断）'}</h3><pre>{item.stderr}</pre></>}
        {item.wallTimeMs !== null && <p>执行用时 {item.executionTimeMs != null ? `${item.executionTimeMs} ms` : '未记录'}
          {item.peakMemoryBytes != null && item.peakMemoryBytes > 0 ? ` · 峰值内存 ${(item.peakMemoryBytes / 1048576).toFixed(1)} MiB` : ' · 峰值内存未测得'}</p>}</details>)}
      {oldCode && <details open><summary>代码版本 {oldCode.version}（只读）</summary><PythonEditor key={oldCode.version} value={oldCode.source} readOnly label="本次运行代码" /></details>}
    </section>
    <section className="panel" aria-label="运行与提交历史"><h2>运行与提交历史</h2>
      {history.isError && <p>历史读取失败。<button onClick={() => history.refetch()}>重新读取历史</button></p>}
      {history.data?.pages.flatMap(page => page.items).map(item => <button key={item.id} className="history-item" onClick={() => { setSelected(item); setOldCode(null); }}>
        {item.kind === 'run' ? '运行' : '正式提交'} · v{item.codeVersion} · {labels[item.verdict || item.status]} · {new Date(item.createdAt).toLocaleTimeString()}</button>)}
      {history.hasNextPage && <button disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>更早记录</button>}
    </section>
  </>;
}

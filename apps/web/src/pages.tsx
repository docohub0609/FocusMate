import { useEffect, useState, useRef } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  emptyIntake,
  intakeOf,
  errorText,
  loadLocal,
  saveLocal,
  removeLocal,
  categories,
} from "./data";
import type { Intake, RecordData, Feature, Report, Evidence } from "./data";
import { IntakeForm } from "./IntakeForm";
import { FeatureEditor, confirmationFeatures } from "./FeatureEditor";
import { repairing, type RepairStatus } from "./repairData";
const pathOf = (r: RecordData) =>
  `/problems/${r.problemPackage.problemId}/versions/${r.problemPackage.version}`;
type Listing = {
  items: {
    problemId: string;
    problemVersion: number;
    title?: string;
    preview: string;
    updatedAt: string;
    status: string;
  }[];
  nextCursor: string | null;
};
export function Home() {
  const client = useQueryClient();
  const [deleted, setDeleted] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<{ id: string; title: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [cursor, setCursor] = useState<string | undefined>();
  const list = useQuery({
    queryKey: ["problems", deleted, cursor],
    queryFn: () =>
      api<Listing>(
        "/problems?limit=20&deleted=" + deleted +
          (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""),
      ),
  });
  async function changeDeleted(id: string, remove: boolean) {
    setBusy(true);
    setMessage("");
    try {
      await api(`/problems/${id}${remove ? "" : "/restore"}`, {}, remove ? "DELETE" : "POST");
      setPendingDelete(null);
      setMessage(remove ? "题目已移入“已删除”，可以恢复。" : "题目已恢复到已保存列表。");
      setCursor(undefined);
      await client.invalidateQueries({ queryKey: ["problems"] });
    } catch {
      setMessage("未能确认操作结果，请刷新列表核对后重试。重复删除不会损坏题目记录。");
    } finally { setBusy(false); }
  }
  return (
    <>
      <div className="intro">
        <div className="eyebrow">做你的刷题小助手</div>
        <h1>先把题目准备好</h1>
        <p>录入题目，核对 AI 整理的条件，再查看测试数据的准备情况。</p>
        <Link className="button" to="/problems/new">
          录入新题目
        </Link>
        {!!loadLocal("intake:new") && (
          <Link className="text-link" to="/problems/new">
            继续本机草稿
          </Link>
        )}
      </div>
      <section className="panel">
        <div className="section-heading">
          <h2>{deleted ? "已删除题目" : "已保存题目"}</h2>
          <button className="secondary" disabled={busy} onClick={() => {
            setDeleted(!deleted); setCursor(undefined); setPendingDelete(null); setMessage("");
          }}>{deleted ? "返回已保存题目" : "查看已删除题目"}</button>
        </div>
        {message && <p role="status">{message}</p>}
        {pendingDelete && <div role="alertdialog" aria-modal="false" aria-label="删除题目确认" className="notice">
          <p>确定删除“{pendingDelete.title}”吗？这道题的所有版本会从已保存列表移除，可以在“已删除题目”中恢复。已有提交和学习记录保留。</p>
          <div className="actions">
            <button disabled={busy} onClick={() => changeDeleted(pendingDelete.id, true)}>确认删除</button>
            <button className="secondary" disabled={busy} onClick={() => setPendingDelete(null)}>取消</button>
          </div>
        </div>}
        <p className="muted">按最近更新时间排列，每页最多 20 道。</p>
        {list.isError && (
          <p role="alert">
            题目列表暂时无法读取。
            <button onClick={() => list.refetch()}>重试</button>
          </p>
        )}
        {list.isPending && <p>正在读取题目…</p>}
        {list.data?.items.length === 0 && (
          <p className="empty">{deleted ? "没有已删除的题目。" : "还没有保存的题目。从录入第一道题开始。"}</p>
        )}
        {list.data?.items.map((p) => (
          <div className="problem-item" key={p.problemId}>
          <Link
            className="problem-row"
            key={p.problemId}
            to={`/problems/${p.problemId}/versions/${p.problemVersion}`}
          >
            <div>
              <strong>{p.title?.trim() || p.preview}</strong>
              <small>
                版本 {p.problemVersion} ·{" "}
                {new Date(p.updatedAt).toLocaleString("zh-CN")}
              </small>
            </div>
            <span className="badge">
              {p.status === "frozen" ? "条件已确认" : "待核对"}
            </span>
          </Link>
          <button className="secondary" disabled={busy} aria-label={`${deleted ? "恢复" : "删除"}题目：${p.title?.trim() || p.preview}`}
            onClick={() => deleted ? changeDeleted(p.problemId, false) : setPendingDelete({ id: p.problemId, title: p.title?.trim() || p.preview })}>
            {deleted ? "恢复" : "删除"}
          </button>
          </div>
        ))}
        <div className="actions">
          {cursor && (
            <button className="secondary" onClick={() => setCursor(undefined)}>
              回到第一页
            </button>
          )}
          {list.data?.nextCursor && (
            <button
              className="secondary"
              onClick={() => setCursor(list.data!.nextCursor!)}
            >
              下一页
            </button>
          )}
        </div>
      </section>
    </>
  );
}
export function ProblemPage() {
  const { id, version, step } = useParams();
  return (
    <ProblemWorkspace key={`${id}:${version}`} id={id} version={version} step={step} />
  );
}
type Job = {
  jobId: string;
  status: string;
  updatedAt: string;
  failureCode?: string;
  failureDetails?: {field: string; message: string}[];
  request: { expectedDraftRevision: number; contentHash: string };
};
function ProblemWorkspace({ id, version, step: routeStep }: { id?: string; version?: string; step?: string }) {
  const navigate = useNavigate(),
    client = useQueryClient(),
    prefix = id ? `/problems/${id}/versions/${version}` : "";
  const localKey = id ? "intake:" + prefix : "intake:new";
  const cached = loadLocal<{ form: Intake; revision?: number }>(localKey);
  const [form, setForm] = useState<Intake>(
    cached?.form ? { ...emptyIntake, ...cached.form } : structuredClone(emptyIntake),
  );
  const [baseRevision, setBaseRevision] = useState<number | undefined>(
    cached?.revision,
  );
  const [reviewDrafts, setReviewDrafts] = useState<Record<string, Feature[]>>({});
  const [confirming, setConfirming] = useState(false);
  const [ready, setReady] = useState(!id),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [storageOk, setStorageOk] = useState(true);
  const record = useQuery({
    queryKey: [prefix],
    queryFn: () => api<RecordData>(prefix),
    enabled: !!id,
  });
  const frozen = record.data?.lifecycle.status === "frozen";
  const step = routeStep || (frozen ? "tests" : "edit");
  const goTo = (next: string) => {
    setMessage("");
    navigate(id ? prefix + "/" + next : "/problems/new");
  };
  useEffect(() => {
    if (id && record.data && !routeStep)
      navigate(prefix + (frozen ? "/tests" : "/edit"), { replace: true });
  }, [id, record.data, routeStep, frozen, prefix, navigate]);
  useEffect(() => { window.scrollTo(0, 0); }, [step]);
  const job = useQuery({
    queryKey: [prefix, "job"],
    queryFn: () => api<Job | null>(prefix + "/feature-extraction"),
    enabled: !!id && !frozen,
    refetchInterval: (q) =>
      q.state.data && ["queued", "running"].includes(q.state.data.status)
        ? 2000
        : false,
  });
  const jobMatchesDraft = job.data?.request.expectedDraftRevision === record.data?.lifecycle.draftRevision
    && job.data?.request.contentHash === record.data?.lifecycle.contentHash;
  const display = useQuery({
    queryKey: [
      prefix,
      "display",
      record.data?.lifecycle.draftRevision,
      job.data?.jobId,
      job.data?.status,
    ],
    queryFn: () =>
      api<{ report: Report; displayComplete: boolean }>(
        prefix + "/features/display",
      ),
    enabled: !!record.data && (!!frozen || (jobMatchesDraft && job.data?.status === "succeeded")),
  });
  const evidence = useQuery({
    queryKey: [prefix, "evidence", record.data?.lifecycle.draftRevision],
    queryFn: () => api<{ items: Evidence[] }>(prefix + "/evidence"),
    enabled: !!record.data,
  });
  useEffect(() => {
    if (record.data && !ready) {
      if (!cached) {
        setForm(intakeOf(record.data.problemPackage));
        setBaseRevision(record.data.lifecycle.draftRevision);
      }
      setReady(true);
    }
  }, [record.data, ready]);
  useEffect(() => {
    if (ready)
      setStorageOk(saveLocal(localKey, { form, revision: baseRevision }));
  }, [form, baseRevision, ready, localKey]);
  const conflict =
    !!record.data &&
    baseRevision !== undefined &&
    baseRevision !== record.data.lifecycle.draftRevision;
  const unchanged =
    !!record.data &&
    JSON.stringify(form) ===
      JSON.stringify(intakeOf(record.data.problemPackage));
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setMessage("");
    try {
      await action();
    } catch (e) {
      setMessage(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    await perform(async () => {
      if (!id) {
        let operation = loadLocal<{ key: string; body: Intake }>(
          "intake:create-operation",
        );
        if (!operation) {
          operation = { key: crypto.randomUUID(), body: form };
          if (!saveLocal("intake:create-operation", operation))
            throw new Error("storage");
        }
        let saved: RecordData;
        try {
          saved = await api<RecordData>(
            "/problems",
            operation.body,
            "POST",
            operation.key,
          );
        } catch (error) {
          if (
            error instanceof ApiError &&
            [400, 413, 415, 422].includes(error.status)
          )
            removeLocal("intake:create-operation");
          throw error;
        }
        if (JSON.stringify(form) !== JSON.stringify(operation.body))
          saveLocal("intake:" + pathOf(saved), {
            form,
            revision: saved.lifecycle.draftRevision,
          });
        removeLocal("intake:create-operation");
        removeLocal(localKey);
        client.invalidateQueries({ queryKey: ["problems"] });
        navigate(pathOf(saved) + (JSON.stringify(form) === JSON.stringify(operation.body) ? "/review" : "/edit"));
      } else if (!unchanged) {
        try {
          const saved = await api<RecordData>(
            prefix,
            { ...form, expectedDraftRevision: baseRevision },
            "PUT",
          );
          setBaseRevision(saved.lifecycle.draftRevision);
          client.setQueryData([prefix], saved);
          setMessage("题目已保存。请重新整理条件。");
        } catch (e) {
          const actual = await api<RecordData>(prefix);
          client.setQueryData([prefix], actual);
          if (
            JSON.stringify(intakeOf(actual.problemPackage)) ===
            JSON.stringify(form)
          ) {
            setBaseRevision(actual.lifecycle.draftRevision);
            setMessage("已核实：这份内容已保存。");
          } else throw e;
        }
        goTo("review");
      } else {
        goTo("review");
      }
    });
  }
  async function parse() {
    await perform(async () => {
      const keyName =
        "parse:" + prefix + ":" + record.data!.lifecycle.draftRevision;
      let key = loadLocal<string>(keyName);
      if (!key) {
        key = crypto.randomUUID();
        if (!saveLocal(keyName, key)) throw new Error("storage");
      }
      await api(
        "/feature-extraction-jobs",
        {
          problemId: id,
          problemVersion: Number(version),
          expectedDraftRevision: record.data!.lifecycle.draftRevision,
          contentHash: record.data!.lifecycle.contentHash,
        },
        "POST",
        key,
      );
      await job.refetch();
    });
  }
  const active = !!job.data && ["queued", "running"].includes(job.data.status);
  const canReview = !!record.data && (frozen || (unchanged && !conflict));
  const steps = [
    { value: "edit", label: "1　填写题目", allowed: true, reason: "" },
    { value: "review", label: "2　核对题目条件", allowed: canReview, reason: "请先保存题目并处理版本冲突" },
    { value: "tests", label: "3　准备测试数据", allowed: frozen, reason: "请先核对并确认题目条件" },
  ];
  return (
    <>
      <Link className="back" to="/">
        ← 所有题目
      </Link>
      <h1>{id ? record.data?.problemPackage.title?.trim() || "准备这道题" : "录入新题目"}</h1>
      {frozen && <p><Link className="start-coding" to={prefix + '/code'}>开始写代码 →</Link></p>}
      <nav aria-label="题目准备步骤">
        <ol className="steps">
          {steps.map((item) => <li key={item.value}>
            <button className="step-button" aria-current={step === item.value ? "step" : undefined}
              disabled={busy || confirming || !item.allowed} title={item.allowed ? undefined : item.reason}
              onClick={() => goTo(item.value)}>{item.label}</button>
          </li>)}
        </ol>
      </nav>
      {record.isError && (
        <p role="alert">
          题目读取失败。
          <button onClick={() => record.refetch()}>重新读取</button>
        </p>
      )}
      {!storageOk && <p role="alert">浏览器无法暂存内容，请勿关闭页面。</p>}
      {message && (
        <p role="status" className="notice">
          {message}
        </p>
      )}
      {ready && (
        <>
          {step === "edit" && <section className="panel">
            <div className="section-heading">
              <h2>填写题目</h2>
              <span className="badge">
                {frozen
                  ? "已确认，当前版本只读"
                  : unchanged
                    ? "已保存到本机后台"
                    : "仅暂存在此浏览器"}
              </span>
            </div>
            {conflict && (
              <div role="alert" className="notice">
                后台已有新的修改，本地内容保留在下方。请对照后再决定。
                <details>
                  <summary>查看后台题面</summary>
                  <IntakeForm
                    value={intakeOf(record.data!.problemPackage)}
                    disabled
                    onChange={() => {}}
                  />
                </details>
                <button
                  onClick={() => {
                    setForm(intakeOf(record.data!.problemPackage));
                    setBaseRevision(record.data!.lifecycle.draftRevision);
                  }}
                >
                  使用后台内容
                </button>
                <button
                  className="secondary"
                  onClick={() =>
                    setBaseRevision(record.data!.lifecycle.draftRevision)
                  }
                >
                  已核对，保留本地内容继续编辑
                </button>
              </div>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                save();
              }}
            >
              <IntakeForm
                value={frozen ? intakeOf(record.data!.problemPackage) : form}
                onChange={setForm}
                disabled={busy || confirming || frozen}
              />
              <div className="actions">
                {!frozen && (
                  <button disabled={busy || confirming || conflict || !ready}>
                    {unchanged ? "下一步：核对题目条件" : "保存并继续"}
                  </button>
                )}
                {frozen && (
                  <button
                    type="button"
                    onClick={() =>
                      perform(async () => {
                        const next = await api<RecordData>(
                          `/problems/${id}/versions`,
                          { baseVersion: Number(version) },
                        );
                        navigate(pathOf(next) + "/edit");
                      })
                    }
                  >
                    创建新版本修改题目
                  </button>
                )}
                {frozen && <button type="button" className="secondary" onClick={() => goTo("review")}>下一步：查看已确认条件</button>}
              </div>
            </form>
          </section>}
          {step === "review" && !canReview && <section className="panel">
            <h2>核对题目条件</h2><p>题面还有未保存的修改或版本冲突，请先回到填写页面处理。</p>
            <button onClick={() => goTo("edit")}>返回填写题目</button>
          </section>}
          {step === "review" && canReview && <>
          {!frozen && <section className="panel">
            {!display.data && <h2>核对题目条件</h2>}
            <p>题目已保存。让 AI 整理条件后，请逐项对照原文核对。</p>
            {!!job.data && !jobMatchesDraft && <p>题目格式或内容已修改，请按当前内容重新整理条件。</p>}
            <button disabled={busy || confirming || active} onClick={() => {
              if (job.data && !active) removeLocal("parse:" + prefix + ":" + record.data!.lifecycle.draftRevision);
              parse();
            }}>{active ? "正在整理…" : "让 AI 整理条件"}</button>
          </section>}
          {id && !frozen && (
            <p aria-live="polite">
              {active
                ? "AI 正在整理并核对题目条件，刷新页面可以继续查看。"
                : job.data && job.data.status !== "succeeded"
                  ? {
                      failed: "这次解析未能完成，可以重新解析。",
                      interrupted: "上次解析因服务中断而停止，请重新解析。",
                      stale: "题面已有变化，请保存后重新解析。",
                    }[job.data.status] || ""
                  : ""}
              {job.isError ? "暂时无法查询解析进度，保留上次状态。" : ""}
            </p>
          )}
          {job.data?.status === "failed" && jobMatchesDraft && <details className="notice">
            <summary>查看解析失败原因</summary>
            <p>{job.data.failureCode === "LLM_OUTPUT_INVALID" ? "AI 返回的条件未通过格式或引用检查。题目已保存，不需要重新录入。" : "本次整理未能完成，题目和本地修改稿仍保留。"}</p>
            {job.data.failureDetails?.length ? <ul>{job.data.failureDetails.map((item, i) => <li key={i}>{item.message}（位置：{item.field}）</li>)}</ul> : <p>这次记录没有提供更细的错误位置。</p>}
            <p>错误编号：{job.data.failureCode || "UNKNOWN"}</p>
          </details>}
          {display.data && evidence.data && (
            <Review
              key={display.data.report.featureSetId}
              prefix={prefix}
              record={record.data!}
              report={display.data.report}
              complete={display.data.displayComplete}
              canConfirm={unchanged && !conflict && !active}
              evidence={evidence.data.items}
              frozen={!!frozen}
              busy={confirming}
              setBusy={setConfirming}
              onUseFormat={(proposal) => {
                // Preparing an editable draft is not saving or freezing the suggestion.
                const next = { ...form, inputSpec: proposal.inputSpec, outputSpec: proposal.outputSpec, examples: proposal.examples };
                setForm(next);
                saveLocal(localKey, { form: next, revision: baseRevision });
                goTo("edit");
                setMessage("已将建议填入草稿，尚未保存。请核对输入说明、输出说明和全部样例，再保存并重新整理条件。");
              }}
              draft={reviewDrafts[display.data.report.featureSetId]}
              onDraftChange={(features) => setReviewDrafts((drafts) => ({ ...drafts, [display.data!.report.featureSetId]: features }))}
              onConfirmed={async (stillViewing) => {
                await record.refetch();
                await display.refetch();
                if (stillViewing()) goTo("tests");
              }}
            />
          )}
          {display.isError && (
            <p role="alert">
              条件报告暂时无法读取，请刷新重试。原题面仍然保留。
            </p>
          )}
          <div className="actions">
            <button className="secondary" disabled={confirming} onClick={() => goTo("edit")}>上一步：填写题目</button>
            {frozen && <button onClick={() => goTo("tests")}>下一步：准备测试数据</button>}
          </div>
          </>}
          {step === "tests" && frozen && <>
            <BuildStatus prefix={prefix} report={display.data?.report} />
            <button className="secondary" onClick={() => goTo("review")}>上一步：核对题目条件</button>
          </>}
          {step === "tests" && !frozen && <section className="panel">
            <h2>准备测试数据</h2><p>请先核对并确认题目条件，后台才能准备对应的测试数据。</p>
            <button onClick={() => goTo(canReview ? "review" : "edit")}>{canReview ? "返回核对题目条件" : "返回填写题目"}</button>
          </section>}
          {!["edit", "review", "tests"].includes(step) && <p>没有找到这个步骤。<button onClick={() => goTo("edit")}>返回填写题目</button></p>}
        </>
      )}
    </>
  );
}
function Review({
  prefix,
  record,
  report,
  complete,
  canConfirm,
  evidence,
  frozen,
  onConfirmed,
  draft,
  onDraftChange,
  busy,
  setBusy,
  onUseFormat,
}: {
  prefix: string;
  record: RecordData;
  report: Report;
  complete: boolean;
  canConfirm: boolean;
  evidence: Evidence[];
  frozen: boolean;
  onConfirmed: (stillViewing: () => boolean) => Promise<void>;
  draft?: Feature[];
  onDraftChange: (features: Feature[]) => void;
  busy: boolean;
  setBusy: (busy: boolean) => void;
  onUseFormat: (proposal: NonNullable<Report["ioProposal"]>["suggestion"]) => void;
}) {
  const sectionRef = useRef<HTMLElement>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const key = "features:" + prefix + ":" + report.featureSetId;
  const [previousId] = useState(() =>
    loadLocal<string>("features:" + prefix + ":latest"),
  );
  const previous =
    previousId && previousId !== report.featureSetId
      ? loadLocal<{ features: Feature[]; evidence: Evidence[] }>(
          "features:" + prefix + ":" + previousId,
        )
      : undefined;
  const local = loadLocal<{ reportId: string; features: Feature[] }>(key);
  const [features, setFeaturesState] = useState(
    draft ?? (local?.reportId === report.featureSetId ? local.features : report.features),
  );
  const setFeatures = (next: Feature[]) => { setFeaturesState(next); onDraftChange(next); };
  const [message, setMessage] = useState("");
  const [keepOriginal, setKeepOriginal] = useState(false);
  useEffect(() => {
    if (!frozen) {
      saveLocal(key, {
        reportId: report.featureSetId,
        revision: record.lifecycle.draftRevision,
        contentHash: report.problemContentHash,
        features,
        evidence,
      });
      saveLocal("features:" + prefix + ":latest", report.featureSetId);
    }
  }, [features, frozen, key, report.featureSetId]);
  async function confirm() {
    if (
      sectionRef.current &&
      !Array.from(
        sectionRef.current.querySelectorAll<
          HTMLInputElement | HTMLTextAreaElement
        >("input,textarea"),
      ).every((input) => input.reportValidity())
    )
      return;
    setBusy(true);
    setMessage("");
    try {
      await api(prefix + "/confirm-and-freeze", {
        expectedDraftRevision: record.lifecycle.draftRevision,
        basedOnFeatureSetId: report.featureSetId,
        requireCompletePresentation: true,
        ...(report.ioProposal && keepOriginal ? { ioProposalDecision: "keepOriginal" } : {}),
        features: confirmationFeatures(features),
      });
      removeLocal(key);
      await onConfirmed(() => mounted.current);
    } catch (e) {
      try {
        const actual = await api<RecordData>(prefix);
        if (actual.lifecycle.status === "frozen") {
          setMessage("后台已有确认结果，请查看当前保存的条件。");
          await onConfirmed(() => mounted.current);
        } else setMessage(errorText(e));
      } catch {
        setMessage(errorText(e));
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" ref={sectionRef}>
      <h2>核对题目条件</h2>
      <p>
        对照原文检查下面每项内容。题目未说明的内容可以保留未知；确认整份内容不会把推测变成事实。
      </p>
      {report.ioProposal && <section className="notice" aria-label="输入输出格式建议">
        <h3>请先核对输入输出格式</h3>
        <p><strong>下面是 AI 提出的 ACM 格式转换建议，尚未作为题目要求保存。</strong></p>
        <p>本平台使用标准输入输出：按行和空格组织数据，整数数组不用方括号或逗号。原题中的函数参数或数组表示也需要转换为可直接读写的文本。</p>
        <p>转换只改变输入输出的表示方式，原题的算法含义、约束和样例数据应保持不变。请对照原文核对后，再决定是否采用。</p>
        <p>{report.ioProposal.suggestion.reason}</p>
        {!frozen && <p>{report.semanticReview?.status === "completed"
          ? report.semanticReview.items.find((item) => item.itemId === "io-proposal")?.verdict === "supported"
            ? "另一轮 AI 检查未发现冲突，仍请核对每个样例的数据和格式。"
            : "另一轮 AI 检查发现冲突或依据不足，请修改建议后再使用。"
          : "这份建议尚未完成独立检查，请仔细核对。"}</p>}
        {([['原来填写的格式', report.ioProposal.original], ['建议采用的 ACM 格式', report.ioProposal.suggestion]] as const).map(([label, value]) => <div key={label}>
          <h4>{label}</h4>
          <p>输入说明</p><pre>{value.inputSpec}</pre>
          <p>输出说明</p><pre>{value.outputSpec}</pre>
          {value.examples.map((sample, index) => <div key={index}>
            <p>样例 {index + 1} 输入</p><pre>{sample.input || '（空输入）'}</pre>
            <p>样例 {index + 1} 输出</p><pre>{sample.output || '（空输出）'}</pre>
          </div>)}
        </div>)}
        <details><summary>查看提出建议时引用的原文</summary>
          {report.ioProposal.suggestion.evidenceRefs.map((ref) => <pre key={ref}>{evidence.find((item) => item.evidenceId === ref)?.sourceText || '原文暂时无法显示，请重新读取。'}</pre>)}
        </details>
        {!frozen ? <>
          <button type="button" disabled={busy || !canConfirm} onClick={() => onUseFormat(report.ioProposal!.suggestion)}>将建议填入草稿，继续核对</button>
          <p>填写后还需保存，并按新格式重新整理条件。测试数据将使用最终确认的输入、输出和样例。</p>
          <label><input type="checkbox" checked={keepOriginal} disabled={busy || !canConfirm} onChange={(e) => setKeepOriginal(e.target.checked)} />我已核对，保留原来填写的格式，不采用这份建议</label>
        </> : <p>确认时选择了保留原格式；上面的建议没有应用到题目。</p>}
      </section>}
      {previous && (
        <p className="notice">
          后台报告已更新，旧修改稿仍保留在浏览器中，本次展示新报告。
        </p>
      )}
      {previous && (
        <details>
          <summary>对照上一次保留的修改稿（只读）</summary>
          <FeatureEditor
            features={previous.features}
            originals={previous.features}
            evidence={previous.evidence || []}
            readonly
            change={() => {}}
          />
        </details>
      )}
      {!complete && (
        <p role="alert" className="notice">
          这份旧报告缺少完整中文展示信息。
          {frozen
            ? "已确认内容保持只读，不会被自动改写。"
            : "请重新解析后再确认，避免提交尚未完整展示的内容。"}
        </p>
      )}
      {!frozen && !canConfirm && (
        <p className="notice">
          题面尚有未保存的修改或解析仍在进行。请先完成保存与解析，再核对对应的条件。
        </p>
      )}
      <FeatureEditor
        features={frozen ? report.features : features}
        originals={report.features}
        evidence={evidence}
        change={setFeatures}
        readonly={frozen || busy || !complete}
      />
      {message && <p role="alert">{message}</p>}
      {!frozen && (
        <button
          disabled={
            busy ||
            !complete ||
            !canConfirm ||
            (!!report.ioProposal && !keepOriginal) ||
            features.some((f) => f.status === "conflict")
          }
          onClick={confirm}
        >
          {busy ? "正在确认…" : "确认以上全部内容"}
        </button>
      )}
    </section>
  );
}
const buildIssueMessages: Record<string, string> = {
  PROBLEM_FACTS_INSUFFICIENT: "题目条件还不足以准备测试数据。请补充并确认题目条件。",
  LLM_TEMPORARILY_UNAVAILABLE: "AI 服务暂时不可用，本次准备已停止。可以稍后重新准备。",
  LLM_CONFIGURATION_REQUIRED: "AI 服务的配置或账户访问不可用，请检查配置和账户状态。",
  VERIFIER_BUILDER_INTERNAL_ERROR: "测试准备服务发生异常，本次检查未完成。可以检查服务后重新准备。",
  CANDIDATE_SCHEMA_INVALID: "AI 返回的测试程序材料格式不符合要求，后台无法读取。可以重新准备。",
  CANDIDATE_COMPILE_FAILED: "生成的测试程序未通过编译检查。可以重新准备，生成新的测试程序。",
  GENERATOR_NON_DETERMINISTIC: "相同条件下生成的测试输入不一致，无法复核。本次材料暂不能用于判题。",
  GENERATOR_CASES_INVALID: "生成的测试输入结构或大小不符合要求，暂不能用于判题。可以重新准备。",
  GENERATOR_COVERAGE_INSUFFICIENT: "调整测试数据后仍缺少必要的测试类型或数量，尚不能用于判题。可以重新准备。",
  SMALL_REFERENCE_TIMEOUT: "调整测试数据后，小用例的参考程序仍执行超时，尚未得到可比较的答案。可以重新准备。",
  GENERATOR_REPAIR_INVALID: "AI 未能返回可用的测试数据修正，前面生成的程序已保留。可以重新准备。",
  GENERATOR_INPUT_LEGALITY_UNCONFIRMED: "调整后的测试输入尚未确认符合题目条件，暂不能用于判题。可以重新准备。",
  REFERENCE_PUBLIC_MISMATCH: "生成的参考程序与公开样例不一致，请重新准备。",
  REFERENCE_EXECUTION_FAILED: "生成的参考程序未能在执行限制内正常完成，尚未得到可比较的答案。请重新准备。",
  REFERENCE_DISAGREEMENT: "生成的两个参考程序给出了不同答案，暂不能用于判题。请重新准备。",
  MUTATION_RESPONSE_INVALID: "AI 返回的检查材料格式无效或被截断，后台无法读取，检查已暂停。前面生成的程序已保留，可以重新准备。",
  MUTATION_TEST_GAP: "已确认有错误程序通过了这批测试，暂不能用于判题。",
  MUTATION_EVIDENCE_INSUFFICIENT: "生成的测试未能通过错误识别检查，暂不能用于判题。请重新准备一批测试材料。",
  SANDBOX_STARTUP_TIMEOUT: "代码运行环境启动超时，请检查 Docker 是否正常运行后重试。",
  BUILD_BUDGET_EXCEEDED: "测试准备超过了本次时间限制，尚未完成全部检查。已保存的题目和条件不受影响。",
  BUILDER_LEASE_EXPIRED: "测试准备服务中断或未及时更新状态，本次检查未完成。可以重新准备。",
  SEALED_MATERIAL_MISMATCH: "已保存的测试材料与检查记录不一致，暂不能用于判题。请重新准备。",
};

function BuildStatus({ prefix, report }: { prefix: string; report?: Report }) {
  const status = useQuery({
    queryKey: [prefix, "build"],
    queryFn: () =>
      api<{
        status: string;
        phase: string | null;
        repair?: RepairStatus | null;
        issueCode: string | null;
        retryable: boolean;
        allowedAction: string;
        assurance?: "full" | "limited";
        warningCodes?: string[];
        updatedAt: string;
      }>(prefix + "/verifier-asset-build"),
    refetchInterval: (q) =>
      !q.state.data || ["queued", "running"].includes(q.state.data.status)
        ? 2000
        : false,
  });
  const [error, setError] = useState("");
  const d = status.data;
  const autoRepairing = d?.repair ? repairing(d.repair) : d?.phase === "repairingCases";
  const missing = ["dataScale", "inputStructure", "outputStructure"].filter(
    (c) =>
      report &&
      !report.features.some(
        (f) => f.category === c && f.status === "confirmed",
      ),
  );
  return (
    <section className="panel">
      <h2>准备测试数据</h2>
      <h3>
        {d
          ? {
              queued: "等待后台开始",
              running: autoRepairing ? "正在自动修复" : "正在准备与检查",
              completed: "测试数据已就绪",
              paused: "准备工作暂时停止",
              failed: "测试数据尚未准备完成",
            }[d.status]
          : status.error instanceof ApiError &&
              status.error.code === "BUILD_JOB_NOT_FOUND"
            ? "等待后台开始"
            : "正在查询准备状态…"}
      </h3>
      {d?.status === "running" && autoRepairing && (
        <p role="status">
          {d.repair?.reason || "已有参考程序已保留，正在修正测试输入；修正后会重新检查。"}
        </p>
      )}
      {d?.status === "completed" && d.assurance === "limited" && (
        <p className="notice" role="status">
          测试数据可用于练习，但一次修正后仍未找到合适的错误程序，尚未验证这些测试能否识别这类错误。
          通过本批测试不能直接作为目标已完成的依据。
        </p>
      )}
      {missing.length > 0 && (
        <p className="notice">
          还缺少已确认的{missing.map((c) => categories[c]).join("、")}
          。请创建新版本补充题目并重新核对条件。
        </p>
      )}
      {d?.issueCode && (
        <p>
          {d.repair && ["failed", "abstained", "interrupted", "exhausted"].includes(d.repair.status) ? d.repair.reason : buildIssueMessages[d.issueCode] ??
            (d.allowedAction === "configureProvider"
              ? buildIssueMessages.LLM_CONFIGURATION_REQUIRED
              : "这次准备未能完成，已保存的题目和条件不受影响。")}
        </p>
      )}
      {d?.status === "queued" && (
        <p>确认已保存。测试准备服务启动后会处理这道题。</p>
      )}
      {status.error instanceof ApiError &&
      status.error.code === "BUILD_JOB_NOT_FOUND" ? (
        <p>条件已确认，等待后台开始准备测试数据。</p>
      ) : (
        status.isError && (
          <p role="alert">
            暂时无法查询，下面保留最后一次已知状态。
            <button onClick={() => status.refetch()}>重新查询</button>
          </p>
        )
      )}
      {d && (
        <small>
          上次状态更新时间：{new Date(d.updatedAt).toLocaleString("zh-CN")}
        </small>
      )}
      {d?.retryable && d.allowedAction === "retry" && (
        <button
          onClick={async () => {
            try {
              await api(prefix + "/verifier-asset-build/retry", {});
              await status.refetch();
            } catch (e) {
              setError(errorText(e));
            }
          }}
        >
          重新准备
        </button>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

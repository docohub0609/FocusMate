export type Json =
  string | number | boolean | null | Json[] | { [key: string]: Json };
export type Intake = {
  title: string;
  statement: string;
  inputSpec: string;
  outputSpec: string;
  constraints: string;
  examples: { input: string; output: string }[];
  language: "python3";
  ioMode: "standardInputOutput";
};
export type Problem = Intake & {
  problemId: string;
  version: number;
  verifierAssetStatus: string;
};
export type RecordData = {
  problemPackage: Problem;
  lifecycle: { status: string; draftRevision: number; contentHash: string };
};
export type Evidence = { evidenceId: string; sourceText: string };
export type Feature = {
  featureKey: string;
  category: string;
  status: string;
  reason: string;
  value?: Json;
  confidence?: number;
  evidence?: Evidence[];
  evidenceRefs?: string[];
  presentation?: { title: string; labels: Record<string, string> };
  review?: { message: string };
  candidates?: { value: Json; evidence: Evidence[] }[];
};
export type Report = {
  featureSetId: string;
  problemContentHash: string;
  features: Feature[];
  ioProposal?: {
    original: Pick<Intake, "inputSpec" | "outputSpec" | "examples">;
    suggestion: Pick<Intake, "inputSpec" | "outputSpec" | "examples"> & { reason: string; evidenceRefs: string[] };
  };
  ioProposalDecision?: "keepOriginal";
  semanticReview?: { status: string; items: { itemId: string; verdict: string }[] };
};
export const emptyIntake: Intake = {
  title: "",
  statement: "",
  inputSpec: "",
  outputSpec: "",
  constraints: "",
  examples: [{ input: "", output: "" }],
  language: "python3",
  ioMode: "standardInputOutput",
};
export function intakeOf(p: Problem): Intake {
  return {
    title: p.title || "",
    statement: p.statement,
    inputSpec: p.inputSpec,
    outputSpec: p.outputSpec,
    constraints: p.constraints,
    examples: p.examples,
    language: "python3",
    ioMode: "standardInputOutput",
  };
}
export class ApiError extends Error {
  constructor(
    public code: string,
    public fields: { field: string; message: string }[] = [],
    public status = 0,
  ) {
    super(code);
  }
}
export async function api<T>(
  path: string,
  body?: unknown,
  method = "POST",
  key?: string,
): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    method: body === undefined ? "GET" : method,
    headers:
      body === undefined
        ? {}
        : {
            "Content-Type": "application/json",
            ...(key ? { "Idempotency-Key": key } : {}),
          },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      data.code || "REQUEST_FAILED",
      data.fieldErrors || [],
      response.status,
    );
  return data;
}
const messages: Record<string, string> = {
  IDEMPOTENCY_CONFLICT: "这次保存的内容与上次请求不同，请先核对已保存的题目。",
  DRAFT_REVISION_CONFLICT:
    "后台题目已发生变化。本地内容已保留，请先核对最新题面。",
  PROBLEM_VERSION_FROZEN: "这版题目已确认，请创建新版本后修改。",
  INVALID_FEATURE_CONFIRMATION: "请检查条件、原文依据和未解决的疑点。",
  LLM_PROVIDER_UNAVAILABLE: "解析服务暂时不可用，请稍后重新解析。",
  EXTRACTION_BUSY: "已有解析正在进行，请等待结果。",
};
export function errorText(error: unknown) {
  return error instanceof ApiError
    ? messages[error.code] || "本次操作未完成，请检查填写内容或稍后重试。"
    : "连接中断，尚未确认操作结果。请保留当前内容后重试。";
}
export function loadLocal<T>(key: string): T | undefined {
  try {
    return JSON.parse(localStorage.getItem(key) || "null") || undefined;
  } catch {
    return undefined;
  }
}
export function saveLocal(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
export function removeLocal(key: string) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* Browser storage can be disabled. */
  }
}
export const categories: Record<string, string> = {
  dataScale: "数据范围",
  inputStructure: "输入格式",
  outputStructure: "输出要求",
  resourceConstraint: "时间与内存",
  domainTag: "题目特点",
};

import type { Json, Feature, Evidence } from "./data";
import { useState, useEffect } from "react";
import { categories } from "./data";
const enumLabels: Record<string, string> = {
  array: "数组",
  string: "字符串",
  matrix: "矩阵",
  tree: "树",
  graph: "图",
  interval: "区间",
  grid: "网格",
  operations: "操作序列",
  integer: "整数",
  integerSequence: "整数序列",
  sequence: "序列",
  leftBoundary: "左边界",
  rightBoundary: "右边界",
  space: "空格",
  newline: "换行",
};
const pointer = (key: string) => key.replace(/~/g, "~0").replace(/\//g, "~1");
function NumberValue({
  value,
  disabled,
  onChange,
}: {
  value: number;
  disabled: boolean;
  onChange: (v: number) => void;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  return (
    <input
      type="number"
      step="any"
      required
      disabled={disabled}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        if (e.target.value !== "" && Number.isFinite(e.target.valueAsNumber))
          onChange(e.target.valueAsNumber);
      }}
    />
  );
}
function ValueEditor({
  value,
  path,
  labels,
  onChange,
  disabled,
}: {
  value: Json;
  path: string;
  labels: Record<string, string>;
  onChange: (v: Json) => void;
  disabled: boolean;
}) {
  const label = labels[path] || "未提供中文名称";
  if (value !== null && typeof value === "object")
    return (
      <fieldset className="values" disabled={disabled}>
        <legend>{label}</legend>
        {Object.entries(value).map(([key, child]) => (
          <ValueEditor
            key={key}
            value={child}
            path={`${path}/${pointer(key)}`}
            labels={labels}
            disabled={disabled}
            onChange={(v) => {
              const next = structuredClone(value);
              if (Array.isArray(next)) next[Number(key)] = v;
              else next[key] = v;
              onChange(next);
            }}
          />
        ))}
      </fieldset>
    );
  if (typeof value === "boolean")
    return (
      <label>
        {label}
        <select
          disabled={disabled}
          value={String(value)}
          onChange={(e) => onChange(e.target.value === "true")}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
    );
  if (typeof value === "string" && enumLabels[value])
    return (
      <label>
        {label}
        <select
          disabled={disabled}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          {(value === "integer"
            ? ["integer"]
            : value === "space" || value === "newline"
              ? ["space", "newline"]
              : value === "leftBoundary" || value === "rightBoundary"
                ? ["leftBoundary", "rightBoundary"]
                : value === "sequence" || value === "integerSequence"
                  ? ["sequence", "integerSequence"]
                  : [
                      "array",
                      "string",
                      "matrix",
                      "tree",
                      "graph",
                      "interval",
                      "grid",
                      "operations",
                    ]
          ).map((v) => (
            <option key={v} value={v}>
              {enumLabels[v]}
            </option>
          ))}
        </select>
      </label>
    );
  if (value === null) return <p>{label}：未提供</p>;
  if (typeof value === "number")
    return (
      <label>
        {label}
        <NumberValue value={value} disabled={disabled} onChange={onChange} />
      </label>
    );
  return (
    <label>
      {label}
      <input
        disabled={disabled}
        type="text"
        required
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
        }}
      />
    </label>
  );
}
export function confirmationFeatures(features: Feature[]) {
  return features.map((f) => ({
    featureKey: f.featureKey,
    category: f.category,
    status: f.status,
    reason: f.reason,
    evidenceRefs: f.evidenceRefs ?? f.evidence?.map((e) => e.evidenceId) ?? [],
    ...(f.value !== undefined
      ? {
          value: f.value,
          confidence: f.status === "confirmed" ? 1 : (f.confidence ?? 0.5),
        }
      : {}),
    ...(f.presentation ? { presentation: f.presentation } : {}),
  }));
}
export function FeatureEditor({
  features,
  originals,
  evidence,
  change,
  readonly,
}: {
  features: Feature[];
  originals: Feature[];
  evidence: Evidence[];
  change: (f: Feature[]) => void;
  readonly: boolean;
}) {
  const set = (index: number, f: Feature) =>
    change(features.map((old, i) => (i === index ? f : old)));
  return (
    <>
      {features.map((f, i) => (
        <section className="feature" key={f.featureKey}>
          <div className="eyebrow">{categories[f.category]}</div>
          <h3>{f.presentation?.title || categories[f.category]}</h3>
          {f.review && <p className="notice">待核对：{f.review.message}</p>}
          <label>
            这项条件的确定程度
            <select
              disabled={readonly || f.status === "conflict"}
              value={f.status}
              onChange={(e) => {
                const next = { ...f, status: e.target.value };
                if (next.status === "unknown") {
                  delete next.value;
                  delete next.confidence;
                  next.evidenceRefs = [];
                  next.presentation = {
                    title: f.presentation?.title || categories[f.category],
                    labels: {},
                  };
                }
                set(i, next);
              }}
            >
              <option value="confirmed" disabled={f.value === undefined}>
                原文已明确
              </option>
              <option value="hypothesis" disabled={f.value === undefined}>
                暂作推测
              </option>
              <option value="unknown">题目未说明</option>
              {f.status === "conflict" && (
                <option value="conflict">存在不同解释</option>
              )}
            </select>
          </label>
          {!readonly &&
            f.status === "unknown" &&
            originals.find((x) => x.featureKey === f.featureKey)?.value !==
              undefined && (
              <button
                className="secondary"
                onClick={() =>
                  set(
                    i,
                    structuredClone(
                      originals.find((x) => x.featureKey === f.featureKey)!,
                    ),
                  )
                }
              >
                恢复原解析内容
              </button>
            )}
          {f.value !== undefined && (
            <ValueEditor
              value={f.value}
              path="/value"
              labels={f.presentation?.labels || {}}
              disabled={readonly}
              onChange={(value) => set(i, { ...f, value })}
            />
          )}
          {f.candidates?.map((c, j) => (
            <div className="candidate" key={j}>
              <ValueEditor
                value={c.value}
                path={`/candidates/${j}/value`}
                labels={f.presentation?.labels || {}}
                disabled
                onChange={() => {}}
              />
              <blockquote>
                {c.evidence.map((e) => e.sourceText).join("\n")}
              </blockquote>
              {!readonly && (
                <button
                  className="secondary"
                  onClick={() => {
                    const prefix = `/candidates/${j}/value`;
                    const labels = Object.fromEntries(
                      Object.entries(f.presentation?.labels || {})
                        .filter(([p]) => p.startsWith(prefix))
                        .map(([p, v]) => [p.replace(prefix, "/value"), v]),
                    );
                    const next = {
                      ...f,
                      status: "confirmed",
                      value: c.value,
                      confidence: 1,
                      evidenceRefs: c.evidence.map((e) => e.evidenceId),
                      presentation: {
                        title: f.presentation?.title || categories[f.category],
                        labels,
                      },
                    };
                    delete next.candidates;
                    set(i, next);
                  }}
                >
                  采用这项解释
                </button>
              )}
            </div>
          ))}
          {f.status === "conflict" && !readonly && (
            <button
              className="quiet"
              onClick={() => {
                const next = {
                  ...f,
                  status: "unknown",
                  evidenceRefs: [],
                  presentation: {
                    title: f.presentation?.title || categories[f.category],
                    labels: {},
                  },
                };
                delete next.candidates;
                set(i, next);
              }}
            >
              暂不能确定，保留未知
            </button>
          )}
          <label>
            说明
            <textarea
              required
              disabled={readonly}
              value={f.reason}
              onChange={(e) => set(i, { ...f, reason: e.target.value })}
            />
          </label>
          <fieldset disabled={readonly || f.status === "unknown"}>
            <legend>对应原文</legend>
            {evidence.map((e) => (
              <label className="check" key={e.evidenceId}>
                <input
                  type="checkbox"
                  checked={(
                    f.evidenceRefs ??
                    f.evidence?.map((x) => x.evidenceId) ??
                    []
                  ).includes(e.evidenceId)}
                  onChange={(ev) => {
                    const refs =
                      f.evidenceRefs ??
                      f.evidence?.map((x) => x.evidenceId) ??
                      [];
                    set(i, {
                      ...f,
                      evidenceRefs: ev.target.checked
                        ? [...refs, e.evidenceId]
                        : refs.filter((r) => r !== e.evidenceId),
                    });
                  }}
                />
                <span>{e.sourceText}</span>
              </label>
            ))}
          </fieldset>
        </section>
      ))}
    </>
  );
}

import type { Intake } from "./data";
export function IntakeForm({
  value,
  onChange,
  disabled = false,
}: {
  value: Intake;
  onChange: (v: Intake) => void;
  disabled?: boolean;
}) {
  const names = {
    statement: "题目描述",
    inputSpec: "输入说明",
    outputSpec: "输出说明",
    constraints: "约束条件",
  };
  return (
    <fieldset disabled={disabled}>
      <div className="form-grid">
        <label className="wide">
          题目名称（选填）
          <input aria-label="题目名称（选填）" maxLength={120}
            placeholder="例如：两数之和。不填写也可以保存。"
            value={value.title || ""} onChange={(e) => onChange({ ...value, title: e.target.value })} />
        </label>
        {Object.entries(names).map(([key, label]) => (
          <label className={key === "statement" ? "wide" : ""} key={key}>
            {label}
            <textarea
              aria-label={label}
              required
              rows={key === "statement" ? 6 : 3}
              value={value[key as keyof typeof names]}
              onChange={(e) => onChange({ ...value, [key]: e.target.value })}
              placeholder={
                key === "constraints"
                  ? "照录题目的限制；未说明时填写“题目未说明”。"
                  : `填写${label}`
              }
            />
            {!value[key as keyof typeof names].trim() && (
              <small>请填写{label}</small>
            )}
          </label>
        ))}
      </div>
      <h3>公开样例</h3>
      {value.examples.map((sample, index) => (
        <div className="sample" key={index}>
          <strong>样例 {index + 1}</strong>
          <div className="form-grid">
            <label>
              样例输入
              <textarea
                rows={3}
                value={sample.input}
                onChange={(e) =>
                  onChange({
                    ...value,
                    examples: value.examples.map((s, i) =>
                      i === index ? { ...s, input: e.target.value } : s,
                    ),
                  })
                }
              />
            </label>
            <label>
              样例输出
              <textarea
                rows={3}
                value={sample.output}
                onChange={(e) =>
                  onChange({
                    ...value,
                    examples: value.examples.map((s, i) =>
                      i === index ? { ...s, output: e.target.value } : s,
                    ),
                  })
                }
              />
            </label>
          </div>
          {value.examples.length > 1 && (
            <button
              type="button"
              className="quiet"
              onClick={() =>
                onChange({
                  ...value,
                  examples: value.examples.filter((_, i) => i !== index),
                })
              }
            >
              删除样例 {index + 1}
            </button>
          )}
        </div>
      ))}
      <button
        type="button"
        className="secondary"
        onClick={() =>
          onChange({
            ...value,
            examples: [...value.examples, { input: "", output: "" }],
          })
        }
      >
        添加样例
      </button>
    </fieldset>
  );
}

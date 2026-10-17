# 合同审核回归样例

`cases.json` 保存24份完整输入快照及外置预期。开发组：两数之和、图连通性、区间合并；保留组：数组去重、括号匹配、网格路径。每题一份正确合同、两个单点错误、一份仅增加非必要未知的合同。前三题原文复用 `examples/goal-compilation`，后三题原文位于本目录 `problems`。

六类判定依次使用 `numericMismatch`（数值）、`conditionMismatch`（条件/对象）、`missingRequirement`（遗漏）、`prescribedStrategy`（强制算法）、`unverifiableObjective`（目标与细则模糊）、`insufficientEvidence`（依据不能证明结论）。每个分组都包含六类。

在首次真实调用前声明可接受的相关分类：两数之和的位置/数值混淆也接受 `objectMismatch`；范围条件遗漏也接受 `conditionMismatch`；样例被当作整体证明也接受 `inappropriateEvidence`。每案的 `acceptableIssueTypes` 与 `acceptableItemIds` 同时限制命中，错误分类必须落在相关目标或原文扫描项，单纯返回 `needsReview` 不算命中。

只有六个开发组错误的代表判定获用户认可，标为 `representativeApproved`；不是这六份完整数据都逐字人工审核。其余18份标为 `aiPreparedUnreviewed`。全部Feature报告都明确标为 `syntheticFixture`：为满足已有确认报告接口，内部 `humanCreated` 只是模拟输入状态，不能当成人工评测证据。生成记录也明示未调用模型。构建过程不调用T041编译器，也不读写业务数据库。

完整正确基线包括输入格式、输出条件、取值规模、题面特殊规则、五阶段及样例范围。区间遗漏案例删去唯一的“与原区间完全相同覆盖范围”细则；其它验收正文没有把它重新写回。保留“覆盖每个原区间”和“结果互不相交”，因此错误输出 `[1,6]` 会符合残余条件，但把输入 `[1,2]`、`[5,6]` 之间的空白填上。图题正确基线故意保留宽标题“充分掌握图的连通性”，其内部细则可观察；仅当这些细则也都变成空话才预期阻止。

从仓库根目录运行 `python tools/eval-goal-contract-review.py --output <新JSON路径> --split development --env-file <本地配置路径>`。默认仅运行开发组，可显式选择 `holdout` 或 `all`。每份顺序调用真实审核器，逐案输出进度；原始输入、原始报告、预期、模型、prompt版本、耗时和案例hash写入新结果文件，绝不覆盖历史。预期及来源标签不传给审核器。

每完成一份，先追加并刷新 `<输出路径>.jsonl` 原始日志，再更新本次拥有的新JSON文件。运行中 `completed=false` 且 `processedCount` 表示已完成数；全部处理完才改为 `completed=true`。中断不会把已完成的付费调用藏到内存里；即使中断发生在汇总文件写入过程中，也可从逐案日志恢复已保存响应。重新运行必须使用新路径，不覆盖已有结果或日志。

计数分开记录 `missedError`（错误合同被放行）、`falseAlarm`（正确/非必要未知被拦）、`unavailable`、`inputError`、`evaluationError` 和 `expectedIssueMatch`。输入验证拒绝不算模型检出；类型或位置不匹配虽可能阻止冻结，仍不算预期命中。无响应时不能据此宣称准确。

此处的单元测试只验证数据、边界和计数，不代表模型准确率。真实保留组必须等审核提示词冻结后首次运行；查看其结果并据此调参后，这组转为回归集。该24例只支持小集判断，不能推导市场准确率或正确性证明。

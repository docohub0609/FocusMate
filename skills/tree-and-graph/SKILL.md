---
name: tree-and-graph
description: 依据当前请求、题目事实、代码快照与运行证据检查树图建模和遍历边界。
---
# 树与图

## 适用条件

当前请求涉及节点、边、连通、路径、树或树图代码时适用；只问数组求和时不因历史图题标签强行使用。

只询问改名、排版等维护操作时不适用，不因附带图题代码转成遍历检查。

## 目标

检查当前材料，给出有证据的局部发现与未知项，帮助用户推进当前 Goal/subgoal；不判断 Goal 已完成。

## 所需输入

按[输入 Schema](schemas/input.schema.json)接收 userRequest、goalRequirement、problemFacts、code、runEvidence。代码与运行证据可为空。外层保证 ref 唯一；运行证据只证明其 codeSnapshotRef 对应快照。

## 处理方法

先依据当前请求与题目事实确定要解释的局部问题。调用方已提供小节时，只使用这些已提供的检查步骤，不重新选择或要求读取其他小节；若调用方提供完整包，只展开与当前问题相关的内容。[分节索引](references/index.json)供外层选择程序使用，不是本次分析必须逐项执行的清单。缺少影响判断的材料时，明确写入 unknowns；未提供的小节不是待补做的任务。

沿代码真实语句顺序说明关键状态与依据；局部判断已足够回答时结束，不附加下一步任务。只有缺口确实影响当前回答时才提出具体核对方向或写 unknowns；无关的未展示分支不列为缺口。材料支持正确时直接肯定，允许 unknowns 为空。

每条发现引用传入 ref，区分静态推演、用户报告与实测；没有执行不声称复现。旧 codeSnapshotRef 的运行记录不能证明当前快照；缺少运行不妨碍有依据的静态分析。

## 禁止行为

不返回完整代码、完整题解或完整推导，不强迫替换用户合理方法。不修改 GoalState，不把分析或 Schema 合规当作 verifier。requiredTools 为空，不执行命令、不读取隐藏测试或无关信息。

## 结果格式

按[输出 Schema](schemas/result.schema.json)返回 applicability、findings、unknowns。applicability 与每条 finding 必须带证据引用；没有有依据发现时 findings 为空。

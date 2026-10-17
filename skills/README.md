# 版本化 Skill

Skill 是提供给教练的方法说明，包含适用范围、所需材料、分析步骤和结果格式。它不授予工具权限，不直接修改 Goal 完成状态。运行时按明确版本与内容指纹读取，候选判断仍需验证。

九类方法覆盖题意、复杂度、数组与哈希、双指针与窗口、二分、栈与队列、树与图、动态规划、测试与边界。领域方法正文位于各目录的 `SKILL.md` 和 `references/`，`descriptor.json` 保存版本、能力和内容指纹。当前 V1 不自动升级这些包。

## 检查包

```bash
python tools/check-skill.py skills/tree-and-graph --json
python tools/list-skills.py --json
```

包只包含 Markdown、文本和 JSON；工具执行由后台单独检查权限。程序会拒绝路径越界、符号链接、身份冲突和指纹不符。修改包正文须显式更新版本和指纹；不要静默覆盖已用于历史记录的内容。

包检查只证明结构与引用有效，不证明辅导内容正确。独立评测工具及模拟测试的区别见[测试说明](../tests/README.md)。

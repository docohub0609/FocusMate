# 测试说明

`npm run check` 运行架构边界、Node/Python 测试、网页类型检查和构建。首次安装请使用 `python -m pip install -e ".[dev]"`。

- `python/`：API、存储、任务恢复、模型候选校验和执行边界测试。
- `node/`：启动与网页服务测试。
- `fixtures/`：独立构造的回归样例，不依赖个人数据库或开发日志。旧报告格式样例是合成材料。
- `tools/experiments/*browser*`：网页交互回归，默认模拟模型与 Runner，并创建隔离数据库。

真实 Docker：先构建 Runner，再运行 `python tools/run-oj-integration.py`。浏览器编辑器测试设置 `T081_REAL_DOCKER=1` 可验证真实执行；故障恢复场景仍使用模拟 Runner。`RUN_DOCKER_OJ_INTEGRATION=1` 可启用对应 Python 集成测试。

真实模型测试会产生费用。各工具使用显式配置，不应读取用户现有题目做实验。Skill 评测 CLI 的 `--case-audit`、`--fixture-audit` 和 `--approval` 需要调用者提供实际审核记录；单元测试只在临时目录创建模拟审核输入，不代表人工批准或模型质量。

模拟通过、真实执行通过、模型建议有帮助是不同结论。跳过的集成测试必须单列，不计作通过。

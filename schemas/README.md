# 数据契约

本目录保存 JSON Schema Draft 2020-12 契约。Python 校验器通过本地注册表解析引用，不依赖联网下载 Schema。

- `common/`：Goal 上下文与错误。
- `problem/`：题目、版本和条件整理。
- `goal/`：Goal 合同、状态和工作目标。
- `process/`：事件、观察和帮助。
- `skills/`：Skill 描述及调用。
- `execution/`：提交、判题、测试资产和验证结果。
- `context/`：模型输入快照与对话摘要。
- `trace/`：决策记录结构。

其余子目录按相应业务划分。Schema 中的示例是非生产测试数据；实际事件、代码和报告保存在运行数据库，不写入本目录。

[契约测试](../tests/python/test_schema_contracts.py)检查字段、引用、事件映射和完成依据。结构通过不代表内容正确；业务服务还会核对版本、权限及可信验证来源。

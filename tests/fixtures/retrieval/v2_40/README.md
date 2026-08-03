# Query V2 已审核 40 条历史归档 Fixture

本 fixture 固化 2026-07-31 审核的 40 条 Query V2 标签：36 条可回答查询各对应一个历史 `source_capsule`，另有 4 条无答案查询。它只用于验证 archive scope 的历史回归，不是 active/raw 检索基线；v1 和 capsule worker 已退役。

- `cases.jsonl`：短路径标签，供 CI 直接加载。
- `cases.manifest.json`：数据集版本与阈值。
- `fixture-map.json`：原审核 capsule 路径到短 fixture 路径的审计映射。
- `vault/`：仅含 36 个审核 capsule；不包含任何 raw 原文，测试运行时会先提交为不可恢复 archive bundle。

CI 使用 lexical 检索运行 archive scope 的 V2 回归，验证标签路径、评估执行与确定性。实际 hybrid 指标仍以任务 artifacts 中的冻结报告为准。

# 变更记录

## 2026-08-14 — 退役清理工具完成

- `wiki_ingest` 删除 `superseded_jobs`，保留 `stale_pages` 与 `generation`；`wiki_status` 删除 `queue`。
- 移除 `SupersedeRegistry`、`repair codegraph-removal` 清理命令、`wiki/sources` 一次性归档脚本及孤立 JS；真实 vault 已完成迁移，无需再次运行清理工具。
- ADR-0012 记录删除前提、公共契约收缩和对 ADR-0011 清理条款/cleanup 任务 PRD 字段闸门的 supersede 关系。

## 2026-08-13 — 移除 CodeGraph 摄入

- 删除 `wiki_codegraph_import`、CodeGraph 摄入包及其 retrieval/write policy 消费点；core MCP 工具从 10 个变为 9 个，`wiki_status` 不再返回 CodeGraph 字段。
- 新增 `repair codegraph-removal plan|apply`，可清理带完整 CodeGraph 标记的 architecture/archive 页面和 `raw/sources/projects/*/codegraph/` 目录；`architecture/` 目录本身、手工页面和其他 raw 内容保留。
- 升级后未清理的旧 CodeGraph 页面按普通 generated 页面处理，查询不再施加 `project_code` 专属隔离；旧 raw 文件按既有 raw scope 索引边界处理。清理后如有显式 vector index，需按返回的 `rebuild_required` 提示重建。

## 2026-08-12 — 架构整改与检索/写入管线收敛

- 统一 durable Markdown 写入：`atomic_write_text`、CAS、`PageMutationCoordinator` 和 page-operation journal 现在共同负责页面事实提交、幂等重试和可恢复投影；页面已提交但派生投影失败时返回 `repair_pending`，不重复创建页面。
- `wiki_write_note`、`wiki_update` 与 chat source 统一接入页面变更协调器；普通页面变更改为 RetrievalIndexStore 单页增量投影，导航与 overview 分离维护；索引缺失或不兼容时明确返回 `rebuild_required`，全量 build/update 仅通过 CLI/admin 显式执行。
- Query V2 增加不可变 `QueryCorpusSnapshot`、统一 `assemble_recovery` 装配边界和协作式取消检查，避免 discovery、回退、向量、图扩展与 context pack 在一次查询内读取漂移状态。
- retrieval evaluation 增加 `EvaluationRuntimeSnapshot` 与 `EvaluationQueryService`，engine、MCP 和 gold 评测复用同一服务边界；MCP 评测不再修改全局配置注册表，也不创建索引或写入 telemetry。
- 统一 `wiki_paths.safe_segment`/`slug` 路径与文件名 owner，并保留人工笔记旧文件名的显式兼容参数；不自动迁移或改名既有文件。
- 补充检索索引状态/构建/更新 CLI 文档、故障恢复边界、评测基线声明和 agent 约定。确定性测试与静态检查通过；真实 vault 生产基线仍需冻结数据集和 manifest 后单独验证。

## 2026-08-10 — 本地工具优化发布候选

- core MCP 工具集合固定为 10 个；新增 metadata-only `wiki_list` 与 opaque-reference `wiki_get`。
- 页面 provenance 使用真实 raw hash、CAS 和可重建 dependency projection；历史迁移改为 CLI/admin plan/apply。
- 新增 privacy audit plan/apply，默认阻断未审批的文件名与 wikilink 变化，支持 fault/CAS rollback。
- 查询增加协作式取消、有界并发、容量状态和 finish-once telemetry；不提供线程强杀承诺。
- error/logging/database 三份 Trellis spec 改为可执行规则，并增加 anchor/registry spec lint。
- P2-1 批量与 P2-4 URL 摄入继续延期：不注册 MCP 工具、不增加队列或网络依赖。

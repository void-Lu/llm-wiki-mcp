# 变更记录

## 2026-08-20 — 架构深化 r5 G1 恢复通道

- `repair page-operation` 重放遇到导航/overview 的已知结构缺口时，显式升级为无 hint 的全量投影，修复 G6 fail-loud 后原有 repair 死循环；首次页面写入仍保持 fail-loud。
- operation journal 安全记录升级标记和原始稳定错误码，增量失败提示改指向真实的 `repair page-operation` admin 命令；不新增普通 MCP 写入中的隐式全量检索重建。
- Query V2 收拢执行视图、请求投影选项、seed 统计和召回切片，`QueryTelemetry` 在数据库被外部重置后自动重建 schema 并重试；公共响应保持逐位不变。
- 初始化投影集的三文件判定统一由 `wiki_paths.py` 持有，navigation-first 的裸 vault 引导顺序改为显式契约并由回归断言守护。
- G4 收敛写路径：`wiki_update` 与 `wiki_write_note` 共用 `page_mutation.explain_stage`，移除 note 响应的死字段 `indexed: null` 和 chat 检索死回退；导航/概览生成页统一使用 `wiki_io.render_page`，概览也只在 UTF-8 字节变化时写入，增量 API 仅保留 `created` 提示。

## 2026-08-19 — discovery 资格与摄入投影防御收敛

- discovery 快照页面资格统一复用 `snapshot_page_eligible`，并清扫零消费兼容转口与校准 loader memo；既有公共结果行为保持不变。
- 摄入 profile 缺失投影阶段返回稳定 `projection_stage_missing` 错误，不再暴露裸 `KeyError`。

## 2026-08-19 — note_writer 接口与脱敏 owner 收缩

- 删除 `save_obsidian_note` 的无效内部参数与 overwrite 分支；页面脱敏和 `redacted_count` 统一由 `prepare_wiki_page` 计算并由 writer 透传，`wiki_write_note` 公共 schema 与响应字段不变。

## 2026-08-19 — Query V2 死代码与只读状态边界清扫

- 删除质量门禁与日志 operation index 的无调用方私有包装、评测死转口，并让评测/server 从各自 owner 导入查询类型与常量；G1 已落地的公开 report seam 与 MCP lexical adapter 保持不变。
- 修正查询取消诊断的 ranking/fallback 阶段标签；`wiki_status` 在没有查询时不再创建 `QueryExecutionRegistry`，只返回未初始化的执行量摘要。

## 2026-08-19 — KnowledgeDependencies 页面策略接口收敛

- 依赖投影内部 `update_page` 改为接收冻结 `PagePolicy`，并由存储边界统一展开 SQL 列与保留策略校验；正式页面、隐私审计和 provenance migration 不再重复展开五个策略字段。MCP 公开接口和表结构不变。

## 2026-08-19 — wiki_update 准备校验顺序统一

- 合并 `wiki_update` preview/apply 的 incoming 准备流水线，统一执行 `REMOVED_FIELDS`、sources、参考段/wikilink 归一化与校验、lifecycle 校验；同一非法 sources + inactive 页面现在返回一致的来源错误优先级。

## 2026-08-19 — Query V2 规则质量门禁接线

- 接通 vault 级可选校准 `artifact_path`：相对路径按 vault root 解析，shadow/enforce 共用一次校准视图；artifact 缺失或 policy 版本不匹配时 fail-open 保留候选并记录有界诊断，评测 engine 与 MCP 入口共用门禁设置。

## 2026-08-18 — Query V2 规则质量门禁

- 新增 Query V2 page-level 规则质量门禁，支持 runtime snapshot 下的 `off`、`shadow`、`enforce` 三态；shadow 不改变公共结果，enforce 全拒绝时 fail-open 返回 baseline 并记录 `gate_would_suppress_all`。
- 新增按 score family/分桶校准、阈值 backoff、质量门禁评测指标、ranking/config identity 与 JSON/Markdown 安全投影；评测 identity 或样本证据不足时保持 `unproven`。
- 当前 holdout 不满足正式放行条件，生产默认保持 `off`/`shadow`；不新增 `insufficient_evidence` 公共结果码，不改变 no-results、discovery-only、index unavailable、取消/超时语义。

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

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库是一个本地 MCP server：把明确文件、人工笔记和受控更新写入外部 Obsidian Markdown Wiki，并由 Query V2 通过 passage FTS、可选向量和 wikilink 图查询返回可引用结果。安装、MCP 客户端配置和工具清单以 [README.md](README.md) 为准；这里保留开发时最需要的命令和跨文件架构约定。

查询侧正文预算政策的唯一 owner 是 `src/retrieval/body_budget.py`；`wiki_get` 的字节预算域归 `content_catalog`，不与查询词元预算共享常量。

## 常用命令

- 同步开发/测试依赖：`uv sync --extra dev`
- CLI 初始化 vault：`uv run llm-wiki-mcp init --vault <name> --root <path> --default`
- CLI 查看状态：`uv run llm-wiki-mcp status`
- CLI 检查/构建/更新检索库：`uv run llm-wiki-mcp index status|build|update --vault <path> [--scope active|raw|archive]`
- CLI 修复/审计（admin）：`uv run llm-wiki-mcp repair page-operation|provenance|privacy-audit <plan|apply> --vault <name>`
- 本机 `uv run pytest` 若报 `uv trampoline failed to canonicalize script path`，改用 `uv run python -m pytest`（已验证可用）。

完成前至少运行相关 `uv run pytest`；较大改动运行全量 `uv run pytest` 和 `uv run ruff check src/`。

## 架构总览

Python 3.11+，`src/` layout，运行依赖只有 `mcp` 和 `PyYAML`，dev 依赖是 `pytest`。入口点在 [pyproject.toml](pyproject.toml)：`llm-wiki-mcp` 调 [cli.py](src/app/cli.py)，`llm-wiki-mcp-server` 调 [server.py](src/app/server.py)。

### MCP 工具入口层

[server.py](src/app/server.py) 用 MCP Python SDK 2.x 的 `MCPServer` 注册所有公开工具，薄封装后委托到业务模块。人工笔记公开入口是 `wiki_write_note`。新增或调整 MCP 工具时，通常需要同时改：

1. 业务模块中的纯函数实现。
2. [server.py](src/app/server.py) 的 tool wrapper / `@mcp.tool()` 注册。
3. [README.md](README.md) 的工具说明（如果公开行为变化）。
4. [tests/app/test_server_tools.py](tests/app/test_server_tools.py) 和对应业务测试。

注册工具清单以 `server.py` 的 `@mcp.tool()` 注册表为准。init/config、retrieval-eval、vector/index 生命周期、archive admin、repair/privacy admin 和 migration 只保留在 CLI 边界。

[public_contracts.py](src/app/public_contracts.py) 定义稳定公开契约 `PublicResult`/`PublicError`（含 `correlation_id`）；[server.py](src/app/server.py) 所有工具经统一 `_register` 注册，入参 schema 为 `extra="forbid"` 严格模式。

### Vault 与路径模型

- `vault_root` 解析优先级在 [runtime_config.py](src/runtime/runtime_config.py)：工具参数 > `LLM_WIKI_VAULT_ROOT` > 全局 `config.yaml` 的 `default_vault`。
- 跨平台配置/数据目录在 [platform_paths.py](src/runtime/platform_paths.py)，测试通过 [tests/conftest.py](tests/conftest.py) 自动隔离这些环境变量。
- Wiki 目录创建、path segment 校验、slug 规则、路径错误翻译表与物理逃逸校验由 [wiki_paths.py](src/wiki/wiki_paths.py) 单一持有，核心入口为 `safe_segment`/`slug`/`translate_path_error`/`resolve_within_root`。外部 Obsidian root 的固定目录清单由 [wiki_paths.py](src/wiki/wiki_paths.py) 单一持有；`note_writer` 通过 `lowercase=False`、`fallback=""`、`ascii_punctuation=True` 的显式参数保持旧人工笔记文件名兼容；现有文件不自动迁移。
- `raw/sources/` 是来源事实层；`wiki_ingest` 只复制明确文件并同步检索投影。`wiki/` 是可读 Markdown 层；活动 Wiki 只接受具体 raw 文件的 `sources` 与 `source_hashes` 溯源。
- Markdown/frontmatter 读写、页面脱敏与 `redacted_count` 单一计算点在 [wiki_io.py](src/wiki/wiki_io.py) 的 `prepare_wiki_page`；生成页只能覆盖 `generated: true` 页面，默认人工页保护语义保持不变。`note_writer` 仅负责笔记路径、来源和正文结构，必要时只为默认文件名做标题预检，随后消费 prepared 结果，不再自行计算脱敏计数。

### 写入与维护流水线

- 单文件摄入在 [ingest_service.py](src/wiki/ingest_service.py)：`ingest_file` 只接受一个已存在文件，按字节复制到 `raw/sources/<source_type>/<project>/<source_name>/`，然后增量更新 RetrievalIndexStore。非 chat 且内容变化时只标记 KnowledgeDependencies 的 stale 页面；知识编译/generation 已停用（`generation: enabled: false`）。MCP 工具名为 `wiki_ingest`。
- 人工笔记写入在 [note_writer.py](src/wiki/note_writer.py)：note 类型为 `spec`/`plan`/`troubleshooting`/`researches`（项目级，写入 `wiki/projects/<project>/` 对应子目录）和 `knowledge`（写入 `wiki/concepts/<domain>/`，不接受 `project`）。MCP 入口为 `wiki_write_note` 工具，server 层接受 `note_type`/`noteType` 等双参数兼容。
- 页面事实提交统一在 [page_mutation.py](src/wiki/page_mutation.py) 的 `PageMutationCoordinator`：CAS 原子写入后按 durable operation journal 执行 dependencies、retrieval、navigation、overview 和 audit log 投影；任一派生阶段失败都保留已提交页面并返回 `repair_pending`，由同一个 operation 走 `repair_page_operation` 恢复。
- 正式知识页日志按职责分开：[wiki_log.py](src/wiki/wiki_log.py) 只负责条目渲染、脱敏、journal 去重和 operation index；[log_volume.py](src/wiki/log_volume.py) 独占 UTF-8 字节分卷、轮转、月度归档、`archives/log.md`、归档索引与逐文件原子写，`publish` 完成一次日志卷宗布局。
- [projection_profile.py](src/wiki/projection_profile.py) 是“字节变更后要追哪些投影”的纯 registry，按 formal/chat、ingest、archive、provenance/privacy admin 登记有序阶段；它不持有执行器、journal 或 repair 状态。页面 mutation 由 [page_mutation_adapters.py](src/wiki/page_mutation_adapters.py) 的 `FormalPageAdapter`/`ChatSourceAdapter` 引用 formal/chat profile，并各自持有路径、请求键和投影入口策略；其他消费方仍把阶段名映射到自己的直接执行方法。
- [page_policy.py](src/wiki/page_policy.py) 是 Wiki frontmatter 派生策略唯一 owner：`derive_page_policy`/`derive` 负责读侧 `freshness`/`maintenance`/`lifecycle`/`generated`/`replaced_by`，`stamp_page_policy`（`stamp` 短别名）负责写侧 `freshness`/`provenance_unverified`；stamp 只校验而不接管调用方的 `maintenance`/`replaced_by`。非法 `lifecycle` 统一归一为 `review_required`，调用方不重复实现。`KnowledgeDependencies.update_page(path, page_hash, sources, *, policy: PagePolicy)` 是依赖投影的内部边界，由存储层展开 SQL 列并保留 lifecycle、superseded/replaced_by 和 freshness 的 fail-closed 校验。
- 故障注入由 [atomic_file.py](src/wiki/atomic_file.py) 的 `fault_context` 统一持有；原子写入、页面投影和归档状态转换从当前 contextvar 读取 barrier，生产签名不再穿透 `fault`/`fault_at`，嵌套 context 退出后必须恢复外层值，执行 registry 进入线程前复制调用方 context。
- `wiki_write_note`、[wiki_update.py](src/wiki/wiki_update.py) 和 chat source 共用页面变更协调器。`wiki_update` 的 preview/apply 共用 incoming 准备纯函数，按 `REMOVED_FIELDS` → `sources` → reference section/ wikilink normalize/validate → active lifecycle 的顺序校验；因此非法 incoming 内容会在页面状态错误之前返回。`preview_update` 返回 hash、plan_id、locked fields、removed sources 和 diff；`apply_update` 继续分别负责 hash/plan、锁定字段、来源 verify 后提交页面事实，再更新依赖、增量检索 projection 和导航/log。MCP 工具为 `wiki_update`。
- [wiki_io.py](src/wiki/wiki_io.py) 的 `refresh_page_retrieval` 与 chat projection 只调用 RetrievalIndexStore 的单页 `update_page`；删除/改名使用对应增量操作。`refresh_navigation` 只维护导航页；[wiki_index.py](src/wiki/wiki_index.py) 的 `rebuild_retrieval_index`/`refresh_indexes` 只用于显式初始化、CLI/admin 或兼容测试，普通 MCP 写入不得隐式全量建库。索引缺失或不兼容时返回 `rebuild_required` 和 `rebuild_retrieval_index` repair action。
- 归档在 [archive_service.py](src/archive/archive_service.py)：`wiki_archive`/`wiki_restore` 只公开 `plan|apply`；purge、recover、rebuild-index 和 migration 只保留在 CLI/admin 边界。
- 隐私审计在 [privacy_audit.py](src/wiki/privacy_audit.py)：CLI `repair privacy-audit` 提供 `plan/apply`，默认阻断未审批的 filename/wikilink 变更（CAS + rollback）；脱敏/locator 策略在 [privacy_policy.py](src/common/privacy_policy.py)。
- 页面写入经 [atomic_file.py](src/wiki/atomic_file.py) 原子写（CAS）；CLI `repair page-operation` 由 [page_repair.py](src/wiki/page_repair.py)/[page_operation_store.py](src/wiki/page_operation_store.py) 支撑，`provenance`/`privacy-audit` 的 admin 维护 plan/apply/逐页 CAS/补偿审计由 [repair_plan.py](src/wiki/repair_plan.py) 统一 owner，领域差异仍在 [provenance_migration.py](src/wiki/provenance_migration.py) 与 [privacy_audit.py](src/wiki/privacy_audit.py) 的回调内；admin 页面路径使用 `wiki_paths.admin_wiki_page_file`，不接入 PageMutationCoordinator 或 archive plan journal。

### 查询与图谱能力

[query_pipeline.py](src/retrieval/query_pipeline.py) 是唯一查询引擎入口（Query V2）：passage FTS/vector 召回 -> RRF 融合 -> 有界强-seed 图扩展 -> 上下文预算裁剪 -> compact context pack，并将紧凑正文直接放入结果项；MCP 工具 `wiki_query` 只负责公共边界与运行时配置解析。

[query_quality_policy.py](src/retrieval/query_quality_policy.py) 是 page-level 质量门禁的纯策略 owner；门禁在完整 recovery、page dedup 后、public results/context projection 前运行，消费同一次 `QueryCorpusSnapshot`。`QualityGateSettings` 属于 runtime snapshot，默认 `off`，不进入 `wiki_query` 公共参数；`shadow` 只产生有界摘要，`enforce` 只投影 accepted 页面，并在非空 baseline 全拒绝时 fail-open 返回 baseline、记录 `gate_would_suppress_all`。门禁不接管 eligibility/filter、fallback/discovery 或 no-result/cancel/timeout 语义。

配置了 `QualityGateSettings.artifact_path` 时，`query_pipeline` 将 vault 相对路径解析到当前 vault、绝对路径按原值读取，并为 shadow/enforce 加载一次校准视图后传入 `evaluate_quality_gate`；artifact 缺失或版本不匹配继续 fail-open 保留候选并记录有界诊断。

质量门禁的评测指标、identity 和安全报告由 [retrieval_eval_report.py](src/retrieval/retrieval_eval_report.py) 统一持有；缺少兼容 identity 或最小 holdout 样本时结论使用 `unproven`，保持 `off`/`shadow`，不得默认放行生产 enforce。修改门禁字段、状态或介入位置时，同步更新 `query-quality-gate` spec 与 Query V2/评测回归。

[query_execution_context.py](src/retrieval/query_execution_context.py) 是单次 Query V2 执行状态、raw/fallback 分支迁移和冻结 `outcome()` 视图的唯一 owner。

[query_recall_policy.py](src/retrieval/query_recall_policy.py) 是 Query V2 召回与回退启发式的唯一 owner：意图分类、查询扩展、relaxed/raw 候选、coverage 合并、自适应扩展和步骤计数均由此模块提供；execution context 只负责状态编排，不复制这些策略。

candidate 条目形状的唯一 owner 是 [candidate_items.py](src/retrieval/candidate_items.py) 模块。

[graph_retrieval.py](src/retrieval/graph_retrieval.py) 提供 Query V2 共用的 wikilink、shared source、common neighbor、same type 有界图扩展；[vector_index.py](src/retrieval/vector_index.py) 提供 `VectorRecord`、`vector_index_records` 及显式向量生命周期所需的索引记录回退。v1 的 `src/wiki/wiki_query.py` 私有查询引擎已删除，不要重新引入第二套检索入口。

Query V2 支持 `expansion_terms` 模糊词扩展和 `filters` 元数据过滤（含 `path_prefix`，见 [metadata_filters.py](src/retrieval/metadata_filters.py)）；查询经 [query_cancellation.py](src/retrieval/query_cancellation.py) 协作式取消与有界并发。`note_type`/`noteType` 合并保持 canonical 优先、falsy 回退和双空报错，不能抽象为 aliases；过滤器的 MCP 边界与目录层防御性规范化契约见 `metadata_filters.py` 模块文档。

[query_snapshot.py](src/retrieval/query_snapshot.py) 的 `QueryCorpusSnapshot` 为每次查询捕获 active/raw store 的不可变页面 metadata、provenance 和 candidate 视图；discovery、过滤、向量、图扩展和回退必须复用该快照，不能在同一请求内重复读取漂移的 store 状态。

[query_shared.py](src/retrieval/query_shared.py) 是查询候选资格策略的 owner：`eligible`/`matches_request` 继续服务 PassageHit，`snapshot_page_eligible` 统一把快照 page record 构造成 probe 并执行两层资格判定；标题候选、向量 allowlist 和图扩展必须复用该入口，不在调用点重拼过滤规则。

[query_recovery.py](src/retrieval/query_recovery.py) 是回退决策、阶梯、每页选择、打分组合与 envelope 装配的唯一 owner；其中 `assemble_recovery` 统一拥有命中统计、按页候选池和 context pack。新增回退分支应扩展 `FallbackPlan`/`RecoveryCondition`/该装配边界，不要在 query pipeline、MCP wrapper 或 telemetry 中复制一套状态逻辑。

[retrieval_eval_dataset.py](src/retrieval/retrieval_eval_dataset.py) 是评测 JSONL/manifest schema、相关性标签和 public/legacy 过滤器投影的唯一 owner；解析函数不依赖 vault 或查询运行时。 [retrieval_eval_report.py](src/retrieval/retrieval_eval_report.py) 是 Recall/Precision/MRR/nDCG、slice/gate、pipeline 安全投影与 JSON/Markdown 报告输出的 owner。 [retrieval_eval.py](src/retrieval/retrieval_eval.py) 只保留 `EvaluationRuntimeSnapshot`、`EngineQueryAdapter`/`McpQueryAdapter`、`EvaluationQueryService` 和评测编排 seam。评测经 `query_telemetry` 只读接口取遥测、经注入 adapter 走 MCP 入口；`app.server` 只允许在 `default_mcp_entry_adapter()` 内惰性导入，评测模块顶层不得加载 server。MCP 入口只在请求局部 ContextVar 中注入不可变 tool resolution，不得修改 `server.CONFIG_REGISTRY`；`parse_evaluation_filters` 由 dataset owner 提供并由 eval 兼容导出。评测必须保持只读，不创建/更新检索库，也不写入 telemetry。

[lexical_analyzer.py](src/retrieval/lexical_analyzer.py) 提供 FTS 和检索共用的词法归一化（Latin/CJK 分词、停用词、编辑距离）。

[wikilinks.py](src/wiki/wikilinks.py) 提供 wikilink 格式化和解析工具函数。query、update 等模块统一使用此模块处理 wikilink，不内嵌正则。

[wiki_files.py](src/wiki/wiki_files.py) 只提供 `wiki_status`：vault 结构、检索/vector index、版本与运行身份。MCP 工具为 `wiki_status`；status 只读取已存在的 `QueryExecutionRegistry`，首次查询前返回 `active/pending=0`，不物化执行 registry。

[content_catalog.py](src/wiki/content_catalog.py)（含 [catalog_cursor.py](src/wiki/catalog_cursor.py)、[content_reference.py](src/wiki/content_reference.py)）是 `wiki_list`/`wiki_get` 的只读 catalog 后端：metadata 分页 + opaque `content_ref`，不读正文。

[chat_memory.py](src/wiki/chat_memory.py) 提供不可变、脱敏的 chat source 持久化；`wiki_write_note` 通过 `chat_metadata`/`chat_derived`/`chat_sources` 参数写入 chat source。

[wiki_limits.py](src/wiki/wiki_limits.py) 定义页面/日志/导航条目字节上限（HARD_PAGE_BYTES=200_000 等）。

[runtime_provenance.py](src/runtime/runtime_provenance.py) 提供服务器版本与运行身份快照，用于 `wiki_status` 和 MCP 握手。

[git_utils.py](src/common/git_utils.py) 提供零外部依赖的 git 辅助函数，用于 runtime provenance。

旧的 `context_budget.py`、`page_merge.py`、`wiki_dedup.py`、`wiki_delete.py`、`wiki_enrich.py`、`wiki_gap.py`、`wiki_ingest.py`、`wiki_insights.py`、`wiki_lint.py`、`wiki_repair.py`、`wiki_research.py`、`wiki_source_index.py`、`wiki_synthesis.py`、`wiki_verify.py`、`wiki_batch.py`、`pipeline_detector.py`、`louvain.py` 已删除；不要重新注册这些模块或 MCP 工具。

## 必守约定

- CodeGraph 摄入已移除；真实 vault 中的旧 CodeGraph 页面/raw 目录已由用户完成迁移，退役清理命令已删除；未来项目级代码分析仍通过普通 architecture 笔记维护。
- 旧 RAG 工具和旧人工笔记入口 `save_obsidian_note` 不应注册；人工笔记公开入口统一为 `wiki_write_note`。
- `knowledge` 写入 `wiki/concepts/<domain>/`，禁止 `project`。
- 项目目录固定为 `wiki/projects/<project>/{index.md,specs/,plans/,architecture/,troubleshooting/,researches/}`；`wiki_write_note` 只写 spec/plan/troubleshooting/researches，架构页由 `wiki_update` 维护。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。
- 不要在代码、测试或文档中硬编码个人 Vault 路径、API key、token、邮箱、手机号等敏感信息；脱敏逻辑在 [redaction.py](src/common/redaction.py)。
- 所有 MCP 工具写入文件统一使用 `encoding="utf-8"`（无 BOM）；读取 Obsidian 文件时可用 `utf-8-sig` 以兼容 BOM，但写入绝不产生 BOM。禁止使用 PowerShell `Set-Content` 默认编码（UTF-16 LE）修改项目文件。
- durable Markdown 写入必须经过 [atomic_file.py](src/wiki/atomic_file.py) 和 `PageMutationCoordinator`；投影失败时修复已有 operation，不通过重试创建页面来恢复。
- fault 测试必须在顶层入口使用 `fault_context` 注入，不能恢复生产参数穿透或创建全局可变 fault/profile registry；未注入请求必须保持 no-op，归档 staged/pending/detaching/committed/rollback 阶段词汇和稳定错误码不变。
- 普通页面变更只做 RetrievalIndexStore 增量投影；全量 `index build|update` 必须是显式 CLI/admin 操作，禁止藏在 MCP 查询或写入调用中。
- Query V2 的所有消费者复用同一次 `QueryCorpusSnapshot`，回退和 context pack 统一走 `assemble_recovery`；不得在不同阶段重新扫描或复制 fallback 状态。
- retrieval evaluation 不得 monkeypatch 或修改全局 runtime registry；使用 `EvaluationRuntimeSnapshot`/`EvaluationQueryService` 和公开 parser。

## 测试定位

测试按功能包分组于 `tests/`，文件名与被测模块对应；单测试命令为 `uv run pytest tests/<package>/test_<module>.py`。

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues in `void-Lu/netsuite-llm-wiki-mcp`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

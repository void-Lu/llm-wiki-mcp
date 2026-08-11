# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库是一个本地 MCP server：把明确文件、人工笔记和受控更新写入外部 Obsidian Markdown Wiki，并用关键词 + wikilink 图查询返回可引用 context pack。安装、MCP 客户端配置和工具清单以 [README.md](README.md) 为准；这里保留开发时最需要的命令和跨文件架构约定。

## 常用命令

- 安装/同步开发环境：`uv sync --extra dev`
- 运行全部测试：`uv run pytest`
- 运行单个测试文件：`uv run pytest tests/wiki/test_wiki_query.py`
- 运行单个测试函数：`uv run pytest tests/wiki/test_wiki_query.py::test_function_name -v`
- 启动 MCP server：`uv run llm-wiki-mcp-server`、`uv run llm-wiki-mcp server` 或 `uv run python -m app.server`
- CLI 初始化 vault：`uv run llm-wiki-mcp init --vault <name> --root <path> --default`
- CLI 查看状态：`uv run llm-wiki-mcp status`
- CLI 修复/审计（admin）：`uv run llm-wiki-mcp repair page-operation|provenance|privacy-audit <plan|apply> --vault <name>`
- 本机 `uv run pytest` 若报 `uv trampoline failed to canonicalize script path`，改用 `uv run python -m pytest`（已验证可用）。

项目使用 `uv.lock` 管理开发环境。Ruff 配置在 pyproject.toml（`select = ["E9", "F"]`），运行 `uv run ruff check src/`；完成前至少运行相关 `uv run pytest`，较大改动运行全量 `uv run pytest` 和 `uv run ruff check src/`。

## 架构总览

Python 3.11+，`src/` layout，运行依赖只有 `mcp` 和 `PyYAML`，dev 依赖是 `pytest`。入口点在 [pyproject.toml](pyproject.toml)：`llm-wiki-mcp` 调 [cli.py](src/app/cli.py)，`llm-wiki-mcp-server` 调 [server.py](src/app/server.py)。

### MCP 工具入口层

[server.py](src/app/server.py) 用 MCP Python SDK 2.x 的 `MCPServer` 注册所有公开工具，薄封装后委托到业务模块。人工笔记公开入口是 `wiki_write_note`；旧 `save_obsidian_note` 不应再注册。新增或调整 MCP 工具时，通常需要同时改：

1. 业务模块中的纯函数实现。
2. [server.py](src/app/server.py) 的 tool wrapper / `@mcp.tool()` 注册。
3. [README.md](README.md) 的工具说明（如果公开行为变化）。
4. [tests/app/test_server_tools.py](tests/app/test_server_tools.py) 和对应业务测试。

注册工具清单（10 个）：`wiki_status`、`wiki_list`（metadata-only catalog）、`wiki_get`（opaque `content_ref` 精确读取）、`wiki_ingest`、`wiki_codegraph_import`、`wiki_write_note`、`wiki_update`、`wiki_query`、`wiki_archive`、`wiki_restore`。`wiki_generation` worker 工具不再注册；init/config、retrieval-eval、vector/index 生命周期、archive admin、repair/privacy admin 和 migration 只保留在 CLI 边界。

[public_contracts.py](src/app/public_contracts.py) 定义稳定公开契约 `PublicResult`/`PublicError`（含 `correlation_id`）；[server.py](src/app/server.py) 所有工具经统一 `_register` 注册，入参 schema 为 `extra="forbid"` 严格模式。

### Vault 与路径模型

- `vault_root` 解析优先级在 [runtime_config.py](src/runtime/runtime_config.py)：工具参数 > `LLM_WIKI_VAULT_ROOT` > 全局 `config.yaml` 的 `default_vault`。
- 跨平台配置/数据目录在 [platform_paths.py](src/runtime/platform_paths.py)，测试通过 [tests/conftest.py](tests/conftest.py) 自动隔离这些环境变量。
- Wiki 目录创建和 path segment 校验在 [wiki_paths.py](src/wiki/wiki_paths.py)。外部 Obsidian root 固定包含 `purpose.md`、`schema.md`、`raw/sources/{projects,file,references,chat}/`、`raw/assets/`、`wiki/index.md`、`wiki/log.md`、`wiki/overview.md`、`wiki/projects/`、`wiki/concepts/`、`wiki/entities/`、`archives/log.md`、`archives/bundles/`、`.obsidian/`、`.llm-wiki/state.sqlite3`；`wiki/archives/` 是已退役的历史路径，不由新运行时创建。
- `raw/sources/` 是来源事实层；`wiki_ingest` 只复制明确文件并同步检索投影。`wiki/` 是可读 Markdown 层；`wiki/sources/` 命名空间已退役（一次性归档由 `scripts/archive_wiki_sources.py` 完成），活动 Wiki 只接受具体 raw 文件的 `sources` 与 `source_hashes` 溯源。
- Durable generation 状态在 `.llm-wiki/state.sqlite3`；`wiki_files.wiki_status` 兼容读取旧 `.llm-wiki/ingest-queue.json` 队列摘要。
- Markdown/frontmatter 读写、覆盖保护和脱敏在 [wiki_io.py](src/wiki/wiki_io.py)。生成页只能覆盖 `generated: true` 页面；人工页不能被静默覆盖。

### 写入与维护流水线

- 单文件摄入在 [ingest_service.py](src/wiki/ingest_service.py)：`ingest_file` 只接受一个已存在文件，按字节复制到 `raw/sources/<source_type>/<project>/<source_name>/`，然后增量更新 RetrievalIndexStore。非 chat 且内容变化时，[knowledge_compiler.py](src/wiki/knowledge_compiler.py) 只做 raw-only provenance 失效（supersede 相关 generation job）；知识编译/generation 已停用（`generation: enabled: false`）。MCP 工具名为 `wiki_ingest`。
- [generation_queue.py](src/wiki/generation_queue.py) 仍是 durable job 存储（`create/claim/apply/fail/release/status`），但没有 worker 工具或 CLI 管理入口——仅用于 supersede 已停用 job 类型。
- 人工笔记写入在 [note_writer.py](src/wiki/note_writer.py)：note 类型为 `spec`/`plan`/`troubleshooting`/`researches`（项目级，写入 `wiki/projects/<project>/` 对应子目录）和 `knowledge`（写入 `wiki/concepts/<domain>/`，不接受 `project`）。MCP 入口为 `wiki_write_note` 工具，server 层接受 `note_type`/`noteType` 等双参数兼容。
- 写入后维护集中在 [wiki_index.py](src/wiki/wiki_index.py)、[wiki_overview.py](src/wiki/wiki_overview.py)、[wiki_log.py](src/wiki/wiki_log.py)。会产生或变更页面的工具通常要刷新 index/overview 并 append log。
- 受控更新在 [wiki_update.py](src/wiki/wiki_update.py)：`preview_update` 返回 hash、plan_id、locked fields、removed sources 和 diff；`apply_update` 在校验 hash/plan、锁定字段、来源与 active lifecycle 后写页面，并更新 knowledge dependencies、index 和 log。MCP 工具为 `wiki_update`。
- 归档在 [archive_service.py](src/archive/archive_service.py)：`wiki_archive`/`wiki_restore` 只公开 `plan|apply`；purge、recover、rebuild-index 和 migration 只保留在 CLI/admin 边界。
- 隐私审计在 [privacy_audit.py](src/wiki/privacy_audit.py)：CLI `repair privacy-audit` 提供 `plan/apply`，默认阻断未审批的 filename/wikilink 变更（CAS + rollback）；脱敏/locator 策略在 [privacy_policy.py](src/common/privacy_policy.py)。
- 页面写入经 [atomic_file.py](src/wiki/atomic_file.py) 原子写（CAS）；CLI `repair` 边界（`page-operation`/`provenance`/`privacy-audit` 的 plan/apply）由 [page_repair.py](src/wiki/page_repair.py)、[page_operation_store.py](src/wiki/page_operation_store.py)、[provenance_migration.py](src/wiki/provenance_migration.py) 支撑。

### 查询与图谱能力

[wiki_query.py](src/wiki/wiki_query.py) 主路径是关键词/CJK bigram 命中 → wikilink、shared source、common neighbor、same type 图扩展 → token budget 裁剪 → numbered citation context pack。可选的本地混合向量检索（`enable_vector=true` + `vector` extra + 显式 `vector build`）在词法和向量独立召回后使用 RRF 融合（缩放后叠加到 `fusion_score`），再加图扩展；默认关闭，不引入 Chroma、`.rag-index` 或外部 embedding 主路径。

[wikilinks.py](src/wiki/wikilinks.py) 提供 wikilink 格式化和解析工具函数：`format_wikilink`（含表格内 `\| 转义）、`normalize_wikilink_targets`（小写化 + 表格别名处理）、`wikilink_targets`、`split_wikilink_inner`、`table_wikilink_alias_pipe_lines` 等。query、update 等模块统一使用此模块处理 wikilink，不内嵌正则。

[wiki_files.py](src/wiki/wiki_files.py) 只提供 `wiki_status`：vault 结构、检索/vector index、generation queue、版本与运行身份，以及 CodeGraph executable 的只读可用性。MCP 工具为 `wiki_status`。

[content_catalog.py](src/wiki/content_catalog.py)（含 [catalog_cursor.py](src/wiki/catalog_cursor.py)、[content_reference.py](src/wiki/content_reference.py)）是 `wiki_list`/`wiki_get` 的只读 catalog 后端：metadata 分页 + opaque `content_ref`，不读正文。

[wiki_models.py](src/wiki/wiki_models.py) 定义核心数据结构 `WikiPage`、`WikiLogEntry`。

[query_pipeline.py](src/retrieval/query_pipeline.py) 是查询引擎核心（V2）：passage FTS/vector 召回 -> RRF 融合 -> 有界强-seed 图扩展 -> 上下文预算裁剪 -> compact context pack。支持 `expansion_terms` 模糊词扩展和 `filters` 元数据过滤（含 `path_prefix`，见 [metadata_filters.py](src/retrieval/metadata_filters.py)）；查询经 [query_cancellation.py](src/retrieval/query_cancellation.py) 协作式取消与有界并发。

[chat_memory.py](src/wiki/chat_memory.py) 提供不可变、脱敏的 chat source 持久化；`wiki_write_note` 通过 `chat_metadata`/`chat_derived`/`chat_sources` 参数写入 chat source。

[lexical_analyzer.py](src/retrieval/lexical_analyzer.py) 提供 FTS 和检索共用的词法归一化（Latin/CJK 分词、停用词、编辑距离）。

[runtime_provenance.py](src/runtime/runtime_provenance.py) 提供服务器版本与运行身份快照，用于 `wiki_status` 和 MCP 握手。

[wiki_limits.py](src/wiki/wiki_limits.py) 定义页面/日志/导航条目字节上限（HARD_PAGE_BYTES=200_000 等）。

[git_utils.py](src/common/git_utils.py) 提供 `get_git_commit`、`get_git_revision`、`get_git_dirty`、`is_git_dirty`、`get_git_branch` 等零外部依赖 git 辅助函数，用于 runtime provenance。

归档相关模块：`archive_models.py` 定义 bundle/plan/tombstone 数据，`archive_manifest.py` 负责 manifest 哈希与校验，`archive_planner.py` 生成归档计划，`archive_migration.py` 处理 legacy migration，`archive_service.py` 是 MCP/CLI 的公开服务边界。

### CodeGraph 同步

[codegraph_sync.py](src/codegraph/codegraph_sync.py) 是 CodeGraph 快照导入器：读取工作目录 `.codegraph/codegraph.db`，将结构化代码事实投影为 `wiki/projects/<project>/architecture/` 下的 code-facts、pipelines 和 code-overview 页面，并同步 active retrieval projection。MCP 工具为 `wiki_codegraph_import`（固定 `sync="sync"`）。[codegraph_policy.py](src/codegraph/codegraph_policy.py) 提供所有权检查（`managed_by=codegraph`、`retrieval_scope=project_code`、`source_name=codegraph`），CodeGraph 管理页只能由 `wiki_codegraph_import` 更新，`wiki_update` 不覆盖。

旧的 `context_budget.py`、`page_merge.py`、`wiki_dedup.py`、`wiki_delete.py`、`wiki_enrich.py`、`wiki_gap.py`、`wiki_ingest.py`、`wiki_insights.py`、`wiki_lint.py`、`wiki_repair.py`、`wiki_research.py`、`wiki_source_index.py`、`wiki_synthesis.py`、`wiki_verify.py`、`wiki_batch.py`、`pipeline_detector.py`、`louvain.py` 已删除；不要重新注册这些模块或 MCP 工具。旧 `codegraph_client.py` 已被 [codegraph_sync.py](src/codegraph/codegraph_sync.py) + [codegraph_policy.py](src/codegraph/codegraph_policy.py) 替代。

## 必守约定

- 不要重新引入已删除的 CodeGraph 摄入模块或旧 RAG 主路径；CodeGraph 只在 `wiki_status` 中作为只读可用性字段。
- 旧 RAG 工具和旧人工笔记入口 `save_obsidian_note` 不应注册；人工笔记公开入口统一为 `wiki_write_note`。
- `knowledge` 写入 `wiki/concepts/<domain>/`，禁止 `project`。
- 项目目录固定为 `wiki/projects/<project>/{index.md,specs/,plans/,architecture/,troubleshooting/,researches/}`；`wiki_write_note` 只写 spec/plan/troubleshooting/researches，架构页由 `wiki_update` 维护。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。
- 不要在代码、测试或文档中硬编码个人 Vault 路径、API key、token、邮箱、手机号等敏感信息；脱敏逻辑在 [redaction.py](src/common/redaction.py)。
- 所有 MCP 工具写入文件统一使用 `encoding="utf-8"`（无 BOM）；读取 Obsidian 文件时可用 `utf-8-sig` 以兼容 BOM，但写入绝不产生 BOM。禁止使用 PowerShell `Set-Content` 默认编码（UTF-16 LE）修改项目文件。

## 测试定位

测试文件按功能包分组，命令为 `uv run pytest tests/<package>/test_<module>.py`：
- CLI/runtime/config/provenance：`test_cli.py`、`test_runtime_config.py`、`test_runtime_provenance.py`、`test_readme_global_mcp_docs.py`
- Wiki 基础设施：`test_wiki_paths.py`、`test_wiki_io.py`、`test_wiki_index.py`、`test_wiki_overview.py`、`test_wiki_log.py`、`test_wiki_files.py`
- MCP 工具注册与业务入口：`test_server_tools.py`、`test_wiki_update.py`、`test_save_obsidian_note.py`、`test_ingest_service.py`、`test_codegraph_sync.py`
- 查询/检索/向量/wikilink：`test_wiki_query.py`、`test_query_pipeline.py`、`test_retrieval_eval.py`、`test_retrieval_index.py`、`test_vector_index.py`、`test_vector_passage_v2.py`、`test_vector_provider.py`、`test_wiki_ingest_normalize.py`、`test_wikilinks.py`
- worker/归档/辅助：`test_knowledge_compiler.py`、`test_generation_queue.py`、`test_archive_lifecycle.py`、`test_archive_wiki_sources.py`、`test_git_utils.py`
- 通用支撑：`test_concept_registry.py`、`test_knowledge_dependencies.py`、`test_context_packer.py`、`test_passage_chunker.py`、`test_content_redaction.py`、`test_fallback_policy.py`、`test_query_telemetry.py`、`test_lexical_analyzer.py`、`test_chat_memory.py`、`test_build_backend.py`
- repair/privacy/契约/catalog：`test_cli_repair.py`、`test_cli_repair_admin.py`、`test_page_repair.py`、`test_privacy_audit.py`、`test_provenance_migration.py`、`test_public_contracts.py`、`test_content_catalog.py`、`test_query_cancellation.py`、`test_query_executor.py`、`test_spec_lint.py`（tests/tools/）

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues in `void-Lu/netsuite-llm-wiki-mcp`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

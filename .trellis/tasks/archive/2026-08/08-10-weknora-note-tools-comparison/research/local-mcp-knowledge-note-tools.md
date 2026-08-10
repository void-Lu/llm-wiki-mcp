# Research: 本地 llm-wiki-mcp 知识库笔记工具与调用链

- Query: 盘点当前工作树中与知识库笔记创建、摄入、查询、受控更新、链接/来源维护、归档/恢复、状态/索引维护直接相关的公开 MCP 工具、参数与返回契约、入口到存储/索引调用链、数据模型、错误语义、安全边界、可观测性、测试和文档；明确 core profile 全量工具，并区分 MCP、CLI/admin、内部与禁用/退役能力。
- Scope: internal
- Date: 2026-08-10

## Findings

### 1. 取证基线与方法

- 仓库：`llm-wiki-mcp` 当前工作区。
- 本地分支：`v0.9.21`。
- HEAD：`19bac922c31932efc1edc5e9d99d186394a36158`。
- 取证开始时工作树：`v0.9.21...origin/v0.9.21`，仅见未跟踪目录 `?? .netsuite-mcp/`；该目录与本任务无关，未读取、未修改、未纳入结论。
- 研究文件写入后的最小校验状态：`M src/app/server.py`、`M tests/app/test_server_tools.py`、`?? .netsuite-mcp/`。前两项是在本代理完成源码取证后由并行协作者产生，不是本代理改动；本代理未读取或回退其新内容。因此本文精确描述的是上述 HEAD 上、这两项并行修改出现前的已取证工作树快照。
- 先使用仓库 `.codegraph/` 的 `codegraph explore` 定位 MCP 注册、工具调用链、检索索引、归档与退役队列符号；首次调用因 Windows 沙箱在 `C:\Users\26327` 上 `lstat` 返回 `EPERM`，随后以获批的沙箱外只读方式成功查询。之后只对 CodeGraph/Semble 已定位文件做定点源码读取。
- 本次没有运行测试：Trellis research 角色禁止在指定研究文件外写入，而 pytest 可能生成缓存；测试结论来自当前测试源码，不代表本轮实际执行结果。

### 2. MCP 公共面：core profile 精确为 8 个工具

测试以集合相等而非“至少包含”锁定公共面：`CORE_TOOLS` 精确包含以下 8 个名称，`Client.list_tools()` 必须与它相等（`tests/app/test_server_tools.py:32`，符号 `CORE_TOOLS`；`tests/app/test_server_tools.py:52`，符号 `_registered_tool_names`；`tests/app/test_server_tools.py:80`，符号 `test_registered_tools_match_core_public_surface`）。README 也声明默认 core 只注册 8 个业务工具（`README.md:151`，章节“工具”）。

| 工具 | MCP 入口与参数契约 | 主要返回/错误 | 入口 → 服务 → 存储/索引 |
| --- | --- | --- | --- |
| `wiki_status` | `detail="summary"`，可选 `vault`、兼容参数 `vault_root`/`vaultRoot`；`detail` 仅允许 `summary/indexes/generation/archive`（`src/app/server.py:159`，符号 `wiki_status`）。 | 成功聚合结构、queue、CodeGraph 可用性、vector、active/archive/raw retrieval、版本/runtime、脱敏配置、archive index/operations；非法 detail 返回 `ok=false, code=invalid_status_detail`。兼容 root 成功时附 `deprecated_vault_root` warning（`src/app/server.py:161`、`src/app/server.py:167`）。 | `wiki_status` → `wiki_files.wiki_status` → `RetrievalIndexStore.status()` 三个 scope、`VectorIndexStore.status()`；wrapper 再调用 `ArchiveService.status()` 并按 detail 投影（`src/wiki/wiki_files.py:14`，符号 `wiki_status`；`src/app/server.py:167`、`:174`）。 |
| `wiki_ingest` | 必填 `source_path`、`source_name`；`project=""`、`source_type="file"`；可选 vault selectors 与 `metadata`（`src/app/server.py:327`，符号 `wiki_ingest`）。 | 文本返回 `operation/source/content_hash/storage_kind=text_source/semantic_indexed/index_scope/index`，变更时还可返回 `generation/stale_pages/superseded_jobs`；非文本返回 `storage_kind=asset`、`semantic_indexed=false`、`asset_not_indexed`。目录或不存在文件为 `source_not_file`（`src/wiki/ingest_service.py:46`，符号 `ingest_file`）。 | wrapper → `ingest_file` → 按 UTF-8 文本判定复制到 `raw/sources/<type>/<project-or-default>/<name>/<basename>`，否则复制到 `raw/assets/...`；文本经 `page_from_file` 写入 active（chat）或 raw SQLite FTS，变更时经 `KnowledgeDependencies.source_changed` 和 `GenerationQueue.supersede_sources` 传播 stale（`src/wiki/ingest_service.py:65`、`:66`、`:88`、`:93`、`:94`、`:110`）。 |
| `wiki_codegraph_import` | `sync: Literal["sync"]="sync"`；可选 vault selectors 与 `workspace_root`；仅支持 `sync`（`src/app/server.py:339`，符号 `wiki_codegraph_import`）。 | 非 `sync` 为 `invalid_codegraph_operation`；领域错误保留 `CodeGraphSyncError.code`，其他异常归一为 `codegraph_sync_failed`（`src/app/server.py:346`、`:352`）。 | wrapper → `run_codegraph_sync`；CodeGraph 产物以 `managed_by=codegraph`、`retrieval_scope=project_code` 等完整所有权标记保护，普通 `wiki_update` 不可修改（`src/codegraph/codegraph_policy.py:28`，符号 `codegraph_architecture_path`；`:38`，`is_codegraph_frontmatter`；`src/wiki/wiki_update.py:50`、`:86`）。测试覆盖缺 DB/不兼容 schema 的写前拒绝、成功 raw/page/index 同步、目录替换回滚与人工目标保护（`tests/codegraph/test_codegraph_sync.py:120`、`:134`、`:153`、`:273`、`:362`）。 |
| `wiki_write_note` | 必填 `title/content`，`note_type` 或兼容 `noteType` 二选一；可选 vault、`project/domain/tags/filename/chat_metadata/chat_derived/chat_sources/related_pages/related_pages_heading/sources`（`src/app/server.py:307`，符号 `wiki_write_note`）。wrapper 固定 `overwrite=False, auto_index=True`（`:322`）。 | 缺类型为 `missing_note_type`；成功通常返回 `ok/path/absolute_path/created/redacted_count/indexed/wikilink_target/normalized_wikilinks/broken_wikilinks`，并按输入附 `related_pages_skipped/sources_skipped/warnings`（`src/wiki/note_writer.py:268`，符号 `save_obsidian_note`）。已存在目标为 `file_exists`。 | wrapper → `save_obsidian_note` → 路径/类型校验 → 正文脱敏、wikilink 规范化、参考段与 raw source 校验 → Markdown/frontmatter 写入 → `refresh_indexes`、`refresh_overview`、`append_log_entry`（`src/wiki/note_writer.py:141`、`:230`、`:236`、`:239`、`:242`、`:259`）。`note_type=chat` 是特例，转入 `ChatMemoryService.save`，不是正式 Wiki 页面（`:174`）。 |
| `wiki_update` | 必填 `page_path/incoming_body`；`action="preview"`；可选 vault、`incoming_frontmatter/plan_id/expected_hash/related_pages/related_pages_heading`（`src/app/server.py:362`，符号 `wiki_update`）。action 仅 `preview/apply`。 | preview 返回 `current_hash/plan_id/locked_fields/violations/removed_sources/diff/wikilink` 检查；apply 返回 `hash/navigation/retrieval_index/wikilink` 检查。稳定错误含 `page_not_found/path_escape/update_path_not_allowed/codegraph_managed_page/inactive_page/source_capsules_removed/expected_hash_mismatch/plan_stale/locked_field/sources_required`（`src/wiki/wiki_update.py:34`，`preview_update`；`:67`，`apply_update`）。 | wrapper → `preview_update` 或 `apply_update` → `write_wiki_page` → retrieval projection → `KnowledgeDependencies.update_page` → `refresh_indexes` → `append_log_entry`（`src/wiki/wiki_update.py:115`、`:123`、`:126`、`:127`、`:128`）。 |
| `wiki_query` | 必填 `question`；`scope=auto`，可选 `project/filters/top_k/expansion_terms/vault selectors/confirmation_token`（`src/app/server.py:271`，符号 `wiki_query`）。scope 枚举为 `auto/knowledge/history/all/archive/raw`；top_k 为 1–40。 | 参数错误为 `invalid_scope/missing_question/invalid_top_k/invalid_expansion_terms/invalid_filters/project_required_for_codegraph`；超时为 `query_timeout`。空成功结果由 wrapper 补 `code=no_results`，但发现/批处理状态不会被覆盖（`src/app/server.py:61`，`attach_no_results_outcome`；`:291`、`:293`、`:295`、`:297`、`:300`）。索引不可用是 `ok=true` 加 `code`、空结果和 pipeline warning，而非 transport error（`src/retrieval/query_pipeline.py:2052`，符号 `run_query_v2`，尤其 `:2097`–`:2113`）。 | wrapper 读取已解析 vault 的 typed retrieval/context/telemetry 快照 → `run_query_v2` → 选择 `.llm-wiki/retrieval.sqlite3`、`archive-index.sqlite3` 或 `raw-retrieval.sqlite3` → FTS、可选 vector、RRF、图扩展、预算打包、entity discovery/batch（`src/app/server.py:238`、`:252`–`:267`；`src/retrieval/query_pipeline.py:2052`）。Query 不构建索引。 |
| `wiki_archive` | 必填单个 `target`；`reason="manual"`、`cascade=false`、`action="plan"`、可选 `plan_id` 与 vault selectors（`src/app/server.py:378`，符号 `wiki_archive`）。reason 精确为 `superseded/deprecated/retention/migration/manual`（`src/archive/archive_models.py:9`，符号 `ARCHIVE_REASONS`）。 | plan 返回序列化 `ArchivePlan`；apply 必须使用未过期、未使用 plan。稳定错误含 `invalid_action/invalid_archive_reason/archive_plan_required/archive_plan_used/archive_plan_expired/archive_plan_drift` 及 planner blockers（`src/archive/archive_service.py:101`、`:108`、`:128`）。成功 apply 返回 `operation_id/archive_id/state=committed/archive_index`（`:253`）。 | wrapper → `ArchiveService(actor="mcp").plan_archive/apply` → SQLite plan/journal → staging/pending/recovery → hash 重验 → active 文件 detach 与 active index delete → immutable bundle rename → archive index rebuild（`src/app/server.py:389`；`src/archive/archive_service.py:42`、`:177`–`:253`）。 |
| `wiki_restore` | 必填 `archive_id`；`action="plan"`、可选 `plan_id` 与 vault selectors（`src/app/server.py:397`，符号 `wiki_restore`）。 | plan 返回 restore plan；apply 复用同一 plan gate。稳定错误含 `archive_not_found/archive_not_restorable/archive_hash_mismatch/restore_target_conflict/restore_apply_failed`；成功为 `operation_id/archive_id/state=committed`（`src/archive/archive_service.py:258`，符号 `_apply_restore`）。 | wrapper → `ArchiveService(actor="mcp").plan_restore/apply` → manifest/hash 校验 → staging → 只创建不存在的目标 → active index update → journal committed（`src/app/server.py:406`；`src/archive/archive_service.py:262`–`:290`）。 |

共同 MCP 边界：8 个函数均由 `_register = mcp.tool()` 注册，server 版本来自进程级 `RUNTIME_PROVENANCE.server_version`（`src/app/server.py:47`，符号 `mcp`；`:121`，`_register`）。工具没有独立的输出模型，公开返回是 `dict[str, Any]`；领域失败通常也作为正常 MCP tool result 中的 `{ok:false, code, error}` 返回，而不是 MCP transport error。

### 3. 创建、来源与链接维护的数据契约

#### 3.1 正式笔记模型与目录

- `save_obsidian_note` 支持 `spec/plan/troubleshooting/researches/knowledge/entity/chat`；项目型前四类写入 `wiki/projects/<project>/...`，`knowledge` 写入 `wiki/concepts/<domain>/`，`entity` 写入 `wiki/entities/<domain>/`（`src/wiki/note_writer.py:21`、`:202`–`:228`，符号 `save_obsidian_note`）。
- frontmatter 至少包含 `type/generated=false/project/author=copilot/updated_at/tags/title`，并按类型补 `status/related_objects/related_scripts/topic/domain` 等（`src/wiki/note_writer.py:104`，符号 `_frontmatter`）。filename/project/domain 均防绝对路径、分隔符、`.`/`..` 与 Windows 保留名（`:34`–`:89`）。
- 正文先做确定性敏感信息脱敏，再自动规范化 wikilink；测试覆盖手机号、邮箱、token、password 脱敏与 wikilink 修复（`src/wiki/note_writer.py:236`–`:238`；`tests/wiki/test_save_obsidian_note.py:323`，`test_redacts_sensitive_body_without_corrupting_frontmatter`；`:408`，`test_write_note_returns_wikilink_target_and_normalizes_wikilinks`）。
- `related_pages` 只接受现存的 `wiki/**/*.md`，去重后追加默认 `## 参考来源`；无效项不阻断主写入，而是返回 skip/warning。raw 路径在这里被标记为 `raw_source_use_sources`（`src/wiki/reference_section.py:17`，符号 `build_reference_section`；`:41`–`:77`）。
- `sources` 只接受现存的 `raw/sources/**` 文件；无效项同样是软失败（`src/wiki/reference_section.py:80`，符号 `validate_raw_sources`）。测试同时覆盖合法 related page、合法 raw source 和两类错误分流（`tests/wiki/test_save_obsidian_note.py:348`，`test_related_pages_and_raw_sources_are_written_with_invalid_entries_skipped`）。

#### 3.2 摄入与 raw 事实层

- 文本资格是后缀白名单加“无 NUL、可完整 UTF-8 decode”；`.py`、PDF/Office/媒体和伪文本二进制都作为 asset，只复制字节/hash、不进语义索引（`src/wiki/ingest_service.py:15`、`:131`，符号 `_is_text_knowledge_source`；`tests/wiki/test_ingest_service.py:38`、`:59`）。
- 相同目标按 SHA-256 判断 `new/unchanged/modified`；相同内容不重复复制（`src/wiki/ingest_service.py:72`–`:76`）。
- 非 chat 文本进入独立 raw FTS；chat 文本进入 active FTS。测试锁定 raw/active 物理隔离（`src/wiki/ingest_service.py:91`–`:102`；`tests/wiki/test_ingest_service.py:26`，`test_single_file_ingest_indexes_non_chat_sources_in_the_raw_store`）。
- raw 变化不会自动生成 Wiki 页面，只把已登记依赖标为 stale/review_required 并 supersede 旧任务（`src/wiki/ingest_service.py:88`–`:120`；`.trellis/spec/backend/knowledge-compilation.md:9`–`:16`）。

#### 3.3 依赖投影

- `.llm-wiki/knowledge-dependencies.sqlite3` 是可重建依赖投影，核心表为 `knowledge_pages` 与 `source_edges`；记录 page hash、freshness、lifecycle、generated、maintenance、replacement 以及 source path/hash（`src/wiki/knowledge_dependencies.py:19`、`:41`–`:70`，类 `KnowledgeDependencies`）。
- raw source 变化时，人工或 manual-maintained 页面转 `review_required`，其他生成页转 `stale`；归档状态只能经 ArchiveService，依赖服务直接请求 `archived` 返回 `archive_service_required`（`src/wiki/knowledge_dependencies.py:72`–`:81`，符号 `source_changed`；`:95`–`:109`，`lifecycle`）。

### 4. 受控更新：现有保护与实际边界

- 允许路径仅 `wiki/concepts/`、`wiki/entities/`、`wiki/projects/`，拒绝 index、绝对路径和 traversal（`src/wiki/wiki_update.py:21`–`:23`、`:133`–`:143`，符号 `_target`）。
- `type/concept_id/entity_id/entity_type/created/source_path/source_hash` 是 locked fields；已退役的 `source_capsules/source_capsule` 一律拒绝（`src/wiki/wiki_update.py:21`–`:22`）。
- preview 计算当前全文 hash、基于 path/current hash/body/frontmatter 的 plan ID、diff、removed sources、locked violations、wikilink 结果（`src/wiki/wiki_update.py:26`–`:31`、`:34`–`:64`）。
- apply 会检查可选 `expected_hash` 和可选 `plan_id`、拒绝 inactive/CodeGraph-managed 页面，之后使用统一 `write_wiki_page` 写入并刷新 active retrieval、依赖投影、导航和 wiki log（`src/wiki/wiki_update.py:67`–`:130`）。
- 生成页被人工更新后保留 `generated=true`，但 `maintenance` 设为 `manual`，并保留 generation provenance（`src/wiki/wiki_update.py:108`–`:113`）。

### 5. 查询、索引与返回契约

#### 5.1 索引模型

- `RetrievalIndexStore` 只有 `active/archive/raw` 三种 scope，默认文件分别为 `.llm-wiki/retrieval.sqlite3`、`.llm-wiki/archive-index.sqlite3`、`.llm-wiki/raw-retrieval.sqlite3`；自定义 index path 必须仍位于 vault 内（`src/retrieval/retrieval_index.py:79`–`:93`，类 `RetrievalIndexStore`）。
- `IndexedPage` 保存 path/title/body/frontmatter/corpus/authority/lifecycle/source kind、原始与脱敏 hash、文件统计及 history provenance；查询命中模型 `PassageHit` 保存 passage/page/heading/text/score/corpus/authority/source kind（`src/retrieval/retrieval_index.py:48`–`:76`，数据类 `IndexedPage`、`PassageHit`）。
- `status()` 返回 missing/incompatible/fresh/stale、schema version、page/passage count、fingerprint；`build()` 先建临时 SQLite、验证后原子替换；`update_page/delete_page/reconcile` 是显式写边界（`src/retrieval/retrieval_index.py:95`–`:188`）。archive 不支持 reconcile，必须从 bundles rebuild（`:171`–`:176`）。
- FTS 查询在 SQL `LIMIT` 前排除导航页、活动 `wiki/sources/**`、CodeGraph raw JSON，并隔离 project_code（`src/retrieval/retrieval_index.py:190`，符号 `search_fts`，尤其 `:228`–`:260`）。
- 可选 vector 索引只由 CLI 显式 status/build/update；普通 query 不建库。向量索引与 raw inclusion/model identity 不兼容时要求 full build（`.trellis/spec/backend/local-vector-retrieval.md:19`–`:35`；`tests/retrieval/test_vector_index.py:29`、`:58`）。

#### 5.2 Query V2

- `QueryFilters` 的领域模型支持 `type/tags/path_prefix`，并规范化 path_prefix 反斜杠（`src/retrieval/query_pipeline.py:331`–`:350`，类 `QueryFilters`）。
- `scope=auto` 根据 intent 选择 knowledge/history；archive/raw 各自打开物理隔离 store（`src/retrieval/query_pipeline.py:353`–`:368`，符号 `_effective_scope`；`:2094`–`:2099`，`run_query_v2`）。
- 检索顺序包括严格 FTS、可选 vector、title recall、RRF/authority/title bonus、图扩展、relaxed recovery 与 entity discovery/batch；批量超过 40 个 entity 需要 fingerprinted confirmation token，并提供 continuation token（`src/retrieval/query_pipeline.py:1876`–`:2013`，符号 `_run_entity_batch`；`:2016`，`_stage_one_fts_hits`；`:2052`，`run_query_v2`）。
- public wrapper 把配置快照中的 embedding、telemetry、hard budget 和 lexical policy传给领域函数；调用方不能覆盖模型、index path、图 hop 或 context window（`src/app/server.py:252`–`:267`；`tests/app/test_server_tools.py:233`，`test_public_query_schema_has_logical_vault_and_no_runtime_overrides`）。
- `_with_timeout` 用 daemon thread 等待固定 300 秒；超时只返回结构化 `query_timeout`，没有取消工作线程（`src/app/server.py:125`–`:155`，符号 `_with_timeout`）。

### 6. 归档/恢复模型与原子性

- `ArchivePlan` 包含 `plan_id/operation_type/archive_id/created_at/expires_at/items/plan_hash/reason/blockers/cascade/restorable/attachments`；`ArchiveManifest` 记录不可变 items、hash、actor、reason、restorable 与附件（`src/archive/archive_models.py:60`–`:124`，数据类 `ArchiveManifest`、`ArchivePlan`）。
- `ArchiveService` 初始化 `.llm-wiki/state.sqlite3`，持久化 plan、operation、items、events 与 tombstones；合法状态 reducer 为 `planned → staged → pending → detaching → committed`，失败进入 rollback/recoverable 分支（`src/archive/archive_service.py:42`–`:72`，类 `ArchiveService`；`src/archive/archive_models.py:11`）。
- archive apply 在动文件前重新 plan 并对照 item/attachment hash，复制到 staging、写 manifest、原子 rename 到 pending，保留 recovery copy，detach active 和索引，最后 rename 到不可变 bundle；失败调用 recovery（`src/archive/archive_service.py:177`–`:256`，符号 `_apply_archive`）。
- restore 验证 bundle/manifest/hash，不覆盖内容不同的现存目标；只为本次 operation 创建的文件可在失败时回滚（`src/archive/archive_service.py:258`–`:294`，符号 `_apply_restore`）。
- 测试覆盖 plan gate、manifest 不变、dependency blocker/cascade、detaching/final rename 故障恢复、restore 回滚和 legacy migration（`tests/archive/test_archive_lifecycle.py:18`、`:55`、`:77`、`:86`、`:105`、`:123`）。

### 7. MCP、CLI/admin、内部与退役能力分层

#### 7.1 公共 MCP

- 只有上述 8 个工具。没有 MCP 级 list/detail/delete/purge/recover/rebuild-index/config/build-vector/lint/verify/debug 工具。
- public archive 只暴露 plan/apply，且 `wiki_archive` 每次只接受一个 target；partial restore targets、force namespace、restorable flag 和 attachments 都是内部服务参数，没有暴露给 MCP（`src/app/server.py:378`、`:397`；`src/archive/archive_service.py:74`、`:96`）。

#### 7.2 CLI/admin

`src/app/cli.py` 的 parser/dispatch 明确提供以下非 MCP 能力（`src/app/cli.py:59`，符号 `_build_parser`；`:341`，`main`）：

- `init`、runtime `status`（`:63`–`:70`）；
- `config validate/show/set-retrieval/set-privacy/set-telemetry/set-archive`（`:71`–`:100`，处理在 `:187`–`:216`）；
- 只读 `retrieval-eval`（`:101`–`:115`，处理在 `:219`–`:253`）；
- `vector status/build/update`，可显式 model/index/include-raw（`:116`–`:127`，处理在 `:267`–`:294`）；
- passage FTS `index status/build/update`，scope 可 active/archive/raw（`:128`–`:134`，处理在 `:314`–`:324`）；
- archive admin `status/recover/rebuild-index/purge/migrate`。purge 需要 archive id，可传 `--authorize` 与 `--forget`；migration 默认 plan，`--apply` 才提交（`:135`–`:141`，处理在 `:327`–`:338`）。

CLI 并不提供普通 note ingest/write/update/query/archive plan/apply/restore 命令；这些是 MCP/领域 API，而不是 CLI 对称面。

#### 7.3 内部能力

- `sync_retrieval_index` 同时维护 active/raw projection；`refresh_indexes` 是导航生成与 retrieval 同步边界（`src/wiki/ingest_service.py:32`，符号 `sync_retrieval_index`；`src/wiki/wiki_index.py:26`–`:55`，`refresh_indexes`）。
- `KnowledgeDependencies` 管 raw→page stale/lifecycle；`GenerationQueue` 只保留通用 job 状态机、审计事件、lease/review/supersede（`src/wiki/knowledge_dependencies.py:19`；`src/wiki/generation_queue.py:38`）。
- `ArchiveService` 还支持 recover/rebuild/purge/partial restore/internal attachments 等 admin 功能，但不注册为普通 MCP。
- 一次性脚本 `scripts/archive_wiki_sources.py` 默认 dry-run，显式 `--apply` 才清理退役 `wiki/sources/**`、supersede capsule jobs 并提交不可恢复 bundle；它不会改 raw（`scripts/archive_wiki_sources.py:1`–`:6`，模块说明；`:231`，符号 `run`；`:319`，`main`）。它不是常态 CLI 子命令（`.trellis/spec/backend/knowledge-compilation.md:59`–`:68`）。

#### 7.4 已禁用/退役

- `source_capsule`、`chat_source_capsule` 是 `DISABLED_JOB_TYPES`；新建会抛 disabled，claim SQL 排除它们，遗留 job 可 supersede（`src/wiki/generation_queue.py:22`、`:89`–`:124`、`:158`–`:176`，类 `GenerationQueue`）。
- 活动 `wiki/sources/**`、source index/capsule worker 已退役；活动写入拒绝 `source_capsules/source_capsule`（`.trellis/spec/backend/knowledge-compilation.md:3`–`:16`；`src/wiki/wiki_update.py:21`–`:22`）。
- `wiki_generation` 不再注册。即使配置 `tool_profile="worker"`，server 仍精确暴露同一 `CORE_TOOLS`（`tests/app/test_server_tools.py:112`，`test_worker_profile_does_not_reintroduce_retired_generation_tool`；`README.md:165`）。
- 当前配置类型只允许 `core|worker`，没有 `full` 或 custom profile 模式（`src/runtime/runtime_config.py:87`–`:91`，数据类 `GlobalConfig`；`:304`–`:306` 的 decoder）。

### 8. 错误语义

- MCP/领域层以稳定 `code` 为主，但没有统一错误基类贯穿所有模块：runtime 用 `RuntimeConfigError`、archive 用 `ArchiveError`、retrieval/vector/CodeGraph 各有自己的 exception；wrapper 最终通常转换为 `{ok:false, code, error}`（`src/app/server.py:108`，`_tool_error`；`src/archive/archive_models.py:18`，`ArchiveError`）。
- 配置/vault 共享错误包括 `missing_default_vault/unknown_vault/ambiguous_vault_selector/invalid_vault_root`；legacy root 成功不是错误而是 warning（`src/runtime/runtime_config.py:341`，`ConfigRegistry.resolve_vault`；`src/app/server.py:94`，`resolve_tool_vault`）。
- 查询有一个重要例外：缺/坏索引可返回 `ok=true`、带 `code/message` 的空结果，表达“请求本身成功但未执行检索”；MCP transport 仍不报错（`src/retrieval/query_pipeline.py:2101`–`:2113`）。
- `related_pages` 与 `sources` 的单项错误采用“主操作成功 + skipped/warnings”，不是整体失败（`src/wiki/reference_section.py:23`–`:29`；`src/wiki/note_writer.py:280`–`:289`）。
- `.trellis/spec/backend/error-handling.md:1`–`:50` 仍是模板，没有记录上述实际稳定 code 与跨层映射；这本身是规范覆盖空白。

### 9. 安全与权限边界

- server 是本地 stdio MCP，没有用户/角色 ACL 或工具级授权检查；有效权限等于 server 进程的 OS 文件权限和 MCP 客户端访问权。逻辑 vault 经启动时 `ConfigRegistry` 快照解析；普通请求不能修改运行配置（`src/runtime/runtime_config.py:341`，`resolve_vault`；`.trellis/spec/backend/runtime-configuration.md:5`–`:19`）。
- 所有公开 vault 工具走统一 resolver；逻辑 vault 与 legacy root 混用、两个 legacy 值冲突会拒绝。legacy absolute root 仍允许调用方选择任意 server 可访问绝对目录，并仅附 deprecation warning（`src/app/server.py:83`–`:105`）。
- `wiki_ingest` 对目标路径段做 traversal/Windows 名校验，但 `source_path` 只要求是 server 可读文件，没有 source allowlist 或 workspace containment（`src/wiki/ingest_service.py:55`–`:70`）。这是本地受信客户端边界，不是多租户上传边界。
- `wiki_write_note` 目标被限制在预定义 Wiki 目录且禁止覆盖；`wiki_update` 再加 allowed-prefix/index/CodeGraph ownership 保护（`src/wiki/note_writer.py:202`–`:234`；`src/wiki/wiki_update.py:133`–`:143`）。
- public archive 无 purge/force；不可逆 purge 只在 CLI/admin，并要求显式授权或 retention policy。`forget` tombstone 不保留可识别路径是 spec 合同（`.trellis/spec/backend/archive-lifecycle.md:35`–`:43`）。
- status wrapper 删除 `vault_root`、CodeGraph executable，配置 status 不回显 model path/规则正文；测试锁定该行为（`src/app/server.py:167`–`:176`；`tests/app/test_server_tools.py:280`，`test_status_hides_absolute_vault_and_model_paths`）。

### 10. 可观测性

- `wiki_status` 是主要公共可观测面：版本/revision/dirty/start time、结构缺失、三套 retrieval 状态、vector、queue、config policy、archive index/operation counts（`src/wiki/wiki_files.py:14`–`:32`；`src/app/server.py:159`–`:183`）。MCP handshake 与 status 共用 `RUNTIME_PROVENANCE`（`tests/app/test_server_tools.py:70`，`test_mcp_initialization_version_matches_runtime_provenance`）。
- retrieval status 暴露 scope、state、page/passage count、fingerprint、schema version，但不暴露绝对 index path（`src/retrieval/retrieval_index.py:95`–`:106`）。
- 写笔记与更新追加 `wiki/log.md` 领域日志；archive 与 generation 在 SQLite 中各有 event 表（`src/wiki/note_writer.py:264`；`src/wiki/wiki_update.py:128`；`src/archive/archive_service.py:67`–`:71`；`src/wiki/generation_queue.py:77`–`:84`）。
- telemetry 配置默认为 enabled、90 天、永不存 query body；public status 只暴露安全摘要（`src/runtime/runtime_config.py:62`–`:66`、`:359`–`:378`，数据类 `TelemetrySettings`、`ConfigRegistry.public_status`）。
- `.trellis/spec/backend/logging-guidelines.md:1`–`:50` 仍为空模板；MCP wrapper 没有统一 structured logger、request/operation correlation ID 或错误日志约定。

### 11. 测试与文档覆盖

#### 已有强覆盖

- MCP registry、SDK handshake、schema、vault resolver/warning、status redaction、query timeout/validation、write/update 参数转发、archive wrapper：`tests/app/test_server_tools.py`。
- note 路径/frontmatter/YAML、正文脱敏、overwrite、related/source 分流、wikilink：`tests/wiki/test_save_obsidian_note.py`。
- ingest 幂等、raw/active 隔离、asset/坏 UTF-8：`tests/wiki/test_ingest_service.py`。
- update preview/apply、CAS happy path、redacted writer、related page、wikilink：`tests/wiki/test_wiki_update.py`。
- retrieval store 物理隔离、build/update/delete/reconcile、projection redaction：`tests/retrieval/test_retrieval_index.py`。
- Query V2 raw/archive 隔离、fallback、stale warning、batch confirmation、扩展：`tests/retrieval/test_query_pipeline.py`。
- archive journal/failure recovery/migration：`tests/archive/test_archive_lifecycle.py` 与 `tests/archive/test_archive_wiki_sources.py`。
- CodeGraph transactional sync/ownership：`tests/codegraph/test_codegraph_sync.py`。

#### 文档

- README 对产品定位、8 个工具、CLI/admin 边界、本地向量和 Wiki 结构有用户向说明（`README.md:1`–`:5`、`:151`–`:165`）。
- `CLAUDE.md:50`–`:55` 概括 archive public/admin 边界和检索主路径。
- 相关可执行规格见后文“Related specs”。

### 12. 已核实的契约不一致与覆盖空白

以下仅记录，不在本研究任务修复。

1. **MCP `path_prefix` 过滤器源码与测试/文档冲突（高确定性）**  
   `QueryFilters` 支持 `path_prefix`（`src/retrieval/query_pipeline.py:331`–`:350`），server docstring 也声称接受（`src/app/server.py:273`–`:277`），测试要求 whitelist 与转发（`tests/app/test_server_tools.py:339`、`:348`）；但当前 wrapper 实际白名单只有 `type/tags`，且错误文本也只写这两项（`src/app/server.py:243`–`:249`）。因此该测试按当前源码应失败，public `path_prefix` 实际不可达。

2. **worker profile 规格已过时（高确定性）**  
   `.trellis/spec/backend/runtime-configuration.md:38`–`:46` 仍写“worker 在 core 上增加 `wiki_generation`”，但 README 明示不再注册，测试锁定 worker 仍为同一 8 工具（`tests/app/test_server_tools.py:112`–`:129`）。当前源码事实是 core/worker 注册面相同。

3. **README Query V2 响应说明与领域 docstring 漂移（高确定性）**  
   README 描述 compact `results` + 唯一正文位于 `context_pack.passages`；`run_query_v2` docstring 则明确旧 `context_pack` 不属于返回合同，正文直接位于 public result（`src/retrieval/query_pipeline.py:2070`–`:2075`）。最终写作应以当前函数实际 return 与协议测试为准，不照抄 README。

4. **`wiki_update` 的 apply 并非强制 plan gate（高确定性）**  
   `plan_id`、`expected_hash` 均为可选；仅在传入时校验（`src/wiki/wiki_update.py:96`–`:100`）。测试明确直接调用无 plan/hash 的 apply 并成功（`tests/wiki/test_wiki_update.py:18`–`:39`）。所以它具有可用 CAS，但 MCP `action=apply` 可绕过 preview；不能把它描述成 archive 那种强制两阶段提交。

5. **正式页来源 hash/依赖登记不完整（高确定性）**  
   `wiki_write_note` 只将验证后的 `sources` 写入 frontmatter，没有计算 `source_hashes`，也没有调用 `KnowledgeDependencies.update_page`（`src/wiki/note_writer.py:242`–`:264`）；`wiki_update` 登记依赖时给每个 source 的 hash 都是空字符串（`src/wiki/wiki_update.py:123`–`:126`）。这与“具体 raw 路径和当前 hash”的 spec（`.trellis/spec/backend/knowledge-compilation.md:34`–`:38`）不一致，并使后续 raw change 的精确 freshness 追踪依赖于不完整投影。现有 note source 测试只断言路径/skip，不断言 hash 或 dependency edge。

6. **update 的 `sources` 未复用 create 的 raw path 校验（高确定性）**  
   create 使用 `validate_raw_sources`；update 只把输入转换成字符串列表并要求非空，没有检查 `raw/sources/**`、存在性或 traversal（`src/wiki/wiki_update.py:106`–`:107`、`:150`–`:152`）。因此 update 可写入任意非空 source 字符串。

7. **`wiki_ingest.metadata` 是公开但无效参数（高确定性）**  
   wrapper 明确 `del metadata`，领域函数没有 metadata 参数（`src/app/server.py:327`–`:334`）。调用方传入的 metadata 静默丢弃，无 warning。

8. **chat 可通过两个语义不同的入口进入（高确定性）**  
   `wiki_write_note(note_type="chat")` 走 `ChatMemoryService` 的 revision/metadata/脱敏合同（`src/wiki/note_writer.py:174`–`:184`）；但 `wiki_ingest(source_type="chat")` 可把任意 UTF-8 文件直接复制到 chat raw 路径并投入 active index（`src/wiki/ingest_service.py:91`–`:102`），不执行 ChatMemory 的角色/metadata 校验。这与 chat 规范的唯一修订路径和显式角色约束（`.trellis/spec/backend/knowledge-compilation.md:27`–`:38`）存在边界重叠。

9. **`wiki_status` 的“read-only”说明与实际副作用冲突（高确定性）**  
   wrapper docstring 称 read-only（`src/app/server.py:159`–`:160`），但每次都会构造 `ArchiveService`；其构造函数创建 `.llm-wiki` 目录、SQLite 文件和表（`src/app/server.py:174`；`src/archive/archive_service.py:42`–`:72`）。领域 `wiki_files.wiki_status` 自身不创建 Wiki 结构，但 MCP wrapper 可能初始化 archive state。现有 `test_wiki_status_reports_missing_structure_without_creating_it` 只覆盖领域 helper（`tests/wiki/test_wiki_files.py:45`），未覆盖 wrapper 的 archive side effect。

10. **write-note 返回绝对路径，而 status 明确隐藏绝对路径（高确定性）**  
    `save_obsidian_note` 成功返回 `absolute_path`（`src/wiki/note_writer.py:269`–`:279`）；public status 则专门移除 `vault_root`/executable 并由测试锁定（`src/app/server.py:167`–`:176`；`tests/app/test_server_tools.py:280`）。这形成公共信息暴露策略不一致，现有 write schema/return 测试未检查绝对路径。

11. **write-note 的 `auto_index/indexed` 返回语义不清（高确定性）**  
    函数接受 `auto_index`，但无条件调用 `refresh_indexes`，又固定返回 `indexed=None`，且忽略 refresh 返回值（`src/wiki/note_writer.py:157`–`:159`、`:259`–`:275`）。测试名称也明确“auto_index is ignored and returns null indexed”（`tests/wiki/test_save_obsidian_note.py:225`）。public wrapper 固定传 `auto_index=True`，调用方无法判断 projection 是否成功。

12. **write-note 多步写入不是事务（中高确定性）**  
    页面 `write_text` 后依次刷新 index/overview/log；后续 `OSError` 会返回 `write_failed`，但没有删除已写页面或回滚先前投影（`src/wiki/note_writer.py:259`–`:266`）。与 archive 的 journal/recovery 原子性明显不同。现有测试覆盖 file_exists/overwrite，但未见 post-write maintenance fault rollback 测试。

13. **正文与 metadata 的脱敏边界不一致（中高确定性）**  
    note writer 只对 `content` 调 `redact_sensitive_text`，随后把原始 `title/tags` 等写入 YAML（`src/wiki/note_writer.py:104`–`:138`、`:236`–`:257`）。测试专门断言正文脱敏，但 YAML injection 测试保留原值。retrieval projection 可能再次脱敏，但 vault Markdown frontmatter 本身仍可含敏感值。update 使用 `write_wiki_page` 的边界不同。

14. **timeout 不取消查询线程（高确定性）**  
    `_with_timeout` 超时后返回，但 daemon worker 继续运行（`src/app/server.py:125`–`:155`）。如果查询后段写遥测，调用方收到 timeout 后仍可能出现后台活动；当前测试只断言响应时限（`tests/app/test_server_tools.py:381`）。

15. **规范层 error/logging/database 指南为空（高确定性）**  
    `.trellis/spec/backend/error-handling.md`、`logging-guidelines.md`、`database-guidelines.md` 均仍是占位模板；实际项目已经有多个 SQLite store、事务模式、稳定错误码和事件表，却没有统一规范索引。这会增加跨模块 drift 风险。

### 13. Files found

| 文件 | 一句话说明 |
| --- | --- |
| `src/app/server.py` | 8 个 MCP 工具、共享 vault resolver、query timeout 与响应适配。 |
| `tests/app/test_server_tools.py` | core registry、MCP schema/协议、resolver、status redaction 和 wrapper 回归合同。 |
| `src/app/cli.py` | 配置、评测、vector/index 与 archive admin CLI 边界。 |
| `src/wiki/note_writer.py` | 正式笔记/chat 创建、路径/frontmatter、脱敏、链接/来源与维护副作用。 |
| `src/wiki/ingest_service.py` | 单文件 raw snapshot、asset/text 分流、FTS 投影与 stale 传播。 |
| `src/wiki/wiki_update.py` | preview/apply、CAS、locked fields、CodeGraph ownership 与更新后投影。 |
| `src/wiki/reference_section.py` | related_pages 与 raw sources 的路径校验、去重、skip/warning。 |
| `src/wiki/knowledge_dependencies.py` | raw→page source edge 与 freshness/lifecycle SQLite projection。 |
| `src/wiki/generation_queue.py` | 通用 generation job journal 及退役 capsule job 屏蔽。 |
| `src/wiki/wiki_files.py` | 领域 status 聚合。 |
| `src/wiki/wiki_index.py` | 导航 index 生成及 explicit retrieval sync 边界。 |
| `src/retrieval/retrieval_index.py` | active/archive/raw SQLite FTS store、IndexedPage/PassageHit 与生命周期。 |
| `src/retrieval/query_pipeline.py` | Query V2 filters、scope、FTS/vector/RRF/graph、fallback 与 batch。 |
| `src/retrieval/vector_index.py` | 显式本地向量 index build/update/status/search。 |
| `src/archive/archive_models.py` | archive reason/state、plan/manifest/item/attachment/tombstone 数据模型。 |
| `src/archive/archive_service.py` | plan/apply、journal、恢复、索引、purge/status admin。 |
| `src/codegraph/codegraph_policy.py` | CodeGraph raw/page ownership及 project_code 检索隔离。 |
| `src/codegraph/codegraph_sync.py` | 外部 CodeGraph DB 到 raw/architecture/active index 的事务同步。 |
| `scripts/archive_wiki_sources.py` | 退役 `wiki/sources` 的一次性审计归档脚本。 |
| `README.md`、`CLAUDE.md` | 用户工具面、CLI/admin 与架构概览；存在部分漂移。 |
| `tests/wiki/test_save_obsidian_note.py` | note 创建、脱敏、路径、覆盖、链接/来源测试。 |
| `tests/wiki/test_ingest_service.py` | ingest 幂等与 raw/asset/index 分流测试。 |
| `tests/wiki/test_wiki_update.py` | update preview/apply、CAS、redaction、链接测试。 |
| `tests/retrieval/test_retrieval_index.py`、`test_query_pipeline.py` | 物理隔离、索引生命周期和 Query V2 行为。 |
| `tests/archive/test_archive_lifecycle.py`、`test_archive_wiki_sources.py` | archive journal、回滚、migration 与退役 source 归档。 |
| `tests/codegraph/test_codegraph_sync.py` | CodeGraph 同步前置校验、写入、回滚和人工页保护。 |

### 14. External references

- 本主题限定当前本地工作树，未使用外部二手资料。
- README 声明运行依赖为 MCP Python SDK `mcp>=2,<3`，stdio transport 与 `MCPServer(..., version=...)`（`README.md:17`–`:21`）；本轮未联网核验 SDK 当前外部文档，因为任务要求的是本地能力基线。

### 15. Related specs

- `.trellis/spec/backend/runtime-configuration.md`：typed vault snapshot、公共 tool/profile、selector 与 status 脱敏合同；其中 worker profile 一项已与代码漂移。
- `.trellis/spec/backend/runtime-provenance.md`：MCP handshake/status 共享进程身份。
- `.trellis/spec/backend/knowledge-compilation.md`：raw provenance、正式页显式写入、退役 capsule/chat 合同。
- `.trellis/spec/backend/local-vector-retrieval.md`：query-time read-only、显式 vector lifecycle 与 offline provider。
- `.trellis/spec/backend/archive-lifecycle.md`：plan gate、journal、archive index isolation、purge/admin、migration。
- `.trellis/spec/backend/codegraph-sync.md`：CodeGraph 外部 SQLite 同步与 managed page/project_code 边界。
- `.trellis/spec/backend/retrieval-evaluation.md`：Query V2 评测与 top_k/报告合同。
- `.trellis/spec/backend/error-handling.md`、`logging-guidelines.md`、`database-guidelines.md`：当前仍为模板，未承载实际合同。

## Caveats / Not Found

- 按主任务要求在证据足以支持规划后停止扩展搜索。`run_codegraph_sync` 顶层完整返回字典、`run_query_v2` 第 2218 行之后的最终结果组装、`ArchiveService.purge/rebuild_archive_index/status` 完整方法体未逐行全部展开；本文件只陈述已由 wrapper、模型、规格和测试共同确认的部分。
- CodeGraph 的“Blast radius / covering tests”提示只表示索引能追踪到的静态关系；动态调用、monkeypatch 和协议级测试可能不全。因此测试覆盖评价以明确读取到的测试函数为准。
- 未运行 pytest/ruff/type-check；“当前测试应失败”的判断仅用于第 12.1 项的静态源码矛盾，最终实现前应单独执行 `tests/app/test_server_tools.py` 验证。
- `README.md` 的 Markdown 工具表含未转义 `|`，终端渲染截断了部分单元格；工具精确签名以 `src/app/server.py` 为准。
- 本研究没有读取或修改用户的 `.netsuite-mcp/` 未跟踪目录，也没有执行任何 git 写操作。
- 取证完成后并行工作使 `src/app/server.py` 与 `tests/app/test_server_tools.py` 变为 modified；受“停止扩展搜索”指令约束，本文没有重新读取它们。尤其第 12.1 项 `path_prefix` 冲突可能正被并行修改处理，主会话在最终引用前应以合并后的 diff/测试重新核验。

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库是一个本地 MCP server：把 CodeGraph 派生的代码事实、LLM 分阶段摄入结果、人工笔记和研究综合写入外部 Obsidian Markdown Wiki，并用关键词 + wikilink 图查询返回可引用 context pack。安装、MCP 客户端配置和工具清单以 [README.md](README.md) 为准；这里保留开发时最需要的命令和跨文件架构约定。

## 常用命令

- 安装/同步开发环境：`uv sync --extra dev`
- 运行全部测试：`uv run pytest`
- 运行单个测试文件：`uv run pytest tests/test_wiki_query.py`
- 运行单个测试函数：`uv run pytest tests/test_wiki_query.py::test_function_name -v`
- 启动 MCP server：`uv run netsuite-llm-wiki-mcp-server`、`uv run netsuite-llm-wiki-mcp server` 或 `uv run python -m netsuite_llm_wiki_mcp.server`
- CLI 初始化 vault：`uv run netsuite-llm-wiki-mcp init --vault <name> --root <path> --default`
- CLI 查看状态：`uv run netsuite-llm-wiki-mcp status`

项目使用 `uv.lock` 管理开发环境。没有单独配置 lint/typecheck 工具；完成前至少运行相关 `uv run pytest`，较大改动运行全量 `uv run pytest`。

## 架构总览

Python 3.11+，`src/` layout，运行依赖只有 `mcp` 和 `PyYAML`，dev 依赖是 `pytest`。入口点在 [pyproject.toml](pyproject.toml)：`netsuite-llm-wiki-mcp` 调 [cli.py](src/netsuite_llm_wiki_mcp/cli.py)，`netsuite-llm-wiki-mcp-server` 调 [server.py](src/netsuite_llm_wiki_mcp/server.py)。

### MCP 工具入口层

[server.py](src/netsuite_llm_wiki_mcp/server.py) 用 FastMCP 注册所有公开工具，薄封装后委托到业务模块。人工笔记公开入口是 `wiki_write_note`；旧 `save_obsidian_note` 不应再注册。新增或调整 MCP 工具时，通常需要同时改：

1. 业务模块中的纯函数实现。
2. [server.py](src/netsuite_llm_wiki_mcp/server.py) 的 tool wrapper / `@mcp.tool()` 注册。
3. [README.md](README.md) 的工具说明（如果公开行为变化）。
4. [tests/test_server_tools.py](tests/test_server_tools.py) 和对应业务测试。

注册工具清单（按 server.py 顺序）：`wiki_init`、`wiki_status`、`wiki_list_files`、`wiki_read_file`、`wiki_ingest_codegraph`、`wiki_query`、`wiki_query_debug`、`wiki_ingest_llm`、`wiki_rescan`、`wiki_lint`、`wiki_changelog`、`wiki_write_note`、`wiki_enrich`、`wiki_page_merge`、`wiki_dedup`、`wiki_insights`、`wiki_delete_source`、`wiki_research`、`wiki_synthesis`、`wiki_ingest_batch`、`wiki_verify`、`wiki_gap`。

### Vault 与路径模型

- `vault_root` 解析优先级在 [runtime_config.py](src/netsuite_llm_wiki_mcp/runtime_config.py)：工具参数 > `NETSUITE_LLM_WIKI_VAULT_ROOT` > 全局 `config.yaml` 的 `default_vault`。
- 跨平台配置/数据目录在 [platform_paths.py](src/netsuite_llm_wiki_mcp/platform_paths.py)，测试通过 [tests/conftest.py](tests/conftest.py) 自动隔离这些环境变量。
- Wiki 目录创建和 path segment 校验在 [wiki_paths.py](src/netsuite_llm_wiki_mcp/wiki_paths.py)。外部 Obsidian root 固定包含 `purpose.md`、`schema.md`、`raw/sources/{projects,file,references,chat}/`、`raw/assets/`、`wiki/index.md`、`wiki/log.md`、`wiki/overview.md`、`wiki/projects/`、`wiki/concepts/`、`wiki/chatlog/`、`wiki/sources/`、`wiki/queries/`、`wiki/entities/`、`wiki/archives/`、`.obsidian/`、`.llm-wiki/{ingest-cache,graph-index,relation-candidates}/`。
- `wiki/sources/` 是纯索引溯源页目录，按关联目标分级：CodeGraph 索引页写 `wiki/sources/projects/<project>/architecture/codegraph.md`；LLM 摄入索引页写 `wiki/sources/<target_dir>/<project>/<source_name>.md`；会话来源按日期写入 `wiki/sources/chatlog/<yyyy>/<mm>/<dd>/`。索引页只保留 frontmatter、一句话摘要、raw source 路径和指向生成页的 wikilinks，不承载知识内容。
- Cache 路径按 source_type 隔离：`.llm-wiki/ingest-cache/{source_type}/{project}/{source_name}.json`。
- Markdown/frontmatter 读写、覆盖保护和脱敏在 [wiki_io.py](src/netsuite_llm_wiki_mcp/wiki_io.py)。生成页只能覆盖 `generated: true` 页面；人工页不能被静默覆盖。

### 写入与维护流水线

- CodeGraph 摄入在 [wiki_ingest.py](src/netsuite_llm_wiki_mcp/wiki_ingest.py)：`CodeGraphClient`（定义在 [codegraph_client.py](src/netsuite_llm_wiki_mcp/codegraph_client.py)）读取 `status/files/context/impact/graph_snapshot` → 写 `raw/sources/projects/<project>/codegraph/` snapshot（含 `graph.json`、`codefacts.json` 等） → 写 `wiki/projects/<project>/architecture/` 可读页 + `wiki/sources/projects/<project>/architecture/codegraph.md` 索引页 → refresh index/overview/log → 写 `.llm-wiki/ingest-cache/codegraph/`。MCP 工具名为 `wiki_ingest_codegraph`，同步执行不需要 LLM。`profile="suitescript"` 时额外生成 `wiki/projects/<project>/pipelines/` 页（需要完整 graph snapshot）。
- LLM 分阶段摄入同在 [wiki_ingest.py](src/netsuite_llm_wiki_mcp/wiki_ingest.py)：推荐两阶段流程 `stage="prepare"`（读源 + 写 `raw/sources/<source_type>/` snapshot + 返回合并 prompt；`source_type="chat"` 时写到 `raw/sources/chat/<yyyy>/<mm>/<dd>/<source_name>/`）→ `stage="apply"`（校验路径并写 generated pages + 按目标目录写索引页到 `wiki/sources/<target_dir>/<project>/`；`source_type="chat"` 且生成 chatlog 时写到 `wiki/sources/chatlog/<yyyy>/<mm>/<dd>/`）。旧三阶段（`prepare_analysis` / `prepare_generation` / `apply_generation`）仍兼容但不推荐。`source_path` 支持绝对路径和相对于 vault_root 的相对路径。`generation` 参数中 `source_summary` 可以是 dict 或纯字符串（只需一句话摘要）；顶层 `concept`/`concepts` key 会自动合并到 `pages`。当 cache manifest 路径与期望 raw dir 不匹配（如 raw/sources 层级重构后旧路径未同步），`rescan`、`prepare` 和 `prepare_analysis` 会自动调用 `_repair_cache_manifest_paths` 修复 manifest 路径，并在响应中返回 `manifest_repaired`、`repaired_count` 和 `repaired_paths`；修复成功视为 `unchanged`，修复失败则回退到完整 re-snapshot。
- URL 摄入已移除（`wiki_ingest_url` 模块和工具不再存在）；URL 来源统一通过 `wiki_ingest_llm` 以 `source_type="url"` 处理。
- 人工笔记写入在 [note_writer.py](src/netsuite_llm_wiki_mcp/note_writer.py)：note 类型为 `spec`/`plan`/`troubleshooting`/`researches`（项目级，写入 `wiki/projects/<project>/` 对应子目录）和 `knowledge`（写入 `wiki/concepts/<domain>/`，不接受 `project`）。MCP 入口为 `wiki_write_note` 工具，server 层接受 `note_type`/`noteType` 等双参数兼容。
- 写入后维护集中在 [wiki_index.py](src/netsuite_llm_wiki_mcp/wiki_index.py)、[wiki_overview.py](src/netsuite_llm_wiki_mcp/wiki_overview.py)、[wiki_log.py](src/netsuite_llm_wiki_mcp/wiki_log.py)。会产生或变更页面的工具通常要刷新 index/overview 并 append log。
- 校验在 [wiki_verify.py](src/netsuite_llm_wiki_mcp/wiki_verify.py)：两阶段 grounding check，`prepare` 从 `wiki/sources/` 索引页出发，通过 frontmatter.sources 读 raw source + 通过 body 中 wikilinks 读关联生成页，返回校验 prompt；`apply` 记录 faithfulness 结果。

### 查询与图谱能力

[wiki_query.py](src/netsuite_llm_wiki_mcp/wiki_query.py) 不使用向量库；主路径是关键词/CJK bigram 命中 → wikilink、shared source、common neighbor、same type 图扩展 → token budget 裁剪 → numbered citation context pack。`enable_vector` 目前只返回未配置警告，不应重新引入 Chroma、embedding 或 `.rag-index` 主路径。

[wikilinks.py](src/netsuite_llm_wiki_mcp/wikilinks.py) 提供 wikilink 格式化和解析工具函数：`format_wikilink`（含表格内 `\| 转义）、`normalize_wikilink_targets`（小写化 + 表格别名处理）、`wikilink_targets`、`split_wikilink_inner`、`table_wikilink_alias_pipe_lines` 等。enrich、query 等模块统一使用此模块处理 wikilink，不内嵌正则。

[wiki_files.py](src/netsuite_llm_wiki_mcp/wiki_files.py) 提供 `wiki_status`（vault 诊断、队列状态、版本、CodeGraph 可用性）、`wiki_list_files`（列出 wiki/sources 下公共文件，支持大小限制）和 `wiki_read_file`（读取文本文件，路径必须在 wiki/ 或 raw/sources/ 下且后缀合法）。MCP 工具为 `wiki_status`、`wiki_list_files`、`wiki_read_file`。

[wiki_models.py](src/netsuite_llm_wiki_mcp/wiki_models.py) 定义核心数据结构 `WikiConfig`、`WikiPage`、`WikiLogEntry`、`WikiSearchResult`、`LintIssue`、`CodeGraphSnapshot`。

[codegraph_client.py](src/netsuite_llm_wiki_mcp/codegraph_client.py) 封装 `codegraph` CLI 调用和 SQLite graph snapshot 读取，提供 `status/files/context/query/callers/callees/impact/graph_snapshot` 方法。[wiki_ingest.py](src/netsuite_llm_wiki_mcp/wiki_ingest.py) 通过 `CodeGraphLike` Protocol 解耦，测试可注入 mock client。

[pipeline_detector.py](src/netsuite_llm_wiki_mcp/pipeline_detector.py) 基于 Louvain 社区检测的 SuiteScript 业务流水线识别：从 CodeGraph 边 + 文本隐式引用（N/task、N/record 等）构建文件级调用图，聚类为 `Pipeline` 对象（含入口点、共享记录、隐式边、置信度）。

[git_utils.py](src/netsuite_llm_wiki_mcp/git_utils.py) 提供 `GitInfo` 数据类和 `get_git_info`、`get_git_commit`、`is_git_dirty`、`get_git_branch` 等零外部依赖 git 辅助函数（用于 `wiki_status` 版本和脏状态检测）。

[context_budget.py](src/netsuite_llm_wiki_mcp/context_budget.py) 为 `wiki_query` 的 context pack 计算 token 预算分配（响应预留、索引、页面、聊天历史、单页上限）。

维护工具按阶段拆分：

- [wiki_lint.py](src/netsuite_llm_wiki_mcp/wiki_lint.py)：结构、frontmatter、source traceability、broken wikilinks、orphan pages、cache manifest。
- [wiki_enrich.py](src/netsuite_llm_wiki_mcp/wiki_enrich.py)：prepare/apply 两阶段 wikilink 富化。apply 阶段使用 [wikilinks.py](src/netsuite_llm_wiki_mcp/wikilinks.py) 的 `format_wikilink` 和 `is_markdown_table_row_at` 生成表格安全链接；搜索 index 时会合并 `index-*.md` 分片；term 替换跳过代码块、行内代码、已有 wikilink 和 Markdown 链接内的保护区。读取时使用 `utf-8-sig` 以兼容 Obsidian UTF-8 BOM 文件，写入时使用 `utf-8`（无 BOM）。
- [wiki_dedup.py](src/netsuite_llm_wiki_mcp/wiki_dedup.py)：detect/confirm/merge 三阶段重复页合并。
- [page_merge.py](src/netsuite_llm_wiki_mcp/page_merge.py)：generated 页面合并，锁定字段保护 + 数组字段 union + 可选 body merge。
- [wiki_insights.py](src/netsuite_llm_wiki_mcp/wiki_insights.py) + [louvain.py](src/netsuite_llm_wiki_mcp/louvain.py)：图谱洞察、社区、桥接页、孤立页。
- [wiki_delete.py](src/netsuite_llm_wiki_mcp/wiki_delete.py)：source 删除及派生页/交叉引用/cache 级联清理。
- [wiki_research.py](src/netsuite_llm_wiki_mcp/wiki_research.py)：prepare/apply 研究综合，写入 `wiki/queries/`。
- [wiki_synthesis.py](src/netsuite_llm_wiki_mcp/wiki_synthesis.py)：prepare/apply 持久化有价值的查询答案或跨页分析，写入 `wiki/projects/<project>/researches/`（`project` 参数必填）。
- [wiki_gap.py](src/netsuite_llm_wiki_mcp/wiki_gap.py)：覆盖缺口分析 analyze/suggest 两阶段，扫描浅页面、悬空链接、未摄入源、分类法缺失，推荐补充动作。
- [wiki_batch.py](src/netsuite_llm_wiki_mcp/wiki_batch.py)：持久化 ingest 队列 `.llm-wiki/ingest-queue.json`。除原有 queue CRUD（enqueue / next / complete / fail / retry / cancel / clear_done）外，新增三个批量 action：`reapply`（从 cache 重新 apply，无需 LLM；含页面完整性检测，报告 `regeneration_needed`、`pages_restored`、`index_refreshed`）、`prepare_all`（批量运行 prepare，标记 `prepared` 状态）、`apply_all`（批量运行 apply，完成 `prepared` → `done` 转换）。任务生命周期扩展为 `pending → processing → prepared → done`（任意阶段可 `failed`）。

## 必守约定

- 代码事实首版来自 CodeGraph；不要重新引入本项目代码扫描 + embedding 的 RAG 主路径。
- 旧 RAG 工具和旧人工笔记入口 `save_obsidian_note` 不应注册；人工笔记公开入口统一为 `wiki_write_note`。
- 不再保存 `script` / `object` 人工事实页；对象、部署、字段和脚本参数折叠到 CodeGraph 派生页或 source 摘要。
- `knowledge` 写入 `wiki/concepts/<domain>/`，禁止 `project`。
- 项目目录固定为 `wiki/projects/<project>/{index.md,specs/,plans/,architecture/,pipelines/,troubleshooting/,researches/,sources/}`。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。
- 不要在代码、测试或文档中硬编码个人 Vault 路径、API key、token、邮箱、手机号等敏感信息；脱敏逻辑在 [redaction.py](src/netsuite_llm_wiki_mcp/redaction.py)。
- 所有 MCP 工具写入文件统一使用 `encoding="utf-8"`（无 BOM）；读取 Obsidian 文件时可用 `utf-8-sig` 以兼容 BOM，但写入绝不产生 BOM。禁止使用 PowerShell `Set-Content` 默认编码（UTF-16 LE）修改项目文件。

## 测试定位

测试文件按模块一一对应，命令为 `uv run pytest tests/test_<module>.py`：
- CLI/runtime/config：`test_cli.py`、`test_runtime_config.py`、`test_readme_global_mcp_docs.py`
- Wiki 基础设施（paths/io/index/overview/log/files）：`test_wiki_paths.py`、`test_wiki_io.py`、`test_wiki_index.py`、`test_wiki_overview.py`、`test_wiki_log.py`、`test_wiki_files.py`
- CodeGraph client / 摄入 / normalize：`test_codegraph_client.py`、`test_wiki_ingest_codegraph.py`、`test_wiki_ingest_normalize.py`
- MCP 工具注册：`test_server_tools.py`
- 人工 note：`test_save_obsidian_note.py`
- 查询 / 上下文预算 / wikilink：`test_wiki_query.py`、`test_context_budget.py`、`test_wiki_enrich.py`
- 维护工具（lint/enrich/merge/dedup/insights/delete/verify/gap/batch/research/synthesis）：对应 `test_wiki_lint.py`、`test_wiki_enrich.py`、`test_page_merge.py`、`test_wiki_dedup.py`、`test_wiki_insights.py`、`test_louvain.py`、`test_wiki_delete.py`、`test_wiki_verify.py`、`test_wiki_gap.py`、`test_wiki_batch.py`、`test_wiki_research.py`、`test_wiki_synthesis.py`
- Git 辅助 / pipeline 检测：`test_git_utils.py`、`test_pipeline_detector.py`

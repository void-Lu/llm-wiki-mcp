# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库是一个本地 MCP server：把 CodeGraph 派生的代码事实、LLM 分阶段摄入结果、人工笔记和研究综合写入外部 Obsidian Markdown Wiki，并用关键词 + wikilink 图查询返回可引用 context pack。安装、MCP 客户端配置和工具清单以 [README.md](README.md) 为准；这里保留开发时最需要的命令和跨文件架构约定。

## 常用命令

- 安装开发依赖：`python -m pip install -e ".[dev]"`
- 运行全部测试：`pytest`
- 运行单个测试文件：`pytest tests/test_wiki_query.py`
- 运行单个测试函数：`pytest tests/test_wiki_query.py::test_function_name -v`
- 启动 MCP server：`netsuite-llm-wiki-mcp-server`、`netsuite-llm-wiki-mcp server` 或 `python -m netsuite_llm_wiki_mcp.server`
- CLI 初始化 vault：`netsuite-llm-wiki-mcp init --vault <name> --root <path> --default`
- CLI 查看状态：`netsuite-llm-wiki-mcp status`

项目没有单独配置 lint/typecheck 工具；完成前至少运行相关 `pytest`，较大改动运行全量 `pytest`。

## 架构总览

Python 3.11+，`src/` layout，运行依赖只有 `mcp` 和 `PyYAML`，dev 依赖是 `pytest`。入口点在 [pyproject.toml](pyproject.toml)：`netsuite-llm-wiki-mcp` 调 [cli.py](src/netsuite_llm_wiki_mcp/cli.py)，`netsuite-llm-wiki-mcp-server` 调 [server.py](src/netsuite_llm_wiki_mcp/server.py)。

### MCP 工具入口层

[server.py](src/netsuite_llm_wiki_mcp/server.py) 用 FastMCP 注册所有公开工具，薄封装后委托到业务模块。人工笔记公开入口是 `wiki_write_note`；旧 `save_obsidian_note` 不应再注册。新增或调整 MCP 工具时，通常需要同时改：

1. 业务模块中的纯函数实现。
2. [server.py](src/netsuite_llm_wiki_mcp/server.py) 的 tool wrapper / `@mcp.tool()` 注册。
3. [README.md](README.md) 的工具说明（如果公开行为变化）。
4. [tests/test_server_tools.py](tests/test_server_tools.py) 和对应业务测试。

### Vault 与路径模型

- `vault_root` 解析优先级在 [runtime_config.py](src/netsuite_llm_wiki_mcp/runtime_config.py)：工具参数 > `NETSUITE_LLM_WIKI_VAULT_ROOT` > 全局 `config.yaml` 的 `default_vault`。
- 跨平台配置/数据目录在 [platform_paths.py](src/netsuite_llm_wiki_mcp/platform_paths.py)，测试通过 [tests/conftest.py](tests/conftest.py) 自动隔离这些环境变量。
- Wiki 目录创建和 path segment 校验在 [wiki_paths.py](src/netsuite_llm_wiki_mcp/wiki_paths.py)。外部 Obsidian root 固定包含 `purpose.md`、`schema.md`、`raw/sources/`、`raw/assets/`、`wiki/index.md`、`wiki/log.md`、`wiki/overview.md`、`wiki/projects/`、`wiki/concepts/`、`wiki/sources/`、`wiki/queries/`、`wiki/synthesis/`、`wiki/comparisons/`、`.obsidian/`、`.llm-wiki/`。
- Markdown/frontmatter 读写、覆盖保护和脱敏在 [wiki_io.py](src/netsuite_llm_wiki_mcp/wiki_io.py)。生成页只能覆盖 `generated: true` 页面；人工页不能被静默覆盖。

### 写入与维护流水线

- CodeGraph 摄入在 [wiki_ingest.py](src/netsuite_llm_wiki_mcp/wiki_ingest.py)：`CodeGraphClient` 读取 `status/files/context/impact` → 写 `raw/sources/codegraph/<project>/<source_name>/` snapshot → 写 `wiki/sources/` source summary 和 `wiki/projects/<project>/code/` code facts → refresh index/overview/log → 写 `.llm-wiki/ingest-cache/`。
- LLM 分阶段摄入同在 [wiki_ingest.py](src/netsuite_llm_wiki_mcp/wiki_ingest.py)：`prepare_analysis` 复制/脱敏原始文本并返回分析 prompt；`prepare_generation` 结合 purpose/schema/index 返回生成 prompt；`apply_generation` 校验路径并写 generated pages。
- 人工笔记写入在 [note_writer.py](src/netsuite_llm_wiki_mcp/note_writer.py)：`decision`/`troubleshooting`/`requirement` 写入项目目录，`knowledge` 写入 `wiki/concepts/<domain>/` 且不接受 `project`。MCP 入口为 `wiki_write_note` 工具。
- 写入后维护集中在 [wiki_index.py](src/netsuite_llm_wiki_mcp/wiki_index.py)、[wiki_overview.py](src/netsuite_llm_wiki_mcp/wiki_overview.py)、[wiki_log.py](src/netsuite_llm_wiki_mcp/wiki_log.py)。会产生或变更页面的工具通常要刷新 index/overview 并 append log。

### 查询与图谱能力

[wiki_query.py](src/netsuite_llm_wiki_mcp/wiki_query.py) 不使用向量库；主路径是关键词/CJK bigram 命中 → wikilink、shared source、common neighbor、same type 图扩展 → token budget 裁剪 → numbered citation context pack。`enable_vector` 目前只返回未配置警告，不应重新引入 Chroma、embedding 或 `.rag-index` 主路径。

维护工具按阶段拆分：

- [wiki_lint.py](src/netsuite_llm_wiki_mcp/wiki_lint.py)：结构、frontmatter、source traceability、broken wikilinks、orphan pages、cache manifest。
- [wiki_enrich.py](src/netsuite_llm_wiki_mcp/wiki_enrich.py)：prepare/apply 两阶段 wikilink 富化。
- [wiki_dedup.py](src/netsuite_llm_wiki_mcp/wiki_dedup.py)：detect/confirm/merge 三阶段重复页合并。
- [page_merge.py](src/netsuite_llm_wiki_mcp/page_merge.py)：generated 页面合并，锁定字段保护 + 数组字段 union + 可选 body merge。
- [wiki_insights.py](src/netsuite_llm_wiki_mcp/wiki_insights.py) + [louvain.py](src/netsuite_llm_wiki_mcp/louvain.py)：图谱洞察、社区、桥接页、孤立页。
- [wiki_delete.py](src/netsuite_llm_wiki_mcp/wiki_delete.py)：source 删除及派生页/交叉引用/cache 级联清理。
- [wiki_research.py](src/netsuite_llm_wiki_mcp/wiki_research.py)：prepare/apply 研究综合，写入 `wiki/queries/`。
- [wiki_synthesis.py](src/netsuite_llm_wiki_mcp/wiki_synthesis.py)：prepare/apply 持久化有价值的查询答案或跨页分析，写入 `wiki/synthesis/`。
- [wiki_batch.py](src/netsuite_llm_wiki_mcp/wiki_batch.py)：持久化 ingest 队列 `.llm-wiki/ingest-queue.json`。

## 必守约定

- 代码事实首版来自 CodeGraph；不要重新引入本项目代码扫描 + embedding 的 RAG 主路径。
- 旧 RAG 工具和旧人工笔记入口 `save_obsidian_note` 不应注册；人工笔记公开入口统一为 `wiki_write_note`。
- 不再保存 `script` / `object` 人工事实页；对象、部署、字段和脚本参数折叠到 CodeGraph 派生页或 source 摘要。
- `knowledge` 写入 `wiki/concepts/<domain>/`，禁止 `project`。
- 项目目录固定为 `wiki/projects/<project>/{index.md,code/,decisions/,troubleshooting/,requirements/}`。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。
- 不要在代码、测试或文档中硬编码个人 Vault 路径、API key、token、邮箱、手机号等敏感信息；脱敏逻辑在 [redaction.py](src/netsuite_llm_wiki_mcp/redaction.py)。

## 测试定位

- CLI/runtime/config/storage：`tests/test_cli.py`、`tests/test_runtime_config.py`、`tests/test_readme_global_mcp_docs.py`
- Wiki primitives（paths/io/index/overview/log）：`tests/test_wiki_paths.py`、`tests/test_wiki_io.py`、`tests/test_wiki_index.py`、`tests/test_wiki_overview.py`、`tests/test_wiki_log.py`
- MCP 工具注册和调用：`tests/test_server_tools.py`
- 人工 note 写入：`tests/test_save_obsidian_note.py`
- CodeGraph client / 摄入：`tests/test_codegraph_client.py`、`tests/test_wiki_ingest_codegraph.py`
- 查询和上下文预算：`tests/test_wiki_query.py`、`tests/test_context_budget.py`
- 维护工具：`tests/test_wiki_lint.py`、`tests/test_wiki_enrich.py`、`tests/test_page_merge.py`、`tests/test_wiki_dedup.py`、`tests/test_wiki_insights.py`、`tests/test_louvain.py`、`tests/test_wiki_delete.py`、`tests/test_wiki_research.py`、`tests/test_wiki_synthesis.py`、`tests/test_wiki_batch.py`

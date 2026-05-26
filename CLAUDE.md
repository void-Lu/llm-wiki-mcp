# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本文件给 AI 编码代理提供项目级工作约定。安装、部署和 MCP 使用细节优先查看 [README.md](README.md)，不要在这里重复维护。

## 快速命令

- 安装开发依赖：`python -m pip install -e ".[dev]"`
- 运行全部测试：`pytest`
- 运行单个测试文件：`pytest tests/test_wiki_query.py`
- 运行单个测试函数：`pytest tests/test_wiki_query.py::test_function_name -v`
- 启动 MCP server：`netsuite-rag-mcp-server` 或 `python -m netsuite_rag_mcp.server`
- CLI 初始化 vault：`netsuite-rag-mcp init --vault <name> --root <path> --default`
- CLI 查看状态：`netsuite-rag-mcp status`

## 架构

Python 3.11+，`src/` 布局，依赖仅 `mcp` + `PyYAML`。通过 FastMCP 暴露工具，所有 MCP 工具定义在 `server.py` 中用 `@mcp.tool()` 注册。

### 层次结构

```
server.py              ← MCP 工具入口（FastMCP @mcp.tool 注册）
├── wiki_ingest.py     ← CodeGraph/LLM 分阶段摄入
├── wiki_query.py      ← 无向量关键词 + wikilink 图查询
├── wiki_lint.py       ← 结构健康检查
├── wiki_enrich.py     ← 两阶段 wikilink 自动富化
├── wiki_dedup.py      ← 重复实体检测与合并（三阶段）
├── wiki_insights.py   ← 知识图谱洞察（孤立页、桥接页、意外连接）
├── wiki_delete.py     ← Source 删除级联清理
├── wiki_research.py   ← 深度研究综合（搜索结果 → wiki 页面）
├── wiki_batch.py      ← 持久化 ingest 队列
├── page_merge.py      ← 页面合并（frontmatter union + body merge）
├── context_budget.py  ← 上下文预算分配器
├── note_writer.py     ← 人工笔记写入
├── wiki_io.py         ← Markdown + YAML frontmatter 读写、覆盖保护、脱敏
├── wiki_paths.py      ← 外部 Wiki 目录结构与路径安全校验
├── wiki_index.py      ← wiki/index.md 与项目 index 维护
├── wiki_overview.py   ← wiki/overview.md 确定性汇总
├── wiki_log.py        ← wiki/log.md append-only 操作记录
├── codegraph_client.py ← CodeGraph CLI 只读封装（subprocess）
├── runtime_config.py   ← vault 解析：参数 > 环境变量 > global config.yaml
├── platform_paths.py   ← 跨平台 config/data 目录
├── redaction.py        ← 敏感信息脱敏
└── cli.py              ← CLI entry point（init / status / server）
```

### 运行时配置解析优先级

1. 函数参数 `vault_root`
2. 环境变量 `NETSUITE_RAG_VAULT_ROOT`
3. 全局 config.yaml 中的 `default_vault`

测试通过 `conftest.py` 的 `isolated_runtime_dirs` fixture 自动隔离环境变量和临时目录。

## 核心数据流

**CodeGraph 摄入**：CodeGraph CLI → `raw/sources/codegraph/<project>/<source_name>/` snapshot → `wiki/sources/` source 摘要 → `wiki/projects/<project>/code/` 代码事实页 → index/overview/log 更新。

**LLM 分阶段摄入**（`wiki_ingest_llm`）：`prepare_analysis` → 返回分析 prompt → `prepare_generation` → 返回生成 prompt → `apply_generation` → 写入 Wiki 页面。缓存在 `.llm-wiki/ingest-cache/`。

**查询**（`wiki_query`）：关键词/CJK bigram 命中 → 可选 vector 阶段（默认关闭）→ `[[wikilink]]` + shared source + common neighbor 图扩展 → 按 token 预算裁剪输出 context pack。

**页面合并**（`wiki_page_merge`）：re-ingest 时对已存在的 generated 页面做 frontmatter union（sources/tags/related）+ locked fields 保护（type/title/created）+ 可选 LLM body merge。

**Wikilink 富化**（`wiki_enrich`）：prepare 阶段返回 prompt → LLM 返回 `{term, target}` JSON → apply 阶段做精确字符串替换插入 `[[]]`。

**重复检测**（`wiki_dedup`）：detect 阶段扫描页面返回 prompt → LLM 返回重复组 → confirm 阶段校验 → merge 阶段重写交叉引用并删除冗余页。

**深度研究**（`wiki_research`）：prepare 阶段接收搜索结果返回综合 prompt → apply 阶段写入 `wiki/queries/`。

**批量摄入**（`wiki_ingest_batch`）：持久化队列 `.llm-wiki/ingest-queue.json`，支持 enqueue/next/complete/fail/retry/cancel。

## 测试对应关系

- Wiki primitives（paths/io/index/overview/log）→ `tests/test_wiki_*.py`
- MCP 工具注册和调用 → `tests/test_server_tools.py`
- 人工 note 写入 → `tests/test_save_obsidian_note.py`
- CodeGraph 摄入 → `tests/test_wiki_ingest_codegraph.py`
- 查询 → `tests/test_wiki_query.py`
- Lint → `tests/test_wiki_lint.py`
- Wikilink 富化 → `tests/test_wiki_enrich.py`
- 页面合并 → `tests/test_page_merge.py`
- 重复检测 → `tests/test_wiki_dedup.py`
- 图谱洞察 → `tests/test_wiki_insights.py`
- Source 删除 → `tests/test_wiki_delete.py`
- 深度研究 → `tests/test_wiki_research.py`
- 批量摄入队列 → `tests/test_wiki_batch.py`
- 上下文预算 → `tests/test_context_budget.py`

## 必守约定

- 外部 Obsidian Wiki root 结构固定为：`purpose.md`、`schema.md`、`raw/sources/`、`raw/assets/`、`wiki/index.md`、`wiki/log.md`、`wiki/overview.md`、`wiki/projects/`、`wiki/concepts/`、`wiki/sources/`、`wiki/queries/`、`wiki/synthesis/`、`wiki/comparisons/`、`.obsidian/`、`.llm-wiki/`。
- 项目目录固定为 `wiki/projects/<project>/{index.md,code/,decisions/,troubleshooting/,requirements/}`。
- 代码事实首版来自 CodeGraph；不要重新引入本项目代码扫描 + embedding 的 RAG 主路径。
- 不再保存 `script` / `object` 人工事实页；对象、部署、字段和脚本参数折叠到 CodeGraph 派生页或 source 摘要。
- `knowledge` 写入 `wiki/concepts/<domain>/`，并禁止 `project`。
- 生成页只能覆盖 `generated: true` 页面；人工页不能被静默覆盖。
- 旧 RAG 工具仅保留 deprecated 响应，不应调用 Chroma、indexer 或 embedding。
- 不要在代码、测试或文档中硬编码个人 Vault 路径、API key、token、邮箱、手机号等敏感信息；脱敏逻辑在 [src/netsuite_rag_mcp/redaction.py](src/netsuite_rag_mcp/redaction.py)。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。

## 开发注意事项

- 本项目是 Python 3.11+，源码在 `src/` 布局下；依赖和 entry points 见 [pyproject.toml](pyproject.toml)。
- 新增行为优先补对应单元测试。
- 完成前运行 `pytest`。

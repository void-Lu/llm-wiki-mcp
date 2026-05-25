# AGENTS.md

本文件给 AI 编码代理提供项目级工作约定。安装、部署和 MCP 使用细节优先查看 [README.md](README.md)，不要在这里重复维护。

## 快速命令

- 安装开发依赖：`python -m pip install -e ".[dev]"`
- 运行测试：`pytest`
- 启动 MCP server：`netsuite-rag-mcp-server` 或 `python -m netsuite_rag_mcp.server`
- 初始化外部 Wiki：调用 MCP 工具 `wiki_init`
- 摄入 CodeGraph source：调用 MCP 工具 `wiki_ingest(source_type="codegraph")`
- 查询 Wiki：调用 MCP 工具 `wiki_query`
- 检查 Wiki：调用 MCP 工具 `wiki_lint`

## 项目地图

- [src/netsuite_rag_mcp/server.py](src/netsuite_rag_mcp/server.py)：MCP 工具入口。
- [src/netsuite_rag_mcp/wiki_paths.py](src/netsuite_rag_mcp/wiki_paths.py)：外部 Obsidian LLM Wiki 目录结构与路径安全。
- [src/netsuite_rag_mcp/wiki_io.py](src/netsuite_rag_mcp/wiki_io.py)：Markdown + YAML frontmatter 读写、覆盖保护、脱敏。
- [src/netsuite_rag_mcp/wiki_log.py](src/netsuite_rag_mcp/wiki_log.py)：`wiki/log.md` append-only 操作记录。
- [src/netsuite_rag_mcp/wiki_index.py](src/netsuite_rag_mcp/wiki_index.py)：`wiki/index.md` 与项目 index 维护。
- [src/netsuite_rag_mcp/wiki_overview.py](src/netsuite_rag_mcp/wiki_overview.py)：`wiki/overview.md` 确定性汇总。
- [src/netsuite_rag_mcp/codegraph_client.py](src/netsuite_rag_mcp/codegraph_client.py)：CodeGraph CLI 只读封装。
- [src/netsuite_rag_mcp/wiki_ingest.py](src/netsuite_rag_mcp/wiki_ingest.py)：CodeGraph source snapshot 和 Wiki 页面生成。
- [src/netsuite_rag_mcp/wiki_query.py](src/netsuite_rag_mcp/wiki_query.py)：无向量 Wiki 查询。
- [src/netsuite_rag_mcp/wiki_lint.py](src/netsuite_rag_mcp/wiki_lint.py)：Wiki 健康检查。
- [src/netsuite_rag_mcp/note_writer.py](src/netsuite_rag_mcp/note_writer.py)：人工 note 写入。
- [tests/](tests/)：pytest 测试套件；新增行为优先补对应单元测试。

## 核心数据流

CodeGraph CLI → `raw/sources/codegraph/<project>/<source_name>/` snapshot → `wiki/sources/` source 摘要 → `wiki/projects/<project>/code/` 代码事实页 → `wiki/index.md` / `wiki/projects/<project>/index.md` / `wiki/overview.md` / `wiki/log.md`。

`wiki_query` 只读取持久 Markdown Wiki：先读 index，再做关键词匹配和 `[[wikilink]]` 一跳扩展；不做 embedding 或 Chroma 检索。

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
- 修改 Wiki primitives 时优先运行 `tests/test_wiki_*.py`。
- 修改 MCP 工具面时运行 [tests/test_server_tools.py](tests/test_server_tools.py)。
- 修改人工 note 写入时运行 [tests/test_save_obsidian_note.py](tests/test_save_obsidian_note.py)。
- 完成前运行 `pytest`。

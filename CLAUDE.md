# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

- 安装开发依赖：`python -m pip install -e ".[dev]"`
- 运行全部测试：`pytest`
- 运行单个测试文件：`pytest tests/test_indexer_multi_source.py`
- 运行单个测试用例：`pytest tests/test_indexer_multi_source.py::test_name`
- 初始化 Vault 配置：`netsuite-rag-mcp init --vault <name> --root <absolute-vault-path> --default`
- 诊断运行时配置：`netsuite-rag-mcp status`
- 启动 MCP server：`netsuite-rag-mcp-server` 或 `python -m netsuite_rag_mcp.server`
- 预下载 embedding 模型：`netsuite-rag-mcp-preload-model`

`pyproject.toml` 当前只配置了 pytest，没有专用 lint、format 或 build 脚本。

## 高层架构

这是一个 Python 3.11+、`src/` 布局的本地 MCP/RAG 服务，用 ChromaDB 和本地 `BAAI/bge-m3` embedding 为 VS Code Copilot 提供 NetSuite Obsidian 笔记与 SuiteCloud 代码的双源检索。

核心数据流：`<Vault>/rag/sources.yaml` → `load_config()` 解析 v2 多数据源配置 → `indexer` 按 source 收集文件并路由到 parser/chunker → chunk 写入 `source_name`、`source_kind`、hash、git 等元数据 → `ChromaVectorStore` upsert → `retriever` 做语义检索、metadata 过滤、问题路由与冲突检测 → `server.py` 暴露 MCP 工具返回结构化结果。

主要模块边界：

- [src/netsuite_rag_mcp/server.py](src/netsuite_rag_mcp/server.py)：FastMCP 工具层，负责解析运行时 Vault、组装过滤条件，并调用索引、检索、保存笔记和 Wiki 生成逻辑。
- [src/netsuite_rag_mcp/cli.py](src/netsuite_rag_mcp/cli.py)、[src/netsuite_rag_mcp/runtime_config.py](src/netsuite_rag_mcp/runtime_config.py)、[src/netsuite_rag_mcp/platform_paths.py](src/netsuite_rag_mcp/platform_paths.py)：用户级配置、Vault 解析、运行时存储路径和诊断命令。
- [src/netsuite_rag_mcp/config.py](src/netsuite_rag_mcp/config.py)、[src/netsuite_rag_mcp/models.py](src/netsuite_rag_mcp/models.py)：`rag/sources.yaml` 加载、v1 到 v2 兼容迁移、数据模型。
- [src/netsuite_rag_mcp/indexer.py](src/netsuite_rag_mcp/indexer.py)、[src/netsuite_rag_mcp/manifest.py](src/netsuite_rag_mcp/manifest.py)：多 source 索引、mtime/size/hash 增量判断、删除检测和 manifest v2 维护。
- [src/netsuite_rag_mcp/parser.py](src/netsuite_rag_mcp/parser.py)、[src/netsuite_rag_mcp/parser_xml_json.py](src/netsuite_rag_mcp/parser_xml_json.py)、[src/netsuite_rag_mcp/chunker.py](src/netsuite_rag_mcp/chunker.py)、[src/netsuite_rag_mcp/chunker_xml_json.py](src/netsuite_rag_mcp/chunker_xml_json.py)：Markdown frontmatter/H2、SuiteScript、XML、JSON 的解析与分块。
- [src/netsuite_rag_mcp/vector_store.py](src/netsuite_rag_mcp/vector_store.py)、[src/netsuite_rag_mcp/retriever.py](src/netsuite_rag_mcp/retriever.py)、[src/netsuite_rag_mcp/policy.py](src/netsuite_rag_mcp/policy.py)：Chroma 封装、搜索结果格式、source 过滤、RAG 路由、冲突检测和 answer policy。
- [src/netsuite_rag_mcp/note_writer.py](src/netsuite_rag_mcp/note_writer.py)、[templates/](templates/)：Obsidian 笔记保存与模板字段约束。
- [src/netsuite_rag_mcp/wiki_generator.py](src/netsuite_rag_mcp/wiki_generator.py)：从 SuiteCloud code source 生成 `projects/<project>/wiki/` 代码事实 Wiki。

## 项目约定

- `sources.yaml` 是 Vault 本地文件，固定在 `<Vault>/rag/sources.yaml`；不要把仓库根目录 `rag/sources.yaml` 或 workspace `.vscode/mcp.json` 加入仓库。
- v2 source 字段名是 `source_name`，不是 `name`；`include` 是 source root 下的相对目录列表，不是任意 glob。
- 默认 collection 名保持 `netsuite_knowledge`，README、indexer、retriever 和测试要一致。
- Manifest v2 key 格式为 `{source_name}:{source_kind}:{relative_path}`；source-scoped full reindex 只能清理目标 source，不能 reset 共享 collection。
- source 过滤依赖 chunk metadata 中实际写入 `source_name` 和 `source_kind`；只设置在 document/dataclass 顶层不会被 Chroma `where` 命中。
- Runtime 路径默认是 Vault-local：`<Vault>/.rag-index/chroma/`、`<Vault>/.rag-index/index-manifest.json`、`<Vault>/.models/`。
- 保持库函数 `load_config()` 对测试和默认值的向后兼容；MCP/server 运行时应显式解析 `RuntimeConfig`，并要求 `sources.yaml` 存在。
- 测试通过 [tests/conftest.py](tests/conftest.py) 设置 `NETSUITE_RAG_CONFIG_DIR` 和 `NETSUITE_RAG_USER_DATA_DIR` 隔离运行时目录；不要让测试写入真实用户配置或 Vault。
- 保存笔记路由：knowledge 笔记要求 `domain` 且禁止 `project`；script 笔记要求 `project` + `script_type`；模板字段使用 `related_objects` / `related_scripts`。
- 代码事实与笔记事实冲突时，实现细节以 code source 为准，业务背景以 note source 为准；策略集中在 [src/netsuite_rag_mcp/policy.py](src/netsuite_rag_mcp/policy.py)。
- 修改 parser、chunker、indexer、retriever 时，优先运行相关测试文件，再运行 `pytest`。
- 需要 embedding 或 Chroma 的测试应使用 fake/test embedder 模式，避免下载模型或依赖真实用户数据。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。

## 文档来源

- [README.md](README.md)：功能、部署、MCP 工具、`sources.yaml` 示例、模板说明。
- [pyproject.toml](pyproject.toml)：依赖、console scripts、pytest 配置。
- [templates/](templates/)：Obsidian 笔记模板。

当前仓库未发现 `.cursor/rules/`、`.cursorrules` 或 `.github/copilot-instructions.md`。

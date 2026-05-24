# AGENTS.md

本文件给 AI 编码代理提供项目级工作约定。安装、部署和 MCP 使用细节优先查看 [README.md](README.md)，不要在这里重复维护。

## 快速命令

- 安装开发依赖：`python -m pip install -e ".[dev]"`
- 运行测试：`pytest`
- 初始化 Vault 配置：`netsuite-rag-mcp init --vault <name> --root <absolute-vault-path> --default`
- 诊断运行时配置：`netsuite-rag-mcp status`
- 启动 MCP server：`netsuite-rag-mcp-server` 或 `python -m netsuite_rag_mcp.server`
- 预下载 embedding 模型：`netsuite-rag-mcp-preload-model`

## 项目地图

- [src/netsuite_rag_mcp/server.py](src/netsuite_rag_mcp/server.py)：MCP 工具入口。
- [src/netsuite_rag_mcp/cli.py](src/netsuite_rag_mcp/cli.py)：`init`、`status`、server CLI。
- [src/netsuite_rag_mcp/runtime_config.py](src/netsuite_rag_mcp/runtime_config.py)：Vault 根目录、用户级配置、运行时存储路径解析。
- [src/netsuite_rag_mcp/config.py](src/netsuite_rag_mcp/config.py)：`rag/sources.yaml` 加载，v1 到 v2 兼容迁移。
- [src/netsuite_rag_mcp/indexer.py](src/netsuite_rag_mcp/indexer.py)：多 source 文件收集、解析/分块路由、增量索引、删除检测。
- [src/netsuite_rag_mcp/manifest.py](src/netsuite_rag_mcp/manifest.py)：索引 manifest v2 与文件哈希。
- [src/netsuite_rag_mcp/vector_store.py](src/netsuite_rag_mcp/vector_store.py)：ChromaDB 封装与 embedder 协议。
- [src/netsuite_rag_mcp/retriever.py](src/netsuite_rag_mcp/retriever.py)：语义搜索、source 过滤、路由、冲突检测、引用格式。
- [src/netsuite_rag_mcp/note_writer.py](src/netsuite_rag_mcp/note_writer.py)：Obsidian 笔记保存与模板路由。
- [templates/](templates/)：笔记模板库；修改模板行为时同步相关测试。
- [tests/](tests/)：pytest 测试套件；新增行为优先补对应单元测试。

## 核心数据流

`<Vault>/rag/sources.yaml` → `load_config()` → `indexer` 收集 source 文件 → parser/chunker 路由 → 注入 `source_name`/`source_kind`/hash/git 元数据 → `ChromaVectorStore` upsert → `retriever` 检索、过滤、路由、冲突检测 → MCP 工具返回结构化结果。

## 必守约定

- `sources.yaml` 是 Vault 本地文件，位置固定为 `<Vault>/rag/sources.yaml`；不要把根目录 `rag/sources.yaml` 或 workspace `.vscode/mcp.json` 加入仓库。
- v2 source 字段名是 `source_name`，不是 `name`；`include` 是 source root 下的相对目录列表，不是任意 glob。
- 默认 collection 名保持 `netsuite_knowledge`，确保 README、indexer、retriever 和测试一致。
- Manifest v2 key 格式为 `{source_name}:{source_kind}:{relative_path}`；source-scoped full reindex 只能清理目标 source，不能 reset 共享 collection。
- source 过滤依赖 chunk metadata 中实际写入 `source_name` 和 `source_kind`；只设置在 document/dataclass 顶层不会被 Chroma `where` 命中。
- Runtime 路径默认是 Vault-local：`<Vault>/.rag-index/chroma/`、`<Vault>/.rag-index/index-manifest.json`、`<Vault>/.models/`。
- 保持库函数 `load_config()` 对测试/默认值的向后兼容；MCP/server 运行时应显式解析 `RuntimeConfig`，并要求 `sources.yaml` 存在。
- 测试会通过 [tests/conftest.py](tests/conftest.py) 设置 `NETSUITE_RAG_CONFIG_DIR` 和 `NETSUITE_RAG_USER_DATA_DIR` 隔离运行时目录；不要让测试写入真实用户配置或 Vault。
- 保存笔记路由：knowledge 笔记要求 `domain` 且禁止 `project`；script 笔记要求 `project` + `script_type`；模板字段使用 `related_objects` / `related_scripts`。
- 代码事实与笔记事实冲突时，实现细节以 code source 为准，业务背景以 note source 为准；相关策略集中在 [src/netsuite_rag_mcp/policy.py](src/netsuite_rag_mcp/policy.py)。

## 开发注意事项

- 本项目是 Python 3.11+，源码在 `src/` 布局下；依赖和 entry points 见 [pyproject.toml](pyproject.toml)。
- 修改 parser/chunker/indexer/retriever 时，优先运行相关测试文件，再运行 `pytest`。
- 需要 embedding 或 Chroma 的测试应使用 fake/test embedder 模式，避免下载模型或依赖真实用户数据。
- 不要在代码、测试或文档中硬编码个人 Vault 路径、API key、token、邮箱、手机号等敏感信息；脱敏逻辑在 [src/netsuite_rag_mcp/redaction.py](src/netsuite_rag_mcp/redaction.py)。
- Windows 路径相关逻辑要覆盖非法字符、冒号 ADS、保留设备名、控制字符、尾随点/空格等边界。

## 现有文档

- [README.md](README.md)：功能、部署、MCP 工具、`sources.yaml` 示例、模板说明。
- [pyproject.toml](pyproject.toml)：依赖、console scripts、pytest 配置。
- [templates/](templates/)：Obsidian 笔记模板。

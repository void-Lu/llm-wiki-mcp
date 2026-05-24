# NetSuite RAG MCP

> 本地部署的 MCP 服务器，为 VS Code Copilot 提供 NetSuite Obsidian 笔记和代码仓库的双源语义检索与 RAG 问答能力。

## ✨ 特性

- 🔍 **双源语义搜索** — 同时索引 Obsidian 笔记（note）和 NetSuite 代码仓库（code），支持按 `source_kind` 过滤
- 🧠 **智能路由** — 自动识别问题类型（业务原因/实现细节/混合），路由到最优数据源
- ⚡ **增量索引** — 基于 mtime + size + SHA-256 哈希的增量检测，仅重建变更文件
- 🔗 **代码感知** — 解析 SuiteScript 的 `@NScriptType`、`define()` 依赖、入口函数和函数边界
- 📊 **冲突检测** — 当笔记与代码事实冲突时自动识别，实现事实以代码为准
- 🏠 **完全本地** — ChromaDB + BAAI/bge-m3 Embedding 均运行在本地，无需配置 LLM API Key
- 🔒 **安全脱敏** — 自动检测并脱敏手机号、邮箱、API Key 等敏感信息
- 📋 **元数据过滤** — 支持按项目、脚本类型、关联对象、关联脚本、来源类型等维度过滤
- 🏷 **增强引用** — 引用格式包含 `source_kind`、函数名、行号、`git_commit` 等定位信息
- 📚 **代码事实 Wiki** — 可从 SuiteCloud 代码仓库生成 `projects/<project>/wiki/` 下的 Obsidian Wiki 页面

## 🛠 MCP 工具一览

| 工具 | 功能 |
| --- | --- |
| `index_vault` | 全量/增量索引 Vault（向后兼容旧命令） |
| `index_sources` | 按 `source_name` 或 `source_kind` 选择性索引指定数据源 |
| `search_netsuite_knowledge` | 语义搜索 + 元数据过滤，返回 chunk（带引用） |
| `ask_netsuite_rag` | 搜索 → 路由 → 冲突检测 → 组装上下文 → 返回结构化答案 |
| `get_index_status` | 返回索引状态：每个数据源的文件数、最后索引时间、git 信息 |
| `save_obsidian_note` | 将结构化 Obsidian 笔记保存到 Vault，并可选触发增量索引 |
| `generate_suitecloud_wiki` | 从 SuiteCloud code source 生成 `projects/<project>/wiki/` 下的代码事实 Wiki，并可选增量索引生成页 |

## 📦 快速部署

### 前置要求

- **Python 3.11+**（推荐 3.11 或 3.12）
- **Git**
- **VS Code** + Copilot 扩展

### 步骤 1：克隆并安装到 MCP 使用的 Python

```powershell
# 克隆仓库
git clone https://github.com/void-Lu/netsuite-rag-mcp.git
cd netsuite-rag-mcp

# 安装到当前 Python 环境
python -m pip install -e ".[dev]"
```

> 关键点是：VS Code user-level MCP config 中启动 server 的 Python，必须已经安装了 `netsuite-rag-mcp`。如果你使用虚拟环境，请先激活虚拟环境后再安装，并确保 VS Code MCP 启动时能找到同一个环境。

虚拟环境适合本仓库开发和运行测试；全局 MCP 配置不要指向工作区变量或项目 `.venv`，也不要依赖只安装在项目 `.venv` 里的包。

安装后会暴露 3 个控制台命令：

- `netsuite-rag-mcp` — 初始化、状态诊断和默认 MCP 启动入口。
- `netsuite-rag-mcp-server` — 显式启动 MCP stdio server。
- `netsuite-rag-mcp-preload-model` — 预下载 embedding 模型到用户本地缓存。

### 步骤 2：配置 Vault 数据源

在 Obsidian Vault 根目录内编辑 `rag/sources.yaml`，支持 v2 多数据源配置。源码仓库不再保留这个文件；它是每个 Vault 的本地数据源声明。

```yaml
schema_version: 2
workspace_root: .

index:
  embedding_model: BAAI/bge-m3
  collections:
    default: netsuite_knowledge

sources:
  - source_name: obsidian
    source_kind: note
    root: .
    include:
      - projects
      - knowledge
    exclude:
      - .git
      - .obsidian
      - .superpowers
      - .rag-index
    file_types:
      - md
    parser: markdown_frontmatter_h2
    collection: netsuite_knowledge
    authority: curated_note_source

  # 取消注释以添加代码仓库数据源
  # - source_name: netsuite_repo
  #   source_kind: code
  #   root: ../netsuite-repos
  #   include:
  #     - suiteapp-order-sync
  #     - netsuite-customizations
  #   exclude:
  #     - .git
  #     - node_modules
  #     - dist
  #   file_types:
  #     - js
  #     - ts
  #     - xml
  #     - json
  #   parser: suitescript_code_and_config
  #   collection: netsuite_knowledge
  #   authority: implementation_source_of_truth
  #   library_exclude_patterns:
  #     - src/FileCabinet/SuiteScripts/tools/crypto-js.js
  #     - src/FileCabinet/SuiteScripts/tools/moment.js
  #   utility_allowlist:
  #     - src/FileCabinet/SuiteScripts/tools/common_api.js
```

### 步骤 3：初始化全局 MCP 配置

### 全局 MCP 配置摘要

- Vault 只保留人工维护的笔记和 `rag/sources.yaml`。
- 生成状态（`.rag-index/`、`.models/`）位于 Vault 内，即 Vault 本地布局。
- 使用 VS Code 用户级 MCP 配置，不使用工作区 `.vscode/mcp.json`。
- 全局 `mcp.json` 不硬编码 Vault 路径。
- 源码仓库不提供根目录 `rag/sources.yaml`；请在已配置的 Vault 下创建或编辑该文件。

先在本机运行一次初始化命令，把 Obsidian Vault 的绝对路径写入用户级配置文件：

```powershell
netsuite-rag-mcp init --vault homework --root "D:\Obsidian Vault\homework" --default
netsuite-rag-mcp status

# 可选：初始化 vault 配置后，预下载 BGE-M3 embedding 模型到用户本地模型缓存
netsuite-rag-mcp-preload-model
```

`netsuite-rag-mcp status` 会打印当前解析到的 vault、`rag/sources.yaml` 是否存在，以及 Chroma、manifest、model cache 的路径。

默认存储布局为 `vault-local`，生成状态位于 Vault 根目录内：

```text
<Vault Root>/
  .rag-index/
    chroma/                    # ChromaDB 向量库
    index-manifest.json        # 索引清单
  .models/                     # embedding 模型缓存
```

其中：

- Chroma 向量库：`<vault>/.rag-index/chroma/`
- 索引 manifest：`<vault>/.rag-index/index-manifest.json`
- embedding 模型缓存：`<vault>/.models/`

### 步骤 4：配置 VS Code 用户级 MCP

使用 VS Code user-level MCP config 启动已安装的 server。这个全局 `mcp.json` 只负责启动服务，Vault 路径来自上一步写入的用户级配置：

```json
{
  "servers": {
    "netsuite-obsidian-rag": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "netsuite_rag_mcp.server"]
    }
  }
}
```

也可以在终端或 MCP 客户端中使用 `netsuite-rag-mcp-server` 作为显式 server 入口；上面的 Python module 写法更容易确认 server 来自已安装的 `netsuite-rag-mcp` 包。

> 💡 **开发者提示**：本仓库不提交 workspace `.vscode/mcp.json` 或根目录 `rag/sources.yaml`。如需本地开发调试，可在自己的工作区创建未跟踪的 `.vscode/mcp.json`；正式使用请配置上述 VS Code user-level MCP config。

### 步骤 5：重新加载 VS Code

按 `Ctrl+Shift+P` → 输入 `Developer: Reload Window` → 回车。

### 步骤 6：建立索引

在 Copilot Chat 中输入：

```text
请调用 index_vault，mode 设为 "full"
```

如果未提前运行 `netsuite-rag-mcp-preload-model`，首次建立索引会自动下载 BGE-M3 模型，请耐心等待。后续可用 `mode: "incremental"` 仅更新变更文件。

也可以选择性索引特定数据源：

```text
请调用 index_sources，source_kind 设为 "code"
```

### 步骤 7：开始提问

```text
请调用 ask_netsuite_rag，question 设为 "这个 Restlet 的用途是什么？"
```

支持按来源过滤：

```text
请调用 ask_netsuite_rag，question 设为 "afterSubmit 的实现逻辑"，source_kind 设为 "code"
```

保存一条知识笔记到 `knowledge/<domain>/`，并自动写入新字段名的 frontmatter：

```text
请调用 save_obsidian_note，note_type 设为 "knowledge"，domain 设为 "suitescript-patterns"，title 设为 "RESTlet 提交流程经验"，content 设为 "## 适用场景\n..."
```

## 📚 SuiteCloud 代码事实 Wiki

当项目以 SuiteCloud/SuiteScript 代码为主时，可以调用 `generate_suitecloud_wiki` 从 `rag/sources.yaml` 中的 `source_kind: code` 数据源生成代码事实 Wiki：

```text
请调用 generate_suitecloud_wiki，project 设为 "huideng"，source_name 设为 "huideng"，auto_index 设为 true
```

生成内容位于 Vault：

```text
projects/<project>/wiki/
  index.md
  scripts/
  objects/
  flows/
  archive/scripts/
```

生成页的 frontmatter 会包含 `type: generated_wiki`、`generated: true`、`do_not_edit: true`、`source_kind: code`、`source_repo`、`source_path`、`archived: false` 等字段。生成页是可覆盖的代码事实页，不建议手工编辑；业务背景、决策原因和排坑过程仍应写入人工维护的 `requirements/`、`decisions/`、`troubleshooting/` 或 `knowledge/<domain>/`。

`tools/` 目录通常混有第三方库和项目工具模块。生成器默认排除常见第三方库：`crypto-js.js`、`moment.js`、`papaparse.js`、`ramda.min.js`；也可以用 `library_exclude_patterns` 显式排除更多文件，用 `utility_allowlist` 显式保留项目工具模块。

当源脚本文件被移除时，对应生成页不会直接删除，而会移动到 `projects/<project>/wiki/archive/scripts/`，并写入 `archived: true`、`archived_at`、`archived_reason: source_removed`、`former_source_path`。搜索默认排除已归档页；如需查历史内容，可在搜索或问答工具中设置 `include_archived: true`，也可用 `content_type: generated_wiki` 只检索生成 Wiki。

## 📝 Obsidian 笔记模板

项目提供 NetSuite 相关笔记模板，按类别存放在 `templates/` 子目录中，复制到你的 Vault 中使用。

### 脚本模板（`templates/scripts/`）

| 模板 | 用途 | 关键 Frontmatter 字段 |
| --- | --- | --- |
| `scripts/default-script-note.md` | SuiteScript 通用脚本笔记 | `script_type`, `script_id`, `deployment_id`, `related_objects`, `related_scripts` |
| `scripts/restlet-note.md` | RESTlet 脚本笔记 | `script_type: restlet`, 入口函数（get/post/put/delete） |
| `scripts/suitelet-note.md` | Suitelet 脚本笔记 | `script_type: suitelet`, 入口函数（onRequest） |
| `scripts/userevent-note.md` | UserEvent 脚本笔记 | `script_type: userevent`, 入口函数（beforeLoad/beforeSubmit/afterSubmit） |
| `scripts/mapreduce-note.md` | Map/Reduce 脚本笔记 | `script_type: mapreduce`, 入口函数（getInputData/map/reduce/summarize） |
| `scripts/clientscript-note.md` | Client Script 笔记 | `script_type: clientscript`, 入口函数（pageInit/fieldChanged/saveRecord 等） |

### 对象模板（`templates/objects/`）

| 模板 | 用途 | 关键 Frontmatter 字段 |
| --- | --- | --- |
| `objects/default-object-note.md` | NetSuite Object 通用笔记 | `object_type`, `object_id`, `related_objects`, `related_scripts` |
| `objects/savedsearch-note.md` | 保存的搜索 | `object_type: savedsearch`, 搜索条件、结果列 |
| `objects/customlist-note.md` | 自定义列表 | `object_type: customlist`, 列表项 |
| `objects/customrecord-note.md` | 自定义记录 | `object_type: customrecord`, 关键字段 |
| `objects/workflow-note.md` | 工作流 | `object_type: workflow`, 状态流转 |
| `objects/role-note.md` | 角色 | `object_type: role`, 核心权限 |
| `objects/deployment-note.md` | 脚本部署 | `object_type: deployment`, 部署配置 |

### 其他模板

| 模板 | 用途 | 关键 Frontmatter 字段 |
| --- | --- | --- |
| `requirement-note.md` | 需求文档 | `zentao_urls`, `related_scripts`, `related_objects` |
| `troubleshooting-note.md` | 排坑记录 | `related_objects`, `related_scripts` |
| `decision-note.md` | 技术决策记录 | `decision_status`, `decision_date`, `related_scripts` |
| `knowledge-note.md` | 知识/经验笔记 | `topic`, `related_script_types`, `related_objects` |

知识/经验笔记按领域存放在 `knowledge/<domain>/`，例如 `knowledge/common-errors/`、`knowledge/integration-patterns/`、`knowledge/netsuite-object-playbooks/`、`knowledge/suitescript-patterns/`。

多数模板共享以下元数据过滤字段：

- `project` — 项目名称
- `status` — `active` / `inactive`（激活 / 停用）
- `tags` — 标签列表

## 🧪 运行测试

```powershell
# 激活虚拟环境后
pytest
```

## 📂 项目结构

```text
.
├── src/netsuite_rag_mcp/
│   ├── __init__.py
│   ├── server.py                          # FastMCP 服务器入口（7 个 MCP 工具）
│   ├── config.py                          # 配置加载（v1/v2 自动迁移）
│   ├── models.py                          # 数据模型（SourceConfig, RoutingResult 等）
│   ├── parser.py                          # Markdown/SuiteScript 解析器
│   ├── parser_xml_json.py                 # XML/JSON 配置解析器
│   ├── chunker.py                         # 文档分块器（H2 标题 + 函数级）
│   ├── chunker_xml_json.py                # XML/JSON 配置分块器
│   ├── metadata.py                       # 元数据编解码 + 过滤匹配
│   ├── redaction.py                       # 敏感信息脱敏
│   ├── policy.py                          # RAG Answer Policy
│   ├── vector_store.py                    # ChromaDB 向量存储封装
│   ├── indexer.py                         # 多源索引器（scan → parse → chunk → embed → upsert）
│   ├── manifest.py                        # 索引清单管理（v2 schema + SHA-256 哈希）
│   ├── git_utils.py                       # Git commit/dirty 提取
│   ├── retriever.py                       # 检索器 + 路由 + 冲突检测 + 问答上下文组装
│   ├── wiki_generator.py                  # SuiteCloud 代码事实 Wiki 生成器
│   ├── cli.py                             # init/status/server 全局配置 CLI
│   ├── platform_paths.py                  # 用户级 config/data 目录解析
│   ├── runtime_config.py                  # Vault 与用户本地存储解析
│   └── preload.py                         # BGE-M3 embedding 模型预下载入口
├── templates/                              # Obsidian 笔记模板
│   ├── scripts/                            # 脚本模板（按脚本类型）
│   │   ├── default-script-note.md          # 通用脚本模板
│   │   ├── restlet-note.md                 # RESTlet 脚本模板
│   │   ├── suitelet-note.md                # Suitelet 脚本模板
│   │   ├── userevent-note.md               # UserEvent 脚本模板
│   │   ├── mapreduce-note.md               # Map/Reduce 脚本模板
│   │   └── clientscript-note.md            # Client Script 模板
│   ├── objects/                            # 对象模板（按对象类型）
│   │   ├── default-object-note.md          # 通用对象模板
│   │   ├── savedsearch-note.md             # 保存的搜索模板
│   │   ├── customlist-note.md              # 自定义列表模板
│   │   ├── customrecord-note.md            # 自定义记录模板
│   │   ├── workflow-note.md                # 工作流模板
│   │   ├── role-note.md                    # 角色模板
│   │   └── deployment-note.md              # 脚本部署模板
│   ├── requirement-note.md                 # 需求文档模板
│   ├── troubleshooting-note.md             # 排坑记录模板
│   ├── decision-note.md                    # 技术决策模板
│   └── knowledge-note.md                   # 知识笔记模板
├── tests/                                  # 测试用例
├── pyproject.toml                          # 项目配置 + 依赖
├── .gitignore
└── README.md
```

Vault 数据源配置位于已初始化的 Obsidian Vault 内：`<Vault>/rag/sources.yaml`。本仓库不提交 `rag/`、`.vscode/`、`docs/plan/` 或 `docs/superpowers/` 等本地/计划导出目录。

## ❓ 常见问题

### Q: 首次运行 `index_vault` 很慢？

A: 首次运行时会自动下载 `BAAI/bge-m3` 模型。建议先运行 `netsuite-rag-mcp init ... --default` 写入 vault 配置，再运行 `netsuite-rag-mcp-preload-model`；模型会缓存到 Vault 内的 `.models/` 目录，后续运行直接使用本地缓存。

### Q: 需要配置 API Key 吗？

A: **不需要！** 本项目完全运行在本地。Embedding 由本地 `BAAI/bge-m3` 模型提供，最终答案由 VS Code Copilot 的云端模型生成，不需要额外配置 LLM API。

### Q: 如何更新索引？

A: 使用 `index_vault` 的 `incremental` 模式，仅重建有变更的文件：

```text
请调用 index_vault，mode 设为 "incremental"
```

### Q: 如何查看索引状态？

A: 使用 `get_index_status` 工具，可查看每个数据源的文件数、最后索引时间和 git 信息：

```text
请调用 get_index_status
```

### Q: 如何只索引代码仓库？

A: 使用 `index_sources` 工具，按 `source_kind` 过滤：

```text
请调用 index_sources，source_kind 设为 "code"
```

### Q: pip 安装依赖太慢？

A: 使用清华镜像源加速：

```powershell
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q: 如何重建全新索引？

A: 使用 `full` 模式，会清除旧索引并从头构建：

```text
请调用 index_vault，mode 设为 "full"
```

## 📄 许可证

MIT

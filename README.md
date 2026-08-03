# NetSuite LLM Wiki MCP

一个本地 MCP（Model Context Protocol）server，让 LLM 编码代理可以完整读写基于 Obsidian 的知识 Wiki。内容通过 MCP 工具摄入、查询、维护和归档；代码事实不再走 CodeGraph 摄入主路径，CodeGraph 只保留在 `wiki_status` 的只读可用性字段中。

默认不使用 embedding 或向量数据库；关键词、图检索和 `[[wikilinks]]` 始终可独立运行。需要语义召回时可显式启用本地 BGE-M3 索引，绝不自动下载模型或向外部服务发送 vault 内容。

## 安装

```bash
uv sync --extra dev
# 只有需要本地向量检索时才安装；不会下载任何模型
uv sync --extra dev --extra vector
```

本仓库提交了 `uv.lock`；开发时优先使用 `uv sync --extra dev` 创建/同步 `.venv`，并安装 `dev` 可选依赖。

### MCP SDK 2.x

运行依赖固定在 `mcp>=2,<3`。stdio 启动方式和现有 MCP 客户端配置保持不变；服务器握手版本由运行时 provenance 通过 `MCPServer(..., version=...)` 公开上报。

升级 SDK 后请重新生成由 `mcp dev` 或 `mcp install` 创建的客户端配置：这些命令会固定生成时的 SDK 版本。v2 的同步 tool handler 在线程中运行、工具结果在发送前强校验、streamable HTTP 的 lifespan 仅执行一次，URI 模板遵循 RFC 6570；WebSocket transport 与 Tasks API 已移除。本项目仅使用 stdio transport，不受已移除 transport 的影响。

## 运行

```bash
# 启动 MCP server
uv run netsuite-llm-wiki-mcp-server

# 或通过 CLI / module 启动
uv run netsuite-llm-wiki-mcp server
uv run python -m netsuite_llm_wiki_mcp.server
```

### CLI

```bash
uv run netsuite-llm-wiki-mcp init --vault <name> --root <path> --default
uv run netsuite-llm-wiki-mcp status
uv run netsuite-llm-wiki-mcp retrieval-eval --vault <path> --dataset <cases.jsonl> --output-dir <reports-dir>
uv run netsuite-llm-wiki-mcp retrieval-eval --vault <path> --dataset <cases.jsonl> --output-dir <reports-dir> --retrieval-mode hybrid --vector-model-path <local-bge-m3-path>
uv run netsuite-llm-wiki-mcp vector status --vault <path>
uv run netsuite-llm-wiki-mcp vector build --vault <path> --model-path <local-bge-m3-path>
uv run netsuite-llm-wiki-mcp vector update --vault <path> --model-path <local-bge-m3-path>
```

## 配置

server 按以下顺序解析 wiki 根目录（vault）：

1. 工具参数 `vault_root`
2. 环境变量 `NETSUITE_LLM_WIKI_VAULT_ROOT`
3. 全局配置 `config.yaml` → `default_vault`

**推荐方式**：将 vault 绝对路径存入系统环境变量 `NETSUITE_LLM_WIKI_VAULT_ROOT`，配置文件中只使用变量引用，避免硬编码绝对路径。

`config.yaml` 位于 `netsuite-llm-wiki-mcp` 的平台用户配置目录（可选，环境变量优先）：

- Windows: `%APPDATA%\\netsuite-llm-wiki-mcp\\config.yaml`
- macOS: `~/Library/Application Support/netsuite-llm-wiki-mcp/config.yaml`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/netsuite-llm-wiki-mcp/config.yaml`

也可以用 `netsuite-llm-wiki-mcp init --vault <name> --root <path> --default` 写入 `config.yaml`。

开发和测试时，可以用 `NETSUITE_LLM_WIKI_CONFIG_DIR` 和 `NETSUITE_LLM_WIKI_USER_DATA_DIR` 覆盖配置/数据目录。

### MCP 客户端配置

本 server 的安装目录和 vault 根路径都通过环境变量传递，避免在配置文件里硬编码绝对路径，也避免依赖 `${workspaceFolder}`（在别的工作区打开时会解析错）。先在系统（或用户）环境变量里设置一次：

| 变量                             | 含义                                    | 示例（Windows）                                         |
| -------------------------------- | --------------------------------------- | ------------------------------------------------------- |
| `NETSUITE_LLM_WIKI_MCP_DIR`    | 本仓库（MCP server 安装目录）的绝对路径 | `c:\Users\<you>\VSCodeProjects\netsuite-llm-wiki-mcp` |
| `NETSUITE_LLM_WIKI_VAULT_ROOT` | Obsidian wiki vault 的绝对路径          | `c:\Users\<you>\Documents\Obsidian Vault\codingwork`  |

设好后再用各客户端对应的变量引用语法取值。三家客户端的变量替换语法不同：

| 客户端                   | 配置文件                                                    | 变量语法       |
| ------------------------ | ----------------------------------------------------------- | -------------- |
| VS Code / GitHub Copilot | 项目根`.vscode/mcp.json`                                  | `${env:VAR}` |
| Claude Code              | 项目根`.mcp.json` 或 `~/.claude.json` 的 `mcpServers` | `${VAR}`     |
| Codex                    | `~/.codex/config.toml`                                    | `$VAR`       |

#### VS Code / GitHub Copilot（推荐）

在项目根目录创建 `.vscode/mcp.json`（[本仓库已提供](.vscode/mcp.json)）：

```json
{
  "servers": {
    "netsuite-wiki": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory",
        "${env:NETSUITE_LLM_WIKI_MCP_DIR}",
        "run",
        "netsuite-llm-wiki-mcp-server"
      ],
      "env": {
        "NETSUITE_LLM_WIKI_VAULT_ROOT": "${env:NETSUITE_LLM_WIKI_VAULT_ROOT}"
      }
    }
  }
}
```

#### Claude Code / Claude Desktop

添加到你的 MCP 客户端配置中（项目根 `.mcp.json`、`~/.claude.json` 或 `claude_desktop_config.json`）。Claude Code 用 `${VAR}` 语法从环境变量取值：

```json
{
  "mcpServers": {
    "netsuite-wiki": {
      "command": "uv",
      "args": [
        "--directory",
        "${NETSUITE_LLM_WIKI_MCP_DIR}",
        "run",
        "netsuite-llm-wiki-mcp-server"
      ],
      "env": {
        "NETSUITE_LLM_WIKI_VAULT_ROOT": "${NETSUITE_LLM_WIKI_VAULT_ROOT}"
      }
    }
  }
}
```

#### Codex

在 `~/.codex/config.toml` 里用 `$VAR` 语法从环境变量取值：

```toml
[mcp_servers.netsuite-wiki]
command = "uv"
args = ["--directory", "$NETSUITE_LLM_WIKI_MCP_DIR", "run", "netsuite-llm-wiki-mcp-server"]

[mcp_servers.netsuite-wiki.env]
NETSUITE_LLM_WIKI_VAULT_ROOT = "$NETSUITE_LLM_WIKI_VAULT_ROOT"
```

> 三种配置都只引用环境变量，不包含任何工作区相关路径或绝对路径，可以原样复制到任意工作区使用。

## 工具

默认 core profile 只注册以下 7 个业务工具。所有工具优先使用 `default_vault`，多库时传逻辑 `vault` 名；`vault_root`/`vaultRoot` 仅保留一个兼容发布周期，并会返回 `deprecated_vault_root` warning。

| 工具                | 说明                                                                                                                                            |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `wiki_status`     | 聚合逻辑 vault、检索配置、active/archive index、generation queue、版本与运行身份；不回显绝对路径、模型路径、凭据或脱敏规则正文。`detail=summary |
| `wiki_ingest`     | 摄入一个明确文件，写入 `raw/sources/` 并同步 raw/检索索引；不会生成 Wiki 页面或 capsule 任务。                                      |
| `wiki_write_note` | 仅创建人工知识页，已有目标不会被覆盖。                                                                                                          |
| `wiki_update`     | 对既有页面执行 `preview                                                                                                                         |
| `wiki_query`      | 只接受问题、`scope`、`project`、`filters`、`top_k` 与逻辑 vault；模型、预算、索引和隐私策略全部来自启动时配置快照。                     |
| `wiki_archive`    | 归档生命周期的 `plan                                                                                                                            |
| `wiki_restore`    | 不可变归档包的 `plan                                                                                                                            |

不再注册 `wiki_generation` worker 工具。init/config、vector build/rebuild、retrieval evaluation、archive admin 和 migration 只保留在 CLI/admin 边界。

`retrieval-eval` 使用版本化 JSONL 查询集和 manifest 只读评测公共查询契约，输出 JSON 与 Markdown 报告。当前唯一查询引擎是 V2，`--query-version` 仅接受 `v2`；评测只读取已构建的 passage/vector index，且关闭查询遥测。`--scope` 控制 V2 corpus。报告包含 Recall@10、MRR@10、nDCG@10、无答案误命中率、过滤器正确性、P95 延迟、context budget、语料指纹和运行 provenance；不会构建索引或写入 vault。CLI 默认对首个 case 单独测量 context budget；可用 `--context-budget-case-limit` 扩大样本，或以 `--no-context-budget` 显式跳过。

### 可选本地向量检索

向量检索默认关闭。唯一支持的 provider 是本地 `local_bge_m3`；模型目录在 `config set-retrieval` 写入用户级配置，加载时启用离线模式和 `local_files_only`，缺模型或未安装 `vector` extra 时只会结构化降级到关键词/图结果。为使 BGE-M3 的 CPU 全量建库可控，文档 embedding 默认上限为 256 tokens；该值是索引身份的一部分，改变后必须执行 full build。

先通过 `vector build` 显式构建索引；`wiki_query` 从不构建、更新索引或下载模型。索引保存于 vault 的 `.llm-wiki/vector-index/`，仅含相对路径、内容哈希、元数据和归一化向量，不保存正文。默认 `include_raw_sources=false`，这会同时约束建库、更新和查询。

配置本地模型后，普通查询不再传模型或索引参数：

```python
wiki_query(
    question="如何自动化应付账款处理？",
    vault="codingwork",
    scope="knowledge",
)
```

Query V2 默认返回 compact response：`results` 只含 path、heading、snippet 和 scores，正文只存在于一次性的 `context_pack.passages`。它按 scope 打开 active/history 或独立 archive store，先做 passage FTS/vector 召回，再以 RRF 和有界强-seed graph 扩展排序；退役的 `wiki/sources`、superseded 与 deprecated 页面不会进入 active 正文。非 chat raw source 会在维护/摄入阶段投影到独立的 `.llm-wiki/raw-retrieval.sqlite3` FTS：查询始终优先 Wiki，且仅在 Wiki 零结果时才回退该 raw FTS。回退只读取已建索引，不扫描 raw 文件、不会为 raw 召回加载模型，并在 `pipeline.fallback` 中标明 `wiki_zero_results`。

查询引擎固定为 V2；配置中的 `retrieval.query_version` 仅保留明确的 `v2` 值，旧 `v1` 配置会在启动解码时拒绝。若旧客户端只需要旧响应字段，可使用 `retrieval.context.response_mode=legacy`，它只适配已经完成的 V2 结果，不会切换检索引擎。

使用 `netsuite-llm-wiki-mcp config validate|show|set-retrieval|set-privacy|set-telemetry|set-archive` 管理配置。配置修改在重启 MCP runtime 后生效；普通 MCP 调用不能修改脱敏、保留期、archive/purge 或索引路径。

## Wiki 结构

```
vault_root/
├── purpose.md
├── schema.md
├── raw/
│   ├── assets/
│   └── sources/
│       ├── projects/
│       ├── chat/
│       ├── file/
│       └── references/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── overview.md
│   ├── concepts/<domain>/
│   ├── projects/
│   │   └── <project>/
│   │       ├── index.md
│   │       ├── specs/
│   │       ├── plans/
│   │       ├── architecture/
│   │       ├── troubleshooting/
│   │       └── researches/
│   ├── entities/
│   └── archives/
├── archives/
│   ├── log.md
│   ├── bundles/
│   └── .staging/
└── .llm-wiki/
    ├── ingest-cache/
    ├── ingest-queue.json
    ├── graph-index/
    └── relation-candidates/
```

## LLM Wiki 工作流

本项目遵循 LLM Wiki 模式：raw sources 保持为事实源 snapshot，而由 LLM 维护的 Markdown 页面会随着时间沉淀成可导航的 wiki。

推荐循环：

1. 用 CLI 注册 vault：`netsuite-llm-wiki-mcp init --vault <name> --root <path> --default`。首次写入时 `create_wiki_root` 会自动补齐 `purpose.md`、`schema.md`、`raw/sources/`、`wiki/` 与归档目录。
2. 摄入明确文件：`wiki_ingest(source_path=..., source_name=..., project=..., source_type="file")`。文件按字节复制到 `raw/sources/<type>/<project>/<source_name>/`，同时同步 raw/检索索引；正式 Wiki 页面由后续显式笔记或更新操作维护。
3. 用 `wiki_query` 查询已积累的知识，回答时引用 numbered context pack。
4. 通过 `wiki_write_note`，把人工整理的 spec、plan、troubleshooting、researches 或 knowledge note 写回 `wiki/projects/<project>/specs/`、`wiki/projects/<project>/plans/`、`wiki/projects/<project>/troubleshooting/`、`wiki/projects/<project>/researches/` 或 `wiki/concepts/`。
5. 用 `wiki_update(action="preview"|"apply")` 对既有页面做受控编辑；preview 返回 hash、plan_id、锁定字段和 diff，apply 在内容变化前校验这些不变量。
6. 用 `wiki_archive`/`wiki_restore` 管理归档生命周期；purge 只保留在 CLI/admin 边界。

对于大范围本地 Markdown 搜索，可以把这个 MCP server 与 qmd 等外部工具搭配使用，但 qmd/vector search 有意不作为默认依赖或主检索路径。

### Raw provenance 与退役 capsule

`wiki_ingest` 只负责 raw snapshot、hash 和索引投影，不调用 LLM，也不生成 `wiki/sources` 页面。`knowledge_compiler.py` 仅保留兼容边界：旧的 `source_capsule` / `chat_source_capsule` job 会被拒绝或 supersede，不再 claim/apply；活动 Wiki 只接受具体 raw 文件的 `sources` 与 `source_hashes`。一次性归档由 `scripts/archive_wiki_sources.py` 完成，不注册为 MCP 常态工具。

执行一次性归档时先预检，再显式提交：

```bash
python scripts/archive_wiki_sources.py --vault-root <vault-root>
python scripts/archive_wiki_sources.py --vault-root <vault-root> --apply
```

脚本会把整个退役 namespace 作为一个不可恢复 bundle 归档；raw 文件不移动、不删除。缺失或冲突的 raw 映射会在页面标记 `review_required`，但不会阻断归档，完整映射审计保存在 bundle 的 `source-remap.json` 中。

批量或重建立索引不是 MCP 工具职责：`retrieval-eval`、`vector build/update`、`index build/update`、archive admin 和 migration 都保留在 CLI 边界。

## 数据流

### 单文件摄入

```
wiki_ingest
    → raw/sources/<source_type>/<project>/<source_name>/<file>
    → raw/active RetrievalIndexStore 增量更新
    → 显式 wiki_write_note / wiki_update 维护 formal Wiki
```

`wiki_ingest` 只接受一个已存在文件，不接受目录或自动下载。raw snapshot 按字节复制，检索投影在复制后同步；若 raw 内容未变化，不会重复复制或刷新该 snapshot。

### 受控更新与归档

```
wiki_update(preview) → current_hash + plan_id + locked-field diff
wiki_update(apply)   → hash/plan 校验 + 锁定字段/来源检查
                     → 写页面 + 更新依赖 + refresh index + append log

wiki_archive(plan)   → 生成不可变 bundle 计划
wiki_archive(apply)  → 写入 archives/bundles + archive index
wiki_restore(plan)   → 从 bundle 生成恢复计划
wiki_restore(apply)  → 恢复页面 + 更新 active/archive index
```

### 查询流水线

```
关键词 / CJK bigrams，带标题/短语/稀有词加权 → 候选页面
  → 图扩展（wikilink、shared source、common neighbor、same type）
  → 上下文预算分配
  → 编号引用 context pack
```

## 开发

```bash
# 运行全部测试
uv run pytest

# 运行单个测试文件
uv run pytest tests/test_wiki_query.py

# 运行单个测试函数
uv run pytest tests/test_wiki_query.py::test_function_name -v
```

### 约定

- Python 3.11+，`src/` layout，最小依赖（`mcp` + `PyYAML`）
- 生成页只能覆盖 frontmatter 中带 `generated: true` 的页面
- 人工编写页面绝不静默覆盖
- 所有写入都限制在 vault root 内；`wiki/concepts/` 和 `wiki/projects/` 下的路径遵循固定结构
- 敏感数据（手机号、邮箱、token）写入前会被脱敏
- Windows 路径安全：非法字符、ADS 冒号、保留设备名、控制字符、尾随点/空格
- 文件写入统一 `encoding="utf-8"`（无 BOM）；读取可用 `utf-8-sig` 兼容 Obsidian BOM 文件，但写入绝不产生 BOM
- 不引入 Chroma、sentence-transformers 或 embedding 模型
- 不创建 `.rag-index/` 或 `.models/`

## 许可证

MIT

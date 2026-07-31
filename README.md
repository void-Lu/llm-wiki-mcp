# NetSuite LLM Wiki MCP

一个本地 MCP（Model Context Protocol）server，让 LLM 编码代理可以完整读写基于 Obsidian 的知识 Wiki。代码事实来自 CodeGraph；其他内容都通过 MCP 工具进行摄入、查询和维护。

默认不使用 embedding 或向量数据库；关键词、图检索和 `[[wikilinks]]` 始终可独立运行。需要语义召回时可显式启用本地 BGE-M3 索引，绝不自动下载模型或向外部服务发送 vault 内容。

## 安装

```bash
uv sync --extra dev
# 只有需要本地向量检索时才安装；不会下载任何模型
uv sync --extra dev --extra vector
```

本仓库提交了 `uv.lock`；开发时优先使用 `uv sync --extra dev` 创建/同步 `.venv`，并安装 `dev` 可选依赖。

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

| 变量 | 含义 | 示例（Windows） |
|------|------|-----------------|
| `NETSUITE_LLM_WIKI_MCP_DIR` | 本仓库（MCP server 安装目录）的绝对路径 | `c:\Users\<you>\VSCodeProjects\netsuite-llm-wiki-mcp` |
| `NETSUITE_LLM_WIKI_VAULT_ROOT` | Obsidian wiki vault 的绝对路径 | `c:\Users\<you>\Documents\Obsidian Vault\codingwork` |

设好后再用各客户端对应的变量引用语法取值。三家客户端的变量替换语法不同：

| 客户端 | 配置文件 | 变量语法 |
|--------|----------|----------|
| VS Code / GitHub Copilot | 项目根 `.vscode/mcp.json` | `${env:VAR}` |
| Claude Code | 项目根 `.mcp.json` 或 `~/.claude.json` 的 `mcpServers` | `${VAR}` |
| Codex | `~/.codex/config.toml` | `$VAR` |

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

| 工具 | 说明 |
|------|------|
| `wiki_status` | 聚合逻辑 vault、检索配置、active/archive index、generation queue、版本与运行身份；不回显绝对路径、模型路径、凭据或脱敏规则正文。`detail=summary|indexes|generation|archive` 只改变只读展示范围。 |
| `wiki_ingest` | 为一个明确 source 准备摄入；目录/batch/reconcile 由 CLI 或 worker 执行。 |
| `wiki_write_note` | 仅创建人工知识页，已有目标不会被覆盖。 |
| `wiki_update` | 对既有页面执行 `preview|apply` 受控更新。 |
| `wiki_query` | 只接受问题、`scope`、`project`、`filters`、`top_k` 与逻辑 vault；模型、预算、索引和隐私策略全部来自启动时配置快照。 |
| `wiki_archive` | 归档生命周期的 `plan|apply` 公开入口；不提供 purge。 |
| `wiki_restore` | 不可变归档包的 `plan|apply` 恢复入口。 |

worker profile 只会额外注册 `wiki_generation`。init/config、batch ingest/reconcile、vector build/rebuild、lint/verify/debug/evaluation、purge 和 migration 只保留在 CLI/admin 边界。

`retrieval-eval` 使用版本化 JSONL 查询集和 manifest 只读评测公共查询契约，输出 JSON 与 Markdown 报告。默认 `--query-version v2`，只读取已构建的 passage/vector index，且关闭查询遥测；用 `--query-version v1` 生成可比的 legacy baseline，`--scope` 控制 V2 corpus。报告包含 Recall@10、MRR@10、nDCG@10、无答案误命中率、过滤器正确性、P95 延迟、context budget、语料指纹和运行 provenance；不会构建索引或写入 vault。CLI 默认对首个 case 单独测量 context budget；可用 `--context-budget-case-limit` 扩大样本，或以 `--no-context-budget` 显式跳过。

### 可选本地向量检索

向量检索默认关闭。唯一支持的 provider 是本地 `local_bge_m3`；模型目录在 `config set-retrieval` 写入用户级配置，加载时启用离线模式和 `local_files_only`，缺模型或未安装 `vector` extra 时只会结构化降级到关键词/图结果。为使 BGE-M3 的 CPU 全量建库可控，文档 embedding 默认上限为 256 tokens；该值是索引身份的一部分，改变后必须执行 full build。

先通过 `vector build` 显式构建索引；`wiki_query` 从不构建、更新索引或下载模型。索引保存于 vault 的 `.llm-wiki/vector-index/`，仅含相对路径、内容哈希、元数据和归一化向量，不保存正文。默认 `include_raw_sources=false`，这会同时约束建库、更新和查询。

配置本地模型后，普通查询不再传模型或索引参数：

```python
wiki_query(
    question="如何自动化应付账款处理？",
    vault="homework",
    scope="knowledge",
)
```

Query V2 默认返回 compact response：`results` 只含 path、heading、snippet 和 scores，正文只存在于一次性的 `context_pack.passages`。它按 scope 打开 active/history 或独立 archive store，先做 passage FTS/vector 召回，再以 RRF 和有界强-seed graph 扩展排序；source index、superseded 与 deprecated 页面不会进入正文。需要精确原文、低覆盖或 stale 证据时，`pipeline.fallback` 会说明原因，并且只追加已声明 source 的 capsule 或相关 raw passage，绝不返回整份 raw。

回滚只修改 vault 配置的 `retrieval.query_version`：默认 `v2`；在兼容排障期设为 `v1` 会使用旧 façade 并返回 `query_v1_legacy_feature_flag` warning。该开关属于启动时配置快照，不能由 MCP query 参数覆盖。

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
│       │   └── <project>/
│       │       ├── requirements/
│       │       ├── codegraph/
│       │       └── assets/
│       ├── chat/
│       │   └── <yyyy>/<mm>/<dd>/<session-id>/
│       ├── file/
│       └── references/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── overview.md
│   ├── concepts/
│   │   ├── index.md
│   │   └── <domain>/
│   ├── chatlog/
│   │   ├── index.md
│   │   └── <yyyy>/<mm>/<dd>/
│   ├── projects/
│   │   └── <project>/
│   │       ├── index.md
│   │       ├── specs/
│   │       ├── plans/
│   │       ├── architecture/
│   │       ├── pipelines/
│   │       ├── troubleshooting/
│   │       └── researches/
│   ├── sources/
│   │   ├── index.md
│   │   ├── concepts/
│   │   ├── projects/
│   │   ├── chatlog/
│   │   ├── queries/
│   │   └── entities/
│   ├── queries/
│   │   ├── index.md
│   │   └── <yyyy>/<mm>/<dd>/<query-id>/
│   ├── entities/
│   │   ├── index.md
│   │   └── <entity>/
│   └── archives/
│       ├── log.md
│       └── <yyyy>/<mm>/<dd>/
└── .llm-wiki/
    ├── ingest-cache/
    ├── ingest-queue.json
    ├── graph-index/
    └── relation-candidates/
```

## LLM Wiki 工作流

本项目遵循 LLM Wiki 模式：raw sources 保持为事实源 snapshot，而由 LLM 维护的 Markdown 页面会随着时间沉淀成可导航的 wiki。

推荐循环：

1. 用 `wiki_init` 初始化 vault，然后根据领域定制 `purpose.md` 和 `schema.md`。
2. 摄入 source：
   - 代码仓库 → `wiki_ingest_codegraph`（同步，不需要 LLM）
   - 本地文件 → `wiki_ingest_llm(stage="prepare")` → LLM 生成 → `wiki_ingest_llm(stage="apply")`
  - 会话历史 → `wiki_ingest_llm(source_type="chat", stage="prepare")` 先保存到 `raw/sources/chat/YYYY/MM/DD/<source_name>/` → `wiki_ingest_llm(stage="apply")` 生成 `wiki/chatlog/YYYY/MM/DD/` 会话页，并写 `wiki/sources/chatlog/YYYY/MM/DD/` 索引溯源页
   - 外部爬虫或人工收集的 MD 文件 → `raw/sources/references/` 或 `raw/sources/file/` → `wiki_ingest_llm(stage="prepare")` → `wiki_ingest_llm(stage="apply")`
3. 用 `wiki_verify` 校验生成页面是否忠实于原始来源。
4. 用 `wiki_query` 查询已积累的知识；回答时引用 numbered context pack。
5. 通过 `wiki_write_note`，把人工整理的 spec、plan、troubleshooting、researches 或 knowledge note 写回 `wiki/projects/<project>/specs/`、`wiki/projects/<project>/plans/`、`wiki/projects/<project>/troubleshooting/`、`wiki/projects/<project>/researches/` 或 `wiki/concepts/`。
6. 用 `wiki_lint` 和 `wiki_enrich` 保持图谱健康；使用 `wiki_lint(stage="prepare_semantic_review")` -> `wiki_lint(stage="apply_semantic_review")` 进行 LLM 辅助的矛盾、过期声明和缺失概念审查。

对于大范围本地 Markdown 搜索，可以把这个 MCP server 与 qmd 等外部工具搭配使用，但 qmd/vector search 有意不作为默认依赖或主检索路径。

### 批量摄入工作流

当需要一次性摄入大量源文件时，使用 `wiki_ingest_batch` 的批量 action：

1. **入队**：`wiki_ingest_batch(action="enqueue", tasks=[...])` — 批量添加待摄入任务
2. **批量 prepare**：`wiki_ingest_batch(action="prepare_all")` — 对所有 `pending` 任务运行 prepare，标记为 `prepared`（需要 LLM）或 `done`（源未变化）。批量响应只返回 `task_id`、`status`、`source_hash`、`has_prompt`、`generation_job_count` 等摘要，不返回整批 prompt 正文
3. **单条取 job**：优先使用 `wiki_ingest_batch(action="next_generation_job")`，每次只取一个 page generation job。兼容旧 source-level 流程时可用 `next_prepared` 取单个 prepared task 和 prompt
4. **隔离生成**：在独立子代理或新会话中只给当前 job 的 `raw_sources`、`raw_reading_instructions`、`expected_response_schema` 和精简 `context`，不要把其他任务的 prompt/generation/失败历史带入同一模型上下文
5. **保存 generation**：`wiki_ingest_batch(action="set_generation", task_id="...", job_id="...", result={"generation": ...})` — 只保存单条模型输出，不改变任务终态
6. **单条或批量 apply**：`wiki_ingest_batch(action="apply_one", task_id="...", job_id="...")` 或 `wiki_ingest_batch(action="apply_all")` — 写入前执行 generation schema / quality gate；通过后写 wiki，失败则标记 `failed`

`status` 也返回瘦身任务摘要：包含 `has_prompt`、`has_generation`、`source_hash`、`generation_hash`、`error_stage`、`validation_errors` 等诊断字段，不返回大段 prompt 或 generation 正文。

### Generation 质量门禁

`wiki_ingest_llm(stage="apply")` 和旧 `apply_generation` 在写任何页面、刷新索引或追加日志前，会先验证 generation payload：

- generation 必须是 JSON object。
- `source_summary` 必须是 object 或非空字符串。
- `pages` 必须是 list；允许为空，此时只生成 source index。
- 非空页面必须包含非空 `path`、`title`、`type`、`summary`、`body`、`sources`。
- `sources` 必须能归一到 prepared manifest 中的 raw source path。
- `path` 必须落在允许的 wiki 目录内，不能逃逸项目或 source_type 约束。
- `type` 只允许 `concept`、`entity`、`pipeline`、`spec`、`plan`、`research`、`troubleshooting`、`chatlog`、`source_index`。
- `body` 去除空白后至少 80 字符；`summary` 必须是短文本，不能塞入多段正文。

校验失败时不会写页面、不会刷新索引、不会追加 ingest log。返回形态为 `code="generation_schema_invalid"` 和结构化 `errors[]`；batch apply 会把任务标记为 `failed`，记录 `error_stage="validate"`、`validation_errors`、`generation_hash`，但不会在批量响应或 status 中回显坏 generation 正文。

**页面损坏恢复**：当 wiki 页面损坏但 ingest cache 完好时，使用 `wiki_ingest_batch(action="reapply")` 从缓存重新 apply，无需重新 prepare 或调用 LLM。reapply 会检测每页完整性，报告 `regeneration_needed`（需要完整 LLM 重新摄入的页面）和 `pages_restored`（仍然完好的页面数）。

## 数据流

### CodeGraph 摄入

```
wiki_ingest_codegraph → raw/sources/projects/<project>/codegraph/
                      → raw/sources/projects/<project>/codegraph/codefacts.json
                      → wiki/sources/projects/<project>/architecture/codegraph.md (索引溯源页)
                      → wiki/projects/<project>/architecture/code-overview.md
                      → wiki/projects/<project>/pipelines/ (仅 profile="suitescript" 且检测到链路时)
                      → index + overview + log 更新
```

`wiki_ingest_codegraph` 默认使用 `profile="generic"`，CodeGraph 机器事实保存到 raw，不再作为普通 wiki 知识页展示；可读层生成 `wiki/sources/` 下的索引溯源页和项目 architecture overview。SuiteScript/SuiteCloud 项目需要传 `profile="suitescript"` 才会启用 `N/task`、`N/record`、`N/url`、`form.clientScriptModulePath`、`custscript_*` 等隐式关系抽取和业务 pipeline 页面生成。对 SDF 项目根目录摄入时，可用 `include_extensions=[".js"]` 只保留脚本文件，避免 `Objects/*.xml` 混入代码事实。

### LLM 分阶段摄入

```
推荐两阶段流程：
prepare → 读源文件 + 写 raw/sources/<source_type>/ snapshot + 返回合并 prompt（agent 发送给 LLM）
  → source_type="chat" 时，写 raw/sources/chat/YYYY/MM/DD/<source_name>/
apply   → 写 wiki/concepts/ 或 wiki/projects/ 下的知识页面
  → 写 wiki/sources/ 下镜像目标 wiki 结构的索引溯源页
  → source_type="chat" 且生成 chatlog 时，写 wiki/sources/chatlog/YYYY/MM/DD/<source_name>.md 索引溯源页
  → 写入前校验 generation：source_summary、pages、页面 path/title/type/summary/body/sources、允许 type、路径安全、body 最低质量和 source manifest 可追溯性

旧三阶段（仍兼容）：
prepare_analysis   → 返回 analysis prompt
prepare_generation → 返回 generation prompt
apply_generation   → 写入 wiki 页面
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

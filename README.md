# NetSuite LLM Wiki MCP

本项目是一个本地 MCP server，用于维护外部 Obsidian 目录中的 NetSuite / SuiteCloud LLM Wiki。它不再以 Chroma、embedding 或传统 RAG 索引作为主路径；代码事实首版来自 CodeGraph，知识沉淀写入持久 Markdown Wiki。

## 核心理念

遵循 Karpathy / LLM Wiki 模式：

- `raw/`：不可变 sources，LLM 只读。
- `wiki/`：LLM 生成和维护的 Markdown Wiki，可直接作为 Obsidian 仓库使用。
- `schema.md`：结构规则、页面类型、frontmatter 和维护流程。
- `purpose.md`：目标、关键问题和研究范围。
- `wiki/index.md`：内容目录和 LLM 导航入口。
- `wiki/log.md`：可解析的时序操作记录。
- `[[wikilink]]`：页面交叉引用。
- YAML frontmatter：每个 Wiki 页面都携带结构化元数据。

## 外部 Obsidian Wiki 目录

```text
Wiki root/
├── purpose.md
├── schema.md
├── raw/
│   ├── sources/
│   └── assets/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── overview.md
│   ├── projects/
│   ├── concepts/
│   ├── sources/
│   ├── queries/
│   ├── synthesis/
│   └── comparisons/
├── .obsidian/
└── .llm-wiki/
```

项目页首版使用：

```text
wiki/projects/<project>/
├── index.md
├── code/
├── decisions/
├── troubleshooting/
└── requirements/
```

## MCP 工具

| 工具 | 功能 |
| --- | --- |
| `wiki_init` | 初始化外部 Obsidian LLM Wiki 目录结构 |
| `wiki_ingest` | 摄入 CodeGraph source；首版支持 `source_type="codegraph"` |
| `wiki_ingest_llm` | 分阶段摄入文本 source：prepare_analysis → prepare_generation → apply_generation |
| `wiki_rescan` | 重扫文本 source，基于 `source_hash` 判断 changed/unchanged 并刷新 raw snapshot |
| `wiki_query` | 基于关键词、`[[wikilink]]`、shared source、type graph 和上下文预算查询 Wiki |
| `wiki_query_debug` | 返回查询分数和 graph expansion 原因，帮助诊断召回 |
| `wiki_lint` | 检查结构、frontmatter、断链、source traceability 和 ingest cache |
| `save_obsidian_note` | 保存人工策展笔记：decision、troubleshooting、requirement、knowledge |

旧 RAG 工具 `index_vault`、`index_sources`、`search_netsuite_knowledge`、`ask_netsuite_rag`、`get_index_status` 仅保留兼容入口，并返回 deprecated 提示。

## 快速开始

```bash
python -m pip install -e ".[dev]"
pytest
netsuite-rag-mcp-server
```

初始化 Wiki：

```text
调用 wiki_init，vault_root 设为外部 Obsidian Wiki 根目录绝对路径。
```

初始化 CodeGraph（在 SuiteCloud 代码仓库中执行）：

```bash
codegraph init -i
```

摄入 CodeGraph 事实：

```text
调用 wiki_ingest：
- vault_root: 外部 Wiki root
- source_type: codegraph
- project: 项目名
- source_name: 代码来源名
- codegraph_project_path: 已初始化 CodeGraph 的代码仓库路径
- query: 可选，例如 "SuiteCloud entry points and dependencies"
```

查询 Wiki：

```text
调用 wiki_query，question 设为你的问题；如需限定项目，传 project。
```

### 分阶段文本摄入

`wiki_ingest_llm` 用于把 Markdown、文本、JSON、YAML、CSV 等 source 转成可追踪 Wiki 页面。流程分三步：

1. `stage="prepare_analysis"`：收集 source，写入 `raw/sources/<source_type>/<project>/<source_name>/`，计算 `source_hash`，返回分析 prompt。
2. `stage="prepare_generation"`：传入上一步的 `analysis`，结合 `purpose.md`、`schema.md` 和 `wiki/index.md`，返回生成 prompt。
3. `stage="apply_generation"`：传入模型生成的 JSON，写入 `wiki/sources/`、`wiki/projects/` 或 `wiki/concepts/`，刷新 index、overview 和 log。

相同 source hash 会跳过重复准备，缓存位于 `.llm-wiki/ingest-cache/<project>/<source_name>.json`。

`wiki_rescan` 可单独重扫 source：未变化返回 `status="unchanged"`，变化后刷新 raw snapshot 并返回新的分析 prompt。

### 查询 Pipeline

`wiki_query` 默认不使用向量库，流程为：

1. 关键词/CJK bigram 命中 Wiki 页面，可选包含 `raw/sources`。
2. 可选 vector 阶段只返回配置告警，不会启用 embedding 主路径。
3. 根据 `[[wikilink]]`、shared source、common neighbor、same type 做图扩展。
4. 按 `context_window_tokens` 生成带编号引用的 context pack。

`wiki_query_debug` 用于查看每个结果的 keyword/vector/graph 分数和 graph reason，例如 direct wikilink、shared source、common neighbor、same type。

检查 Wiki：

```text
调用 wiki_lint，确认结构、链接和来源追踪健康。
```

`wiki_lint` 还会检查生成页和 source summary 的 `sources` 是否存在，以及 `.llm-wiki/ingest-cache/` 中 manifest 的路径和 `stored_sha256` 是否与 raw snapshot 一致。

## 人工笔记路径

- `decision` → `wiki/projects/<project>/decisions/<slug>.md`
- `troubleshooting` → `wiki/projects/<project>/troubleshooting/<slug>.md`
- `requirement` → `wiki/projects/<project>/requirements/<slug>.md`
- `knowledge` → `wiki/concepts/<domain>/<slug>.md`

不再保存 `script` / `object` 人工事实页；对象、部署、字段、脚本参数等代码事实由 CodeGraph ingest 折叠到 `wiki/projects/<project>/code/` 或 source 摘要中。

## 开发约定

- 新行为优先补 pytest。
- 不引入 Chroma、sentence-transformers 或 embedding 模型。
- 不创建 `.rag-index/` 或 `.models/`。
- 所有写入都必须限制在外部 Wiki root 内。
- 生成页只能覆盖 `generated: true` 页面；人工页不得被静默覆盖。
- 写入 Markdown 前必须脱敏手机号、邮箱、token、密码等敏感信息。
- Windows 路径必须覆盖非法字符、ADS 冒号、保留设备名、控制字符、尾随点/空格等边界。

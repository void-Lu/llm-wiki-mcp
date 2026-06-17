# NetSuite LLM Wiki MCP

一个本地 MCP（Model Context Protocol）server，让 LLM 编码代理可以完整读写基于 Obsidian 的知识 Wiki。代码事实来自 CodeGraph；其他内容都通过 MCP 工具进行摄入、查询和维护。

不使用 embedding，不使用向量数据库，不使用 Chroma。只有 Markdown、YAML frontmatter 和 `[[wikilinks]]`。

## 安装

```bash
python -m pip install -e ".[dev]"
```

## 运行

```bash
# 启动 MCP server
netsuite-llm-wiki-mcp-server

# 或通过 CLI / module 启动
netsuite-llm-wiki-mcp server
python -m netsuite_llm_wiki_mcp.server
```

### CLI

```bash
netsuite-llm-wiki-mcp init --vault <name> --root <path> --default
netsuite-llm-wiki-mcp status
```

## 配置

server 按以下顺序解析 wiki 根目录（vault）：

1. 工具参数 `vault_root`
2. 环境变量 `NETSUITE_LLM_WIKI_VAULT_ROOT`
3. 全局配置 `config.yaml` → `default_vault`

`config.yaml` 位于 `netsuite-llm-wiki-mcp` 的平台用户配置目录：

- Windows: `%APPDATA%\\netsuite-llm-wiki-mcp\\config.yaml`
- macOS: `~/Library/Application Support/netsuite-llm-wiki-mcp/config.yaml`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/netsuite-llm-wiki-mcp/config.yaml`

开发和测试时，可以用 `NETSUITE_LLM_WIKI_CONFIG_DIR` 和 `NETSUITE_LLM_WIKI_USER_DATA_DIR` 覆盖配置/数据目录。

### MCP 客户端配置

添加到你的 MCP 客户端配置中（例如 Claude Code 的 `settings.json`）：

```json
{
  "mcpServers": {
    "netsuite-wiki": {
      "command": "netsuite-llm-wiki-mcp-server"
    }
  }
}
```

## 工具

### 诊断与文件

| 工具 | 说明 |
|------|------|
| `wiki_status` | 返回 vault 结构诊断、ingest queue 计数、版本和 CodeGraph 可用性；不会创建或修改 vault |
| `wiki_list_files` | 只列出公开路径 `wiki/` 和 `raw/sources/` 下的文件，支持 `root_name="wiki"|"sources"|"all"`、递归和数量限制 |
| `wiki_read_file` | 只读取 `wiki/` 或 `raw/sources/` 下的文本文件，拒绝绝对路径、路径穿越、运行时私有目录和非文本扩展，并按字节数截断 |

### 摄入

| 工具 | 说明 |
|------|------|
| `wiki_init` | 在 Obsidian vault 中创建 wiki 目录结构 |
| `wiki_ingest_codegraph` | 将 CodeGraph 快照和机器代码事实同步摄入 raw，并生成项目 source/architecture/pipeline 可读页（不需要 LLM） |
| `wiki_ingest_llm` | 两阶段 LLM 摄入（推荐）：`prepare`（返回合并 prompt）→ `apply`（写入页面）。旧三阶段 `prepare_analysis` → `prepare_generation` → `apply_generation` 仍兼容 |
| `wiki_rescan` | 重新扫描 source；如果 SHA256 未变化则跳过，如果变化则刷新 raw snapshot |
| `wiki_ingest_batch` | 持久化摄入队列：enqueue / next / complete / fail / retry / cancel / clear_done |

### 查询

| 工具 | 说明 |
|------|------|
| `wiki_query` | 关键词 + CJK bigram 搜索 → 图扩展 → 按上下文预算输出；结果包含标题匹配和嵌入图片元数据 |
| `wiki_query_debug` | 与查询相同，但返回每个结果的分数和图扩展原因 |

### 维护

| 工具 | 说明 |
|------|------|
| `wiki_lint` | 结构健康检查和分阶段语义审查：frontmatter、断链、source 可追溯性、cache 完整性、孤立页面、矛盾、过期声明、缺失概念 |
| `wiki_enrich` | 两阶段 wikilink 富化：prepare（返回 LLM prompt）→ apply（插入链接） |
| `wiki_page_merge` | 合并页面：frontmatter union + 锁定字段保护 + 可选 LLM 正文合并 |
| `wiki_dedup` | 重复页检测和合并：detect → confirm → merge（三阶段） |
| `wiki_insights` | 图谱洞察：孤立页面、桥接节点、意外跨类型连接、Louvain 社区 |
| `wiki_delete_source` | 删除 source 并级联清理：派生页面、交叉引用、cache；多 source 生成页会被保留，并移除被删除的 source |
| `wiki_verify` | 两阶段 grounding check：从 `wiki/sources/` 索引页出发，读取关联的 raw source 和生成页，返回 faithfulness 校验 prompt → `apply` 记录结果 |
| `wiki_gap` | 覆盖缺口分析：`analyze`（扫描浅页面、悬空链接、未摄入源、分类法缺失）→ `suggest`（推荐具体补充动作和工具） |
| `wiki_changelog` | 最近的 wiki log 条目 |

### 研究与笔记

| 工具 | 说明 |
|------|------|
| `wiki_research` | 深度研究综合：搜索结果 + `purpose.md` / `wiki/overview.md` / `wiki/index.md` → LLM 综合 → `wiki/queries/` 页面 |
| `wiki_synthesis` | 将有价值的查询答案或分析保存为项目内 `wiki/projects/<project>/researches/` 页面：`prepare` → `apply` |
| `wiki_write_note` | 写入人工整理的 wiki note；替代旧的 `save_obsidian_note` 公开工具名 |

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
5. 通过 `wiki_research` / `wiki_synthesis` / `wiki_write_note`，把有价值的研究、对比、查询答案和人工整理内容写回 `wiki/queries/`、`wiki/projects/<project>/researches/`、`wiki/projects/<project>/specs/`、`wiki/projects/<project>/plans/` 或 `wiki/concepts/`。
6. 用 `wiki_lint`、`wiki_enrich`、`wiki_dedup`、`wiki_insights` 和 `wiki_changelog` 保持图谱健康；使用 `wiki_lint(stage="prepare_semantic_review")` → `wiki_lint(stage="apply_semantic_review")` 进行 LLM 辅助的矛盾、过期声明和缺失概念审查。

对于大范围本地 Markdown 搜索，可以把这个 MCP server 与 qmd 等外部工具搭配使用，但 qmd/vector search 有意不作为默认依赖或主检索路径。

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
pytest

# 运行单个测试文件
pytest tests/test_wiki_query.py

# 运行单个测试函数
pytest tests/test_wiki_query.py::test_function_name -v
```

### 约定

- Python 3.11+，`src/` layout，最小依赖（`mcp` + `PyYAML`）
- 生成页只能覆盖 frontmatter 中带 `generated: true` 的页面
- 人工编写页面绝不静默覆盖
- 所有写入都限制在 vault root 内；`wiki/concepts/` 和 `wiki/projects/` 下的路径遵循固定结构
- 敏感数据（手机号、邮箱、token）写入前会被脱敏
- Windows 路径安全：非法字符、ADS 冒号、保留设备名、控制字符、尾随点/空格
- 不引入 Chroma、sentence-transformers 或 embedding 模型
- 不创建 `.rag-index/` 或 `.models/`

## 许可证

MIT

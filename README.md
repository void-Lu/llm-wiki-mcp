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

### 摄入

| 工具 | 说明 |
|------|------|
| `wiki_init` | 在 Obsidian vault 中创建 wiki 目录结构 |
| `wiki_ingest` | 将一个 CodeGraph source 摄入为 wiki 页面 |
| `wiki_ingest_llm` | 三阶段 LLM 摄入：`prepare_analysis` → `prepare_generation` → `apply_generation` |
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
| `wiki_changelog` | 最近的 wiki log 条目 |

### 研究与笔记

| 工具 | 说明 |
|------|------|
| `wiki_research` | 深度研究综合：搜索结果 + `purpose.md` / `wiki/overview.md` / `wiki/index.md` → LLM 综合 → `wiki/queries/` 页面 |
| `wiki_synthesis` | 将有价值的查询答案或分析保存为持久的 `wiki/synthesis/` 页面：`prepare` → `apply` |
| `wiki_write_note` | 写入人工整理的 wiki note；替代旧的 `save_obsidian_note` 公开工具名 |

## Wiki 结构

```
vault_root/
├── purpose.md              # 研究范围和关键问题
├── schema.md               # 页面类型、frontmatter 规范、维护规则
├── raw/
│   ├── sources/            # 不可变 source snapshot（LLM 只读）
│   └── assets/             # 二进制资产
├── wiki/
│   ├── index.md            # 内容目录，LLM 导航入口
│   ├── log.md              # 仅追加操作日志
│   ├── overview.md         # 自动生成摘要
│   ├── projects/<project>/ # 项目范围页面
│   │   ├── index.md
│   │   ├── code/           # CodeGraph 派生事实
│   │   ├── decisions/
│   │   ├── troubleshooting/
│   │   └── requirements/
│   ├── concepts/           # 领域知识（按 domain 子目录组织）
│   ├── sources/            # source 摘要页面
│   ├── queries/            # 研究综合页面
│   ├── synthesis/          # 跨页面分析
│   └── comparisons/        # 并排对比
├── .obsidian/              # Obsidian 应用配置
└── .llm-wiki/              # 运行时状态（ingest cache、queue）
```

## LLM Wiki 工作流

本项目遵循 LLM Wiki 模式：raw sources 保持为事实源 snapshot，而由 LLM 维护的 Markdown 页面会随着时间沉淀成可导航的 wiki。

推荐循环：

1. 用 `wiki_init` 初始化 vault，然后根据领域定制 `purpose.md` 和 `schema.md`。
2. 用 `wiki_ingest` 或分阶段的 `wiki_ingest_llm` 一次摄入一个 source，并在应用前审查生成摘要。
3. 用 `wiki_query` 查询已积累的知识；回答时引用 numbered context pack。
4. 通过 `wiki_research` / `wiki_synthesis` / `wiki_write_note`，把有价值的研究、对比、查询答案和人工决策写回 `wiki/queries/`、`wiki/synthesis/` 或项目笔记目录。
5. 用 `wiki_lint`、`wiki_enrich`、`wiki_dedup`、`wiki_insights` 和 `wiki_changelog` 保持图谱健康；使用 `wiki_lint(stage="prepare_semantic_review")` → `wiki_lint(stage="apply_semantic_review")` 进行 LLM 辅助的矛盾、过期声明和缺失概念审查。

对于大范围本地 Markdown 搜索，可以把这个 MCP server 与 qmd 等外部工具搭配使用，但 qmd/vector search 有意不作为默认依赖或主检索路径。

## 数据流

### CodeGraph 摄入

```
CodeGraph CLI → raw/sources/codegraph/<project>/<source_name>/
             → wiki/sources/ (source 摘要)
             → wiki/projects/<project>/code/ (代码事实页面)
             → index + overview + log 更新
```

### LLM 分阶段摄入

```
prepare_analysis  → 返回 analysis prompt（agent 发送给 LLM）
prepare_generation → 返回 generation prompt（agent 发送给 LLM）
apply_generation  → 从 LLM JSON 输出写入 wiki 页面
```

Cache：`.llm-wiki/ingest-cache/<project>/<source_name>.json`（通过 SHA256 跳过未变化 sources）。

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

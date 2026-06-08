# F/G 与 qmd 替代关系调查记录

**日期**：2026-05-27
**主题**：评估新接入的 qmd / qmd skill 是否可替代 F、G 两类拟议能力
**状态**：调查结论归档，本轮不实现

---

## 背景

上游同步分析中曾提出两个候选能力：

- **F：安全文件树/页面读取工具**
  例如 `wiki_list_files` / `wiki_read_page`，让 MCP 客户端安全列出和读取 `purpose.md`、`schema.md`、`wiki/**`、`raw/sources/**` 中的文本文件。

- **G：扩展 `wiki_research` 的 query generation 阶段**
  例如新增 `stage="prepare_queries"`，读取 `purpose.md` / `overview.md` / `index.md` / 相关 wiki 页面，为外部 Deep Research 生成搜索 query。

用户已安装 qmd 并接入 qmd skill，因此本文件记录 qmd 对 F/G 的替代程度与后续研究方向。

## qmd 能力摘要

qmd 用于搜索本地 Markdown collections，典型流程是：

1. `qmd search` 或 `qmd query` 找候选文档。
2. `qmd get` / `qmd multi-get` 取完整 Markdown。
3. agent 基于取回文本回答，并引用路径或 docid。

qmd 支持：

- BM25 lexical search：适合标题、路径、精确词、代码符号。
- hybrid/semantic query：适合概念检索。
- structured query：`intent:`、`lex:`、`vec:`、`hyde:` 组合。
- collection 过滤：例如只搜 `concepts`、`sources`、docs collections。
- MCP server / skill 接入：agent 可在需要本地 Markdown 知识时调用。

## 对 F 的替代评估

### F 原始目标

F 希望提供稳定 MCP 工具，用于：

- 列出 vault 内文件树。
- 读取指定 Markdown 文本。
- 限定可读路径，避免越权读取 `.llm-wiki/`、配置、密钥等。
- 给任意 MCP 客户端一个固定 JSON API。

### qmd 可替代部分

qmd 能很好替代以下使用场景：

- 搜索 wiki / raw sources 中的 Markdown。
- 按 docid / qmd path 取完整文档。
- 用 collection 约束搜索范围。
- 让 agent 在回答前检索本地知识库。
- 避免本 MCP server 为“读 Markdown”重复造轮子。

### qmd 不完全替代部分

qmd 与 F 的边界不同：

1. **安全边界不同**
   F 的安全模型由 `vault_root` 和固定 allow-list 控制；qmd 的边界由 qmd collection 配置控制。

2. **API 合约不同**
   F 是本 MCP server 的稳定工具合约；qmd 是外部工具/索引系统。

3. **文件树视角不同**
   F 可原样返回 vault 文件树；qmd 更偏检索和文档读取，不一定保留完整目录浏览语义。

4. **索引新鲜度不同**
   qmd 依赖 collection update/embed；F 直接读文件，看到的是磁盘当前状态。

### F 结论

当前用户工作流下，**F 可以暂缓**。建议优先使用 qmd 作为 Markdown 检索/读取层，本 MCP server 继续专注：写入、维护、ingest、图谱、source lifecycle。

如果未来出现以下需求，再重新考虑 F：

- 需要给非 qmd 客户端提供统一文件读取 API。
- 需要严格由 `vault_root` 控制读取边界。
- 需要无需 qmd 索引即可读取当前磁盘文件。
- 需要 MCP 工具返回标准化文件树用于 UI/自动化。

## 对 G 的替代评估

### G 原始目标

G 希望为 `wiki_research` 增加 query generation 阶段：

```text
topic + purpose.md + overview.md + index.md + relevant pages
  → 生成 N 条外部搜索 query
  → 调用方执行 web search
  → wiki_research prepare/apply 综合写入 wiki/queries/
```

### qmd 可替代部分

qmd 可以承担 G 的“本地上下文召回”部分：

- 搜 `purpose.md` / `overview.md` / `wiki/index.md`。
- 搜与 topic 相关的 concepts / sources / decisions。
- 取回完整 Markdown，供 agent 生成外部搜索 query。
- 使用 structured query，把精确词和语义意图结合起来。

### qmd 不完全替代部分

qmd 本身不负责：

- 产出固定 JSON schema 的搜索 query 列表。
- 管理 Tavily / SerpApi / SearXNG 等外部搜索 provider。
- 将搜索 query generation 固化为 MCP 工具调用。
- 把 query generation 与 `wiki_research` 的 `prepare/apply` 状态机合并。

### G 推荐工作流（暂不新增 MCP 工具）

当前建议用 agent + qmd 组合完成 G：

1. agent 使用 qmd 搜索本地 wiki 上下文。
2. agent 取回完整页面并引用路径。
3. agent 基于上下文生成外部搜索 query。
4. 用户或 agent 执行 web search。
5. 将搜索结果传给 `wiki_research(stage="prepare")`。
6. 将 LLM synthesis 传给 `wiki_research(stage="apply")`。

该流程保留了当前 MCP 的 staged 可控性，也避免新增搜索 API key 和 provider 配置。

### G 结论

**G 可以暂缓**。qmd 已足够支撑“本地上下文召回 + agent 生成搜索 query”的人工协作流程。

如果未来需要自动化批处理，再考虑新增 `wiki_research(stage="prepare_queries")`，但它应只生成 query，不直接联网搜索。

## 总体建议

1. 本轮不实现 F/G。
2. 将 qmd 作为首选本地 Markdown 检索/读取方案。
3. 不把 qmd 作为 `netsuite-llm-wiki-mcp` 默认依赖，避免扩大安装面。
4. 后续可在 README 增加“推荐搭配 qmd skill 查询本地 Markdown”的使用说明，但这不是本轮范围。
5. 若后续实现 F/G，应先明确目标是“补齐 qmd 无法覆盖的稳定 MCP API”，而不是重复 qmd 的检索能力。

## 后续研究问题

1. qmd collection 是否应按 vault 子目录拆分，例如 `concepts`、`sources`、`projects`？
2. qmd 的索引更新是否可以由用户手动维护，还是需要 MCP 工具提示用户运行 update？
3. qmd 的 docid/path 引用能否稳定映射回 Obsidian 相对路径？
4. 如果多个 vault 共存，qmd collection 命名如何避免串库？
5. 是否需要在 `wiki_query` 结果中提示“更广泛 Markdown 搜索请使用 qmd”？

## 当前决策

- **F：暂不实现，优先由 qmd 覆盖搜索/读取场景。**
- **G：暂不实现，优先由 qmd + agent 组合生成外部研究 query。**
- **H：另行设计并优先考虑实现，因为它是 source lifecycle 的写入/抓取能力，qmd 不覆盖。**
# raw/sources 标记 URL 抓取与清洗设计方案

**日期**：2026-05-27
**优先级**：P1（高价值、中等复杂度）
**影响模块**：`wiki_ingest.py`、`server.py`、新增 `wiki_fetch_url.py`（建议）
**状态**：设计草案，本轮不实现

---

## 背景

当前 `wiki_ingest_llm` 支持从本地文本源复制/脱敏到 `raw/sources/<source_type>/<project>/<source_name>/`，再通过三阶段 LLM 流程生成 wiki 页面。用户希望进一步支持：在 `raw/sources/**` 的 Markdown 文件里显式标记 URL，然后由 MCP 工具继续抓取网页、清洗正文、保存为新的 raw snapshot，并复用现有 ingest 流水线。

该能力对应上游 `llm_wiki` 的 Web Clipper / Deep Research 部分，但当前项目是无头 MCP server，不应引入浏览器扩展或常驻 daemon。更适合的形态是显式、可审计、可 dry-run 的 MCP 工具。

## 目标

1. 扫描 `raw/sources/**.md` 或用户指定 Markdown 源文件中的显式 URL 标记。
2. 支持 dry-run：先返回将要抓取的 URL 列表、来源文件和去重结果，不写文件。
3. 非 dry-run 时抓取 URL、清洗 HTML/文本、写入 `raw/sources/web/<project>/<source_name>/`。
4. 生成 manifest/cache，使相同 URL 内容未变化时可跳过。
5. 返回下一步 staged ingest 提示，复用 `wiki_ingest_llm` 的 `prepare_analysis` → `prepare_generation` → `apply_generation`。

## 非目标

- 不自动抓取普通 Markdown 链接，避免把引用、导航、示例链接误认为来源。
- 不实现 Chrome Web Clipper、浏览器扩展或后台 watcher。
- 不引入向量库、embedding、`.rag-index` 或新的 RAG 主路径。
- 不直接调用搜索引擎；本功能只处理已经在 Markdown 中显式标记的 URL。
- 不在 v1 处理 PDF/Office/图片多模态网页资源；仅保存清洗后的文本/Markdown。

## URL 标记格式

### 首选：frontmatter `source_urls`

```yaml
---
title: 需求调研入口
source_urls:
  - https://example.com/article-a
  - https://example.com/article-b
---
```

### 可选：正文 HTML 注释标记

```markdown
<!-- llm-wiki:fetch-url https://example.com/article-a -->
```

### 不建议：扫描所有普通 Markdown 链接

普通链接如 `[参考](https://example.com)` 默认不抓取。原因：普通链接可能只是引用、跳转、截图说明或示例，自动抓取会产生噪声，也更容易触发安全和限流问题。

## 建议工具形态

新增 MCP 工具：`wiki_fetch_marked_urls`

### 参数

```python
def wiki_fetch_marked_urls(
    vault_root: str,
    project: str,
    source_name: str,
    source_path: str | None = None,
    dry_run: bool = True,
    max_urls: int = 20,
    timeout_seconds: int = 15,
    max_bytes: int = 2_000_000,
    user_agent: str = "llm-wiki-mcp/0.5",
  ) -> dict[str, Any]:
    """Fetch explicitly marked URLs and persist cleaned web snapshots."""
```

### 参数语义

- `vault_root`：目标 wiki vault。
- `project` / `source_name`：写入 `raw/sources/web/<project>/<source_name>/` 的命名空间。
- `source_path`：可选。为空时扫描 `vault_root/raw/sources/**/*.md`；有值时只扫描指定文件或目录。
- `dry_run`：默认 `True`。返回将抓取 URL，不写文件。
- `max_urls`：单次最多抓取 URL 数量，默认 20，建议硬上限 100。
- `timeout_seconds`：单 URL 超时。
- `max_bytes`：单 URL 最大响应体。
- `user_agent`：可配置但默认固定，便于目标站点识别。

## 数据流

```text
raw/sources/**/*.md
  → 提取 frontmatter source_urls + 注释标记
  → URL 校验 / 去重 / dry_run 报告
  → 抓取 HTTP(S)
  → HTML/文本清洗
  → 脱敏
  → raw/sources/web/<project>/<source_name>/<slug>.md
  → manifest.json + fetch-cache
  → 返回 wiki_ingest_llm prepare_analysis 下一步提示
```

## 写入结构

```text
raw/sources/web/<project>/<source_name>/
├── manifest.json
├── article-a.md
└── article-b.md

.llm-wiki/fetch-cache/<project>/<source_name>.json
```

抓取后的 Markdown frontmatter 示例：

```yaml
---
type: web_snapshot
generated: true
source_url: https://example.com/article-a
source_parent: raw/sources/file/alpha/docs/seed.md
fetched_at: 2026-05-27T00:00:00Z
content_sha256: <sha256>
stored_sha256: <sha256-after-redaction>
---
```

正文结构建议：

```markdown
# <网页标题或 URL slug>

Source URL: https://example.com/article-a

## Extracted Content

<清洗后的正文>
```

## 清洗策略

### v1：标准库优先

为了维持当前项目“运行依赖只有 `mcp` + `PyYAML`”的约束，v1 建议使用 Python 标准库：

- `urllib.request` 抓取 HTTP(S)。
- `html.parser.HTMLParser` 提取标题和正文文本。
- 移除 `script`、`style`、`noscript`、`svg` 等标签内容。
- 对段落、标题、列表做基础换行。

优点：无新增依赖，安装/测试简单。
缺点：正文抽取质量不如 Readability。

### 后续可选增强

如果后续接受可选依赖，可考虑：

- `beautifulsoup4`：更稳的 HTML 清洗。
- `readability-lxml`：更接近 Web Clipper 的正文抽取质量。

但这应作为可选 extras，例如 `.[web]`，不要进入默认依赖。

## 安全边界

必须实现以下校验：

1. 只允许 `http://` 和 `https://`。
2. 拒绝 `localhost`、`127.0.0.0/8`、`::1`、RFC1918 内网地址、link-local、metadata 地址。
3. 拒绝重定向到不安全地址。
4. 限制 `max_urls`、`max_bytes`、`timeout_seconds`。
5. 只接受文本型响应：`text/html`、`text/plain`、`text/markdown`、`application/xhtml+xml` 等。
6. 不把 URL token、query 中疑似密钥内容写入日志；必要时复用 `redaction.py`。
7. 写入路径只使用 `safe_segment` / `slug` 生成，不信任网页标题。
8. 默认 dry-run，避免 agent 在未确认时发起外部网络请求。

## 缓存与去重

`.llm-wiki/fetch-cache/<project>/<source_name>.json` 建议记录：

```json
{
  "urls": {
    "https://example.com/article-a": {
      "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "stored_path": "raw/sources/web/alpha/research/article-a.md",
      "fetched_at": "2026-05-27T00:00:00Z",
      "status": "fetched"
    }
  }
}
```

如果 URL 内容 hash 未变化，工具返回 `status: "unchanged"` 并跳过重写。若网页不可达，记录为单 URL 错误，不影响其他 URL。

## 错误返回建议

- `no_marked_urls`：未找到显式 URL 标记。
- `too_many_urls`：超过 `max_urls`。
- `unsupported_url_scheme`：非 HTTP(S)。
- `blocked_private_address`：命中 SSRF 防护。
- `fetch_timeout`：抓取超时。
- `response_too_large`：响应体超过上限。
- `unsupported_content_type`：非文本响应。
- `fetch_failed`：其他网络错误。

## 测试计划

新增 `tests/test_wiki_fetch_url.py`，按 TDD 覆盖：

1. 从 frontmatter `source_urls` 提取 URL。
2. 从 `<!-- llm-wiki:fetch-url https://example.com/article-a -->` 提取 URL。
3. dry-run 不写文件，只返回 URL 列表。
4. 非 dry-run 写入 `raw/sources/web/<project>/<source_name>/article-a.md` 和 manifest。
5. 重复 URL 去重。
6. 拒绝 `file://`、localhost、内网 IP。
7. 超过 `max_urls` 返回错误。
8. 抓取 HTML 时移除 script/style 并保留标题/正文。
9. 缓存命中时返回 unchanged。

`tests/test_server_tools.py` 需要补 MCP 注册与 wrapper 委托测试。README 需要补工具说明和数据流说明。

## 推荐实施任务

1. 新增 `wiki_fetch_url.py`：URL 标记提取、校验、抓取、清洗、写入、缓存。
2. 新增 `server.py` wrapper：`wiki_fetch_marked_urls_tool` + `@mcp.tool()` 注册。
3. 新增测试：业务测试 + server 工具测试。
4. 更新 README 工具表和数据流。
5. 跑相关测试，再跑全量 `pytest`。

## 开放问题

1. 是否接受 v1 只用标准库，牺牲一点网页正文抽取质量？
2. `source_path=None` 时是否扫描所有 `raw/sources/**/*.md`，还是要求用户显式传入 source path？建议默认扫描，但 `max_urls` 严格限制。
3. 是否需要尊重 `robots.txt`？作为 MCP 本地工具，建议至少保留清晰 User-Agent；robots 处理可后续设计。
4. 抓取结果是否自动进入 `wiki_ingest_llm prepare_analysis`？建议只返回 `next_call`，不自动调用下一阶段，保持 staged 可控。

## 验收标准

1. 用户能在 raw/source Markdown 中显式标记 URL。
2. dry-run 能准确报告待抓取 URL 和来源文件。
3. 非 dry-run 能抓取、清洗、脱敏并保存为 raw web snapshot。
4. SSRF/大小/超时/content-type 防护有测试覆盖。
5. 返回结果包含可直接继续调用 `wiki_ingest_llm` 的下一步提示。
6. 不新增默认运行依赖，不引入 embedding/vector 主路径。
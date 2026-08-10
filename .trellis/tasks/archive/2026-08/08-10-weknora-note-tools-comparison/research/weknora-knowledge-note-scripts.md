# Research: Tencent/WeKnora 知识库与知识条目脚本、接口与调用链

- Query: 基于 Tencent/WeKnora 官方一手来源，锁定当前默认分支提交，梳理知识库容器与知识条目/文档/分块的创建、导入、解析、查询、检索、更新、删除、批处理、状态与任务维护，并提炼与本地 MCP 对照的能力维度。
- Scope: mixed（官方 Git 仓库、固定提交源码、官方仓库内文档与测试；不含二手来源，不含运行时压测）
- Date: 2026-08-10

## Findings

### 1. 研究基线与可复现性

- 官方仓库：[`Tencent/WeKnora`](https://github.com/Tencent/WeKnora)。
- 访问时通过官方 Git remote 的符号引用确认默认分支为 `main`，`HEAD` 为 **`355d161d644793e6d7613ab34ef0aad610f9f9bf`**。
- 固定提交：[355d161d644793e6d7613ab34ef0aad610f9f9bf](https://github.com/Tencent/WeKnora/commit/355d161d644793e6d7613ab34ef0aad610f9f9bf)；官方提交元数据显示 committer time 为 2026-08-10，提交标题为 `feat: add Metaso web search provider`。
- 本文所有 WeKnora 源码链接都固定到该 SHA；即使 `main` 后续移动，本文证据仍可复现。

### 2. 首要结论：必须区分三层对象

| 层级 | WeKnora 含义 | 关键字段/职责 | 固定证据 |
| --- | --- | --- | --- |
| Knowledge Base（KB） | 容器与策略边界，不是单篇笔记 | `id/name/type/tenant_id/creator_id`；分块、模型、存储、向量库、图谱、Wiki、自动标签等配置；聚合 `knowledge_count/chunk_count/processing_count` | [`KnowledgeBase` 模型 L58-L149](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledgebase.go#L58-L149) |
| Knowledge | 一条来源记录，通常对应一个文件、URL、手工 Markdown 或 passage | 来源、标题、文件信息、内部/自定义 metadata、处理状态、摘要状态、错误、所属 KB；它本身不是检索最小单元 | [`Knowledge` 状态 L40-L90、模型 L120-L191](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledge.go#L40-L191) |
| Chunk | 解析后或派生出的检索单元 | 文本、原文偏移、前后/父子关系、类型、启用状态、索引状态、内容修订、图片与 metadata；向量/关键词检索主要返回此层 | [`Chunk` 类型 L15-L50、模型 L107-L195](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/chunk.go#L15-L195) |

这一区分对本地 MCP 对照非常重要：本地“笔记页面”若同时承担容器、原始文档与检索 passage 三种职责，不能按名称机械映射；应分别对照 KB 生命周期、Knowledge 来源生命周期和 Chunk/索引生命周期。

### 3. 相关文件清单

#### 3.1 路由、Handler、Service、Repository、模型

| 文件 | 一句话说明 |
| --- | --- |
| [`internal/router/routes_knowledge.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go) | KB、Knowledge、Chunk、FAQ、Tag、Wiki 的 HTTP 路由、API-key capability 与 RBAC/KB access 守卫总表。 |
| [`internal/handler/knowledgebase.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go) | KB 创建、列表、详情、更新、删除、置顶、混合检索、复制/副本/进度入口。 |
| [`internal/handler/knowledge.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go) | 文件/URL/手工知识创建、列表/详情、阶段追踪、更新、删除、重解析、取消、批处理、移动与 metadata 搜索入口。 |
| [`internal/handler/chunk.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/chunk.go) | Chunk 列表/详情、编辑、修订、回退、删除和生成问题维护入口。 |
| [`internal/handler/session/qa.go#L657-L757`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/session/qa.go#L657-L757) | 独立 `/knowledge-search` 的 `SearchKnowledge` Handler 符号所在处。 |
| [`internal/application/service/knowledgebase.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase.go) | KB 业务规则、计数、更新、软删除与异步重清理、复制/仅配置副本。 |
| [`internal/application/service/knowledgebase_search.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search.go) | 多 KB/多 store 混合检索主流程。 |
| [`internal/application/service/knowledgebase_search_fanout.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search_fanout.go) | 按存储组并发 fan-out 与超时/归一化。 |
| [`internal/application/service/knowledgebase_search_fusion.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search_fusion.go) | 向量/关键词分类、单路去重和加权 RRF 融合。 |
| [`internal/application/service/knowledgebase_search_results.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search_results.go) | Chunk、父块、相邻/关系块与知识元信息的结果组装。 |
| [`internal/application/service/knowledge_create.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go) | 文件、URL、远程文件、手工 Markdown、passage 的创建、去重、存储、落库与任务入队。 |
| [`internal/application/service/knowledge_process.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go) | 文档解析、分块、索引、摘要/问题任务、重解析、取消与批量重解析 worker。 |
| [`internal/application/service/knowledge_post_process.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_post_process.go) | 主流水线后的摘要、问题、图谱、Wiki、自动标签扇出与终态协调。 |
| [`internal/application/service/knowledge_delete.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_delete.go) | Knowledge/批量 Knowledge 的索引、Chunk、图谱、Wiki、文件与存储统计清理。 |
| [`internal/application/service/knowledge_housekeeping.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_housekeeping.go) | 卡死 `pending/processing/finalizing` 与摘要任务的周期恢复。 |
| [`internal/application/repository/knowledgebase.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/repository/knowledgebase.go) | KB GORM 持久化和查询。 |
| [`internal/application/repository/knowledge.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/repository/knowledge.go) | Knowledge 持久化、分页/过滤、状态与 finalization 原子更新。 |
| [`internal/application/repository/chunk.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/repository/chunk.go) | Chunk CRUD、批量写入、修订与 FAQ 差量相关持久化。 |
| [`internal/types/task.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/task.go) | 队列拓扑、任务类型、Document/KB delete/copy/Knowledge list delete/reparse/move 等 payload 与进度模型。 |
| [`internal/router/task.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/task.go) | Asynq 客户端、worker pool、handler 注册、重试与死信状态维护。 |
| [`internal/router/sync_task.go`](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/sync_task.go) | Lite 模式同步执行器，注册与 Asynq 模式相同的业务 handler。 |

#### 3.2 官方命令/脚本入口

- 官方 Go CLI 位于 `cli/`。知识生命周期主命令组为 `kb`、`doc`、`chunk`、`search`：官方文档列出 KB 创建/查看/编辑/删除/状态与配置，文档上传/创建/列表/详情/更新/删除/重解析/等待，以及 Chunk 列表/详情/删除和混合检索。[CLI KB 命令 L282-L300](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/05-clients/02-cli.md#L282-L300)、[doc 命令 L302-L320](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/05-clients/02-cli.md#L302-L320)、[chunk/search 命令 L322-L341](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/05-clients/02-cli.md#L322-L341)。
- 与本题最直接的源码入口包括：[`cli/cmd/kb/`](https://github.com/Tencent/WeKnora/tree/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/kb)、[`cli/cmd/doc/`](https://github.com/Tencent/WeKnora/tree/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/doc)、[`cli/cmd/chunk/`](https://github.com/Tencent/WeKnora/tree/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/chunk)、[`cli/cmd/search/`](https://github.com/Tencent/WeKnora/tree/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/search)。`doc upload --recursive --glob` 提供目录批量上传；`doc wait` 把异步处理状态变成可脚本化等待点。
- CLI 内建 MCP 只暴露精选的只读 KB/doc/chunk/search 工具以及会创建会话记录的 chat 工具，明确排除 create/delete/upload 等破坏性动词；因此不能把 CLI MCP 工具集等同于 REST/CLI 的完整生命周期能力。[官方说明 L435-L443](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/05-clients/02-cli.md#L435-L443)。
- `docreader/scripts/parse_local.py` 是解析器本地调试脚本，不是 KB/Knowledge 持久化入口；真正入库仍由 Go Handler/Service 驱动。[固定文件](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/docreader/scripts/parse_local.py)。
- 仓库还保留独立 Python `mcp-server/`（含 `weknora_mcp_server.py`、`upload_paths.py` 与测试），但本轮未完成其与新版 Go CLI MCP 的版本/部署优先级对齐，故仅记录存在，不据此宣称完整工具契约。[固定目录](https://github.com/Tencent/WeKnora/tree/355d161d644793e6d7613ab34ef0aad610f9f9bf/mcp-server)。

### 4. API 入口清单

#### 4.1 KB 容器层

路由总表直接注册以下能力，并在入口层区分 `retrieve` 与 `manage_kbs` API-key capability；内容写入则属于子资源的 `ingest` capability。[路由 L182-L245](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L182-L245)。

| 方法与路径 | 能力 | 主要 Handler | 关键契约 |
| --- | --- | --- | --- |
| `POST /knowledge-bases` | 创建容器 | `CreateKnowledgeBase` | 请求直接绑定 `types.KnowledgeBase`；成功 201，返回 `success + data`；存储 provider、prompt、extract 和 vector-store binding 会校验。[Handler L340-L405](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L340-L405) |
| `GET /knowledge-bases` | 列表 | `ListKnowledgeBases` | 支持 `agent_id` 共享智能体范围；聚合知识、Chunk、处理中数量和个人 pin。[Handler L559-L620](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L559-L620) |
| `GET /knowledge-bases/:id` | 详情 | `GetKnowledgeBase` | 详情补聚合计数、解析存储视图与共享权限。[Handler L538-L556](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L538-L556) |
| `PUT /knowledge-bases/:id` | 更新容器配置 | `UpdateKnowledgeBase` | DTO 只收 `name/description/config`；`vector_store_id` 创建后不可改。[Handler/DTO L831-L904](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L831-L904) |
| `DELETE /knowledge-bases/:id` | 删除容器及内容 | `DeleteKnowledgeBase` | 所有者租户 Admin 才能删；HTTP 先软删除 KB，再异步重清理。[Handler L907-L952](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L907-L952) |
| `PUT /knowledge-bases/:id/pin` | 当前用户置顶 | `TogglePinKnowledgeBase` | pin 是 per-user，而不是 KB 全局配置。[路由 L217-L224](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L217-L224) |
| `POST`（兼容 `GET`）`/:id/hybrid-search` | Chunk 级混合检索 | `HybridSearch` | `query_text` 必填，除非提供预计算 embedding 且仅走向量；返回 `data: SearchResult[]`。[Handler L286-L337](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L286-L337) |
| `POST /knowledge-bases/copy` + `GET /copy/progress/:task_id` | 异步复制内容 | `CopyKnowledgeBase` / `GetKBCloneProgress` | 有源/目标 KB 预检与租户/存储/embedding 一致性约束。[路由 L229-L242](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L229-L242) |
| `POST /knowledge-bases/:id/duplicate` | 仅复制设置 | `DuplicateKnowledgeBase` | 明确不复制 Knowledge、Chunk、FAQ、Wiki、索引、分享或 pin。[Service L1204-L1265](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase.go#L1204-L1265) |
| `GET /knowledge-bases/:id/move-targets` | 可迁移目标 | `ListMoveTargets` | 供 Knowledge 跨库移动预检。[路由 L243-L244](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L243-L244) |

官方 API 总表与字段说明还记录 KB 类型、配置对象、聚合计数和 vector-store 只读视图字段。[API 文档 L5-L27、L28-L50](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/docs/api/knowledge-base.md#L5-L50)。

#### 4.2 Knowledge 来源/文档层

路由把 KB-scoped 创建/列表/清空与按 Knowledge ID 的维护分开；所有读写继续经过父 KB access/ownership 链。[路由 L57-L133](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L57-L133)。

| 方法与路径 | 能力 | 参数/返回/语义摘要 |
| --- | --- | --- |
| `POST /knowledge-bases/:id/knowledge/file` | multipart 文件上传 | `file` 必需；可带 `fileName`（同时形成逻辑 `folder_path`）、JSON `metadata`、`tag_ids`、`channel`、`enable_multimodel`、`process_config`。成功返回 Knowledge；默认文件大小上限 50MB（环境变量可改）。[Handler L316-L438](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L316-L438) |
| `POST /knowledge-bases/:id/knowledge/url` | 网页或远程文件导入 | JSON 包含 `url/file_name/file_type/title/tag_ids/channel/process_config`；入口与 worker 都做 SSRF 校验；可自动分流到远程文件模式。[Handler L441-L536](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L441-L536) |
| `POST /knowledge-bases/:id/knowledge/manual` | 手工 Markdown 笔记 | `ManualKnowledgePayload{title,content,status,tag_ids,channel,process_config}`；`draft` 仅落库，`publish` 入异步分块/索引。[模型 L254-L269](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledge.go#L254-L269)、[Service L727-L847](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L727-L847) |
| `GET /knowledge-bases/:id/knowledge` | KB 内分页列表 | 支持 tags、keyword、file_type、parse_status、source、时间区间、folder_path 与 subtree；返回 `data/total/page/page_size`。[Handler L935-L1030](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L935-L1030) |
| `GET /knowledge/:id` | 单条详情 | 验证父 KB Viewer access；返回带 tags 等关联字段的 Knowledge。[Handler L598-L642](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L598-L642) |
| `GET /knowledge/:id/stages`、`/spans` | 解析过程详情 | 返回 attempt、parse status、current stage、五阶段 trace 与 last error；支持查看历史 attempt。[Handler L645-L744](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L645-L744) |
| `PUT /knowledge/:id` | Knowledge metadata 更新 | 绑定整个 `Knowledge` 请求对象并回读更新结果；metadata 更新可触发摘要刷新。[Handler L1794-L1833](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L1794-L1833) |
| `PUT /knowledge/manual/:id` | 手工 Markdown 更新 | 只允许 manual；draft 保存不索引，publish 重置为 pending 并异步清理/重索引。[Service L986-L1136](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L986-L1136) |
| `DELETE /knowledge/:id` | 单条异步删除 | 复用批量删除 pipeline，返回 `task_id`，而非在 HTTP 请求中同步做全部清理。[Handler L1291-L1334](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L1291-L1334) |
| `DELETE /knowledge-bases/:id/knowledge` | 清空 KB 内容但保留 KB | 仅所有者 Admin；空库幂等返回 0，否则提交列表删除任务。[Handler L1434-L1502](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L1434-L1502) |
| `POST /knowledge/:id/reparse` | 单条重解析 | 可选 `process_config` 覆盖；先清旧资源、重置状态，再按 file/file_url/url/manual 重新入队。[Handler L1928-L1994](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L1928-L1994) |
| `POST /knowledge/:id/cancel-parse` | 取消解析/富化 | 仅 pending/processing/finalizing 可取消；保留已写 Chunk/索引，清待处理子任务计数并尽力撤队；可随后 reparse。[Service L2690-L2789](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L2690-L2789) |
| `POST /knowledge/batch-delete` | 同 KB 批量删除 | 去重后 1–200 个 ID；先整批验证全部存在且都属于 `kb_id`，否则 400 整批拒绝；成功返回 `task_id/deleted_count`。[Handler L1337-L1431](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L1337-L1431) |
| `POST /knowledge/batch-reparse` | 同 KB 批量重解析 | 去重后 1–200 个 ID、可带公共 `process_config`；先整批归属校验，再入维护队列。[Handler L2686-L2786](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L2686-L2786) |
| `POST /knowledge/move` + `GET /move/progress/:task_id` | 跨 KB 异步迁移 | `reuse_vectors` 或 `reparse`；源/目标不同、同类型、同 embedding；仅 completed 文档；跨 vector store 禁止 reuse，要求 reparse。[Handler L2411-L2577](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L2411-L2577) |
| `GET /knowledge/search` | Knowledge 元信息搜索 | 搜 title/file 等元信息，支持 keyword/query、recent、offset/limit、file_types 和 agent/API-key scope；这不是 Chunk 语义检索。[Handler L2204-L2378](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L2204-L2378) |

另有 folder tree/rename/move、批量标签、摘要重生成、原文件 download/preview 与图片信息更新，均已在同一路由块注册。[路由 L76-L81、L102-L132](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L76-L132)。

#### 4.3 Chunk/分块层

- 完整路由包含列表、按 ID 详情、修订历史、更新、回退、单块/全块删除，以及生成问题增删改/再生成；读为 Viewer + 父 KB read，写为 KB owner 或 Admin + write。[路由 L22-L54](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L22-L54)。
- `GET /chunks/:knowledge_id` 是分页分块列表；`GET /chunks/by-id/:id` 是单块详情；`PUT /chunks/:knowledge_id/:id` 支持内容、启用状态与 `expected_revision`。官方 API 明确：revision 不匹配返回 409；成功保存旧版本、`content_revision + 1`，索引状态经历 `processing → ready`，重建失败则为 `failed`。[官方 Chunk API L13-L58](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/04-api/02-api-chunks.md#L13-L58)。
- 删除单块或一个 Knowledge 下所有块是独立能力；这意味着 WeKnora 允许维护检索投影而不删除上层 Knowledge。[官方 Chunk API L91-L108](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/04-api/02-api-chunks.md#L91-L108)。

#### 4.4 两类“搜索”不能混淆

1. `GET /knowledge/search` 搜的是 Knowledge 文件/来源元信息，返回 Knowledge 卡片与分页信息；它不是正文 passage 检索。[Handler L2204-L2378](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L2204-L2378)。
2. `POST /knowledge-search` 和 `POST /knowledge-bases/:id/hybrid-search` 搜正文 Chunk。独立接口支持单/多 KB、可选 `knowledge_ids` 限定文件，直接返回命中 Chunk、所属 Knowledge、原文偏移、score、类型、metadata 和来源，不调用 LLM 总结。[官方接口 L5-L26、L59-L101](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/docs/api/knowledge-search.md#L5-L101)。

### 5. 入口 → 服务 → 存储/任务调用链

#### 5.1 创建 KB

```text
POST /knowledge-bases
  → KnowledgeBaseHandler.CreateKnowledgeBase
  → knowledgeBaseService.CreateKnowledgeBase
  → 校验租户、存储与 vector-store binding，填默认配置
  → KnowledgeBaseRepository.CreateKnowledgeBase（GORM）
  → 201 {success:true,data:KB视图}
```

Handler 的请求绑定、校验、typed AppError 透传和 201 响应见 [L352-L405](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L352-L405)，Service 创建函数边界见 [`knowledgebase.go` L113-L173](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase.go#L113-L173)。

#### 5.2 文件上传

```text
multipart POST /knowledge-bases/:kb_id/knowledge/file
  → KnowledgeHandler.CreateKnowledgeFromFile
  → knowledgeService.CreateKnowledgeFromFile
  → 解析 folder/metadata/process overrides、校验文件类型/配额
  → 文件 hash + repo.CheckKnowledgeExists（KB 内去重）
  → FileService.SaveFile
  → repo.CreateKnowledge(parse_status=pending, enable_status=disabled)
  → tag relations
  → Asynq TypeDocumentProcess(DocumentProcessPayload)
  → 返回已持久化 Knowledge
```

核心实现证据：文件名/文件夹和合法性 [L25-L77](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L25-L77)，hash 去重/配额 [L79-L150](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L79-L150)，Knowledge 记录与 FileService/GORM 写入 [L152-L204](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L152-L204)，任务 payload 与入队 [L206-L274](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L206-L274)。

#### 5.3 URL 与手工 Markdown

- URL：Handler 先做 SSRF 校验；Service 再校验 URL、按 URL hash 去重，创建 pending Knowledge，再入相同 `TypeDocumentProcess`；远程文件 URL 会下载并落入存储后走文件解析。[Service L296-L463](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L296-L463)。
- 手工 Markdown：正文清洗、长度/标题/status 校验；draft 只保存在 Knowledge metadata；publish 入 `TypeManualProcess`，随后复用分块/索引逻辑；更新 publish 会先清旧资源。[Service L727-L847、L986-L1136](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L727-L1136)。

#### 5.4 解析、分块、索引与富化

```text
Asynq TypeDocumentProcess
  → knowledgeService.ProcessDocument
  → 载入 Tenant、Knowledge、KB，幂等/取消/删除守卫
  → convert（DocReader；file / file_url / URL）或直接 passage
  → ASR（音频，可选）与图片存储/URL 改写
  → Go chunker.Split 或 SplitParentChild
  → processChunks
      → 清旧 chunks/index/graph
      → parent/text/image 派生 Chunk 写 DB
      → RetrieveEngine.BatchIndex（父块不 embedding）
      → enable_status=enabled（此时已经可检索）
  → KnowledgePostProcessService.Handle
      → summary / question batch / graph chunk / wiki / auto-tag 任务
      → pending_subtasks_count 原子递减至 0
      → parse_status=completed
```

- Worker 的 retry、幂等状态和入口守卫见 [`ProcessDocument` L3149-L3250](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L3149-L3250)。
- file/file_url/URL/passage 分支、ASR 和图片处理见 [L3299-L3503](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L3299-L3503)。
- flat/parent-child 分块与调用 `processChunks` 见 [L3505-L3561](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L3505-L3561)。
- `processChunks` 先清旧投影，再组装 Chunk；父块只进 DB、子/平面文本块参与索引，见 [L243-L304、L370-L478](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L243-L478)。
- DB 写块、embedding 输入、索引阶段与失败状态见 [L476-L555](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L476-L555)。
- 官方架构文档给出完整序列和状态机，且说明 HTTP Handler 只落库/入队、耗时工作由 Asynq worker 执行。[入库架构 L22-L70](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/03-document-pipeline.md#L22-L70)、[端到端小结 L530-L538](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/03-document-pipeline.md#L530-L538)。

#### 5.5 混合检索

```text
POST /knowledge-bases/:id/hybrid-search
  → KnowledgeBaseHandler.HybridSearch
  → knowledgeBaseService.HybridSearch
  → 批量加载/逐 KB 鉴权 + embedding 模型一致性
  → 查询 embedding 只计算一次
  → 按 (vector store, owner tenant) 分组
  → retrieveFromStores 并发 fan-out + engine-aware normalization
  → 向量/关键词分类
  → 单路最高分去重，双路 weighted RRF
  → FAQ 特殊后处理、截断 match_count
  → processSearchResults 回读 Chunk/父块/上下文并返回
```

主流程和边界见 [`HybridSearch` L87-L255](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search.go#L87-L255)；RRF 公式与保留单路原始 score 的语义见 [`knowledgebase_search_fusion.go` L31-L141](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search_fusion.go#L31-L141)。

#### 5.6 更新与删除

- KB 更新只写允许的配置字段；`VectorStoreID` 模型用 `gorm:"<-:create"` 且更新 DTO 不包含该字段，形成双层不可变约束。[模型 L97-L103](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledgebase.go#L97-L103)、[更新 Service L487-L571](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase.go#L487-L571)。
- KB 删除先软删除记录、清分享/数据源/队列，再把 `KBDeletePayload` 放入 maintenance 队列；异步重清理 embeddings、Chunk、文件与图数据。即使 payload 序列化或 enqueue 失败，HTTP 删除仍不回滚已经软删的 KB，任务系统是 durable backstop 而不是数据库事务的一部分。[Service L698-L801](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase.go#L698-L801)、[`ProcessKBDelete` L804-L860](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase.go#L804-L860)。
- Knowledge 单删、批删、清空 KB 都收敛到 `KnowledgeListDeletePayload`；后台 `ProcessKnowledgeListDelete` 调用清理服务。代码入口位于 [`knowledge_delete.go` L491-L782](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_delete.go#L491-L782)。
- Chunk 编辑维护不可变 revision，并用 `expected_revision` 做乐观并发控制；索引重建失败不会丢数据库中的新正文，而是显式留下 `index_status=failed`。[官方 API L39-L58](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/04-api/02-api-chunks.md#L39-L58)。

### 6. 状态、任务与自愈语义

#### 6.1 Knowledge 状态机

| 状态 | 含义 |
| --- | --- |
| `pending` | 已创建，等待 worker。 |
| `processing` | DocReader、分块、embedding 或多模态主流程执行中。 |
| `finalizing` | 主索引已完成、已可检索，但摘要/问题/图谱/Wiki 等子任务未全部终结。 |
| `completed` | 主流程和全部富化子任务完成。 |
| `failed` | 本轮处理失败，`error_message` 记录原因。 |
| `deleting` | 正在删除，用于阻止异步任务竞争。 |
| `cancelled` | 用户取消；保留已产生的 Chunk/索引，可 reparse。 |

状态定义与精确注释见 [`internal/types/knowledge.go` L40-L82](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledge.go#L40-L82)。`enable_status=enabled` 与 `parse_status=completed` 不等价：索引一旦成功即可检索，富化未完成时仍是 processing/finalizing。[`finalizeIndexedKnowledgeState` L167-L200](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L167-L200)。

#### 6.2 Task 模型与执行模式

- `internal/types/task.go` 是队列定义与 payload 的事实来源：DocumentProcess、FAQ import、问题/摘要、KB copy/delete、Knowledge list delete/reparse/move、manual process、multimodal、postprocess、auto-tag 等都带 tenant 与资源 ID。[payload L271-L510](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/task.go#L271-L510)。
- 标准部署用 Asynq + Redis，Lite 模式用 drop-in 同步执行器；两边注册相同 handler，力求语义一致。[官方异步文档 L18-L31](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/05-async-tasks.md#L18-L31)。
- worker pool 分 core、postprocess、enrichment、maintenance、shared、wiki，分别隔离用户主流程、富化、长维护和 Wiki 负载。[官方异步文档 L84-L103](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/05-async-tasks.md#L84-L103)。
- 批量重解析 worker 会尝试全部条目；部分失败返回 `asynq.SkipRetry`，避免重试 wrapper 时再次破坏性重解析已成功提交的条目；失败条目留给用户显式重试。[实现 L3881-L3940](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L3881-L3940)。
- Housekeeping 以 `updated_at + span heartbeat + queue inspection` 三重判据识别卡死任务，避免误杀仍在排队或仍有心跳的任务；官方文档记录恢复到 failed 的行为。[入库文档 L446-L467](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/03-document-pipeline.md#L446-L467)。

### 7. 关键错误、安全与返回语义

| 场景 | 已核实语义 | 证据 |
| --- | --- | --- |
| 无效 JSON/参数 | Handler 生成 typed 400 AppError；Service 返回 AppError 时尽量原样透传，未分类 infra error 才包装 500。 | [KB create L357-L396](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase.go#L357-L396) |
| 重复文件/URL | KB 内基于文件 hash/URL hash 去重；返回既有 Knowledge 和 typed duplicate error，HTTP 注解为 409。 | [文件去重 L79-L108](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L79-L108)、[URL 去重 L337-L360](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L337-L360) |
| 文件已保存但任务入队失败 | 不删除已持久化 Knowledge；把 `parse_status=failed`、写错误并仍返回 Knowledge，调用方可 reparse。 | [L231-L259](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create.go#L231-L259) |
| URL 安全 | HTTP 入口和 worker 下载前均执行 SSRF 验证，以降低 DNS rebinding 风险。 | [Handler L496-L501](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L496-L501)、[Worker L3303-L3311](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process.go#L3303-L3311) |
| 批量请求 | batch delete/reparse 都限制 200，先验证所有 ID 存在并属于同一 KB，任一异常整批 400；worker 执行期的 reparse 部分失败则不重试已成功项。 | [删除 L1365-L1412](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L1365-L1412)、[重解析 L2715-L2767](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L2715-L2767) |
| 跨 KB 移动 | 源目标同租户、不同 ID、同类型、同 embedding；仅 completed；跨 store 只能 reparse，不能 reuse_vectors。 | [L2421-L2513](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledge.go#L2421-L2513) |
| Chunk 并发编辑 | `expected_revision` 冲突 409；数据库正文可成功而检索重建失败，此时 `index_status=failed` 显式暴露投影不一致。 | [Chunk API L39-L58](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/04-api/02-api-chunks.md#L39-L58) |
| 权限边界 | KB 生命周期使用 `manage_kbs`，内容写使用 `ingest`，读使用 `retrieve`；JWT 还叠加 Viewer/Contributor/Admin/owner 与 KB read/write/ownership 守卫。 | [路由 L57-L133、L182-L245](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/router/routes_knowledge.go#L57-L245) |

### 8. 数据与检索模型细节

- KB 配置不仅有 chunk size/overlap，还支持 parser engine rules、父子分块、adaptive strategy、token limit、language hints 与 table metadata instructions。[`ChunkingConfig` L243-L279](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledgebase.go#L243-L279)。
- 每次上传/重解析可用 `KnowledgeProcessOverrides` 覆盖 parser rules、chunking、multimodal、VLM、ASR、问题生成、图谱与 parser 参数；覆盖持久化在 Knowledge metadata，之后 reparse 可沿用。[模型 L3-L27](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledge_process.go#L3-L27)、[metadata merge L403-L450](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledge.go#L403-L450)。
- `Knowledge.Metadata` 保存内部摄取状态，`CustomMetadata` 保存用户描述性 metadata，两者刻意分离；后者参与摘要与文档级模型上下文。[模型 L172-L176](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/knowledge.go#L172-L176)。
- Chunk 类型覆盖 text、parent_text、image OCR/caption、summary、entity/relation、FAQ、table summary/column 和 wiki page；不同类型不是都参与同一种索引。[类型 L15-L40](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/chunk.go#L15-L40)。
- Chunk 同时保存 `Content`、不可变 parser `SourceContent`、`ContentRevision`、`IndexStatus` 与 prior revisions，可把“业务正文写成功”与“检索投影同步成功”分开观察。[模型 L126-L137、L180-L195](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/types/chunk.go#L126-L195)。
- 混合检索在多 KB 场景先验证 embedding 模型一致性，并按 store/owner tenant 分组；query embedding 只计算一次，避免 N store 重复调用模型。[检索 Service L91-L179](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledgebase_search.go#L91-L179)。

### 9. 测试证据

以下不是只凭文件名推断；测试函数名已在固定提交中核对。

| 测试文件 | 已覆盖行为 |
| --- | --- |
| [`knowledge_create_test.go` L130-L288](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_create_test.go#L130-L288) | 存储失败不落 Knowledge、成功持久化路径、DB 创建失败补偿删文件、process overrides 持久化。 |
| [`knowledge_batch_reparse_test.go` L68-L140](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_batch_reparse_test.go#L68-L140) | manual enqueue failure 可见；批量重解析部分失败与全成功语义。 |
| [`knowledge_process_status_test.go` L10-L100](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_process_status_test.go#L10-L100) | 可检索/富化状态分离和新 attempt 清旧错误。 |
| [`knowledge_finalize_test.go` L125-L340](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/repository/knowledge_finalize_test.go#L125-L340) | 并发 FinalizeSubtask 仅一次提升、计数不越界、状态写保护。 |
| [`knowledge_housekeeping_test.go` L152-L315](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_housekeeping_test.go#L152-L315) | abandoned/pending missing queue 恢复；有活跃 span/排队任务不误杀；queue probe error fail-safe。 |
| [`knowledge_move_gate_test.go` L17-L75](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/service/knowledge_move_gate_test.go#L17-L75) | 跨 vector store 的 reuse_vectors 拒绝和 store 共享判定。 |
| [`chunk_revision_test.go` L16-L120](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/application/repository/chunk_revision_test.go#L16-L120) | Chunk revision 保存原子性与乐观锁。 |
| [`knowledgebase_request_test.go` L22-L60](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase_request_test.go#L22-L60) | 更新 DTO 不得重新接收 `vector_store_id`。 |
| [`knowledgebase_hybrid_search_test.go` L51-L125](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/internal/handler/knowledgebase_hybrid_search_test.go#L51-L125) | 缺 query_text 拒绝、正常 query 接受、预计算向量例外。 |
| [`cli/cmd/doc/upload_test.go` L56-L330](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/doc/upload_test.go#L56-L330) | 上传成功、HTTP/409、路径安全、三态多模态、metadata 与 channel。 |
| [`cli/cmd/doc/upload_recursive_test.go` L65-L220](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/doc/upload_recursive_test.go#L65-L220) | 递归、glob、部分失败、无匹配、参数传播和 JSON batch envelope。 |
| [`cli/cmd/doc/wait_test.go` L90-L335](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/doc/wait_test.go#L90-L335) | 单/多文档完成、失败、超时、wait-all、draft 快速失败与 exit code。 |
| [`cli/cmd/search/chunks_test.go` L30-L230](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/cli/cmd/search/chunks_test.go#L30-L230) | 文本/JSON、空结果、limit 上限、禁用双通道错误、KB 解析、向量/关键词 flags。 |

官方 CLI 还记录真实服务器 E2E 闭环：`kb create → doc upload → doc wait → search → chat`，并用 cleanup 保证失败后删除临时 KB。[CLI 验收说明 L485-L504](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/05-clients/02-cli.md#L485-L504)。

### 10. 官方文档索引

- [知识库与 Knowledge API](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/04-api/02-api-knowledge.md)：KB 与 Knowledge 全量入口、参数和响应。
- [Chunk 与标签 API](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/04-api/02-api-chunks.md)：Chunk CRUD、revision、问题维护与 chunker preview。
- [知识检索 API](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/docs/api/knowledge-search.md)：不经 LLM 的 Chunk 检索请求/响应。
- [文档入库流水线](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/03-document-pipeline.md)：创建、存储、Asynq、DocReader、Chunk、索引、状态、删除和 FAQ 流程。
- [异步任务系统](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/02-architecture/05-async-tasks.md)：任务类型、队列、worker pool、重试、死信与两种执行模式。
- [官方 CLI](https://github.com/Tencent/WeKnora/blob/355d161d644793e6d7613ab34ef0aad610f9f9bf/website-docs/05-clients/02-cli.md)：命令、JSON envelope、exit code、MCP 子集与验收测试。

### 11. 值得与本地 MCP 对照的能力维度

| 维度 | WeKnora 基线 | 本地比较时应回答的问题 |
| --- | --- | --- |
| 对象分层 | KB → Knowledge → Chunk/Revision/Index | 本地 vault/page/raw source/passage/index 分别对应哪层？是否把容器操作误当笔记操作？ |
| 创建来源 | file、URL/file_url、manual Markdown、passage，另有 FAQ/data source 邻域 | 本地是否支持显式手工页、原始文件 ingest、URL ingest；哪些会创建正式页，哪些只创建证据/索引？ |
| 异步契约 | 先持久化、返回资源/任务 ID，随后 parse/index/finalize；有 wait/progress/spans | 本地是同步原子写还是异步；是否需要 task/status/wait，或简单同步反而更符合定位？ |
| 状态分离 | parse、enable/retrievable、summary、pending subtasks、chunk index status 分离 | 本地 freshness/stale/index 状态能否表达“正文成功、投影失败”与“已可检索、后处理未完”？ |
| 列表/详情 | 分页、多标签、来源、状态、时间、文件类型、目录过滤；单条详情与阶段 trace 分开 | 本地 list/get 是否有稳定分页/过滤/投影，是否泄漏内部路径？ |
| 检索 | metadata search 与 Chunk semantic/hybrid search 分开；跨 KB、knowledge_ids 限定、多 store、RRF、父块回捞 | 本地 query 是页级还是 passage 级；是否需要显式 lexical/vector/hybrid、source filter、debug/provenance？ |
| 更新与并发 | KB config、Knowledge metadata、manual content、Chunk content 分层；Chunk revision + CAS | 本地更新是否有 expected hash/revision；投影失败是否可观察、可重试？ |
| 删除/清空 | 单 Knowledge、批量、清 KB 内容、删 KB、单 Chunk、全 Chunk；重清理异步 | 本地 archive/restore/purge 是否比硬删更合适；每种删除对 raw、正式页、索引的边界是什么？ |
| 批处理 | 递归上传、batch delete/reparse/tag/move；入口整批校验，worker 部分失败不重放成功项 | 本地是否值得增加批处理；怎样保证幂等、部分失败与可恢复性？ |
| 配置作用域 | KB 默认 + 每次上传/重解析 overrides | 本地配置应放启动快照、vault 配置还是 tool 参数；哪些参数不应暴露给普通调用方？ |
| 存储/溯源 | Knowledge 保留 source/file/hash/path；Chunk 保留 offset/parent/revision；索引是派生投影 | 本地 raw snapshot、page source/source_hash、passage ID 与索引 manifest 是否形成更强可追溯性？ |
| 权限与安全 | tenant/RBAC/KB ownership/API-key capability/allow-list；SSRF、文件名/大小/类型校验 | 单用户本地 MCP 哪些不适用；路径穿越、URL SSRF、敏感 metadata、危险删除仍需哪些守卫？ |
| 可观测性 | audit activity、Langfuse retrieval span、processing span tree、task progress、housekeeping | 本地是否需要稳定 debug tool、projection warning、操作审计或仅结构化错误即可？ |
| CLI/MCP 边界 | REST/CLI 有写操作，但官方 CLI MCP 刻意只给精选只读工具 | 本地 MCP 是否应暴露所有维护操作；高风险能力是否转 CLI/显式确认/独立 profile？ |
| 测试与文档 | Service/Repository/Handler/CLI/E2E 多层契约测试，官方架构/API 文档同仓 | 本地每个工具是否有参数、错误、数据流、索引一致性和文档链接的成套测试？ |

优先对照项建议是：对象分层、写入与索引状态分离、metadata search 与正文检索分离、乐观并发/投影失败、批处理幂等、删除边界、MCP 高风险工具暴露策略。这些维度比简单比较工具数量更能避免把产品定位差异误判为缺陷。

### 12. Related specs（本地后续对照所需）

- `.trellis/spec/backend/knowledge-compilation.md`：本地 raw snapshot、正式 Wiki 页、source hash、显式写入与 ingest 不自动生成正式页的边界。
- `.trellis/spec/backend/local-vector-retrieval.md`：本地 passage、SQLite/JSONL projection、显式 build/update、query 零写入、hybrid RRF 与降级语义。
- `.trellis/spec/backend/archive-lifecycle.md`：本地 archive/restore/purge 与不可变 bundle，可用于判断 WeKnora 硬删除/软删除能力是否适用。
- `.trellis/spec/backend/error-handling.md`、`.trellis/spec/backend/database-guidelines.md` 当前仍为待补模板，不能作为现有稳定实现契约；最终文档应主要引用本地实际代码和测试。

## Caveats / Not Found

1. 本轮按父任务要求在证据足以支持规划后停止扩展搜索；未对所有 frontend 调用、FAQ/Wiki CRUD、数据源同步、临时文档、图片/音频的每个邻接功能逐一展开。它们存在于相同路由/Service 邻域，但不应据本文推断为已全面审计。
2. `/knowledge-search` 的公开参数/响应已用官方 API 文档核实，Handler 符号位置也已确认；未继续逐行追踪其内部到 `KnowledgeBaseService.HybridSearch` 的所有适配分支，因此本文只断言公开契约和 Chunk 返回层级，不断言所有内部调用细节。
3. 未穷尽 PostgreSQL、SQLite、Elasticsearch、OpenSearch、Qdrant、Milvus、Weaviate、Doris、Tencent VectorDB 等每个 RetrieverRepository 的 SQL/SDK 行为；本文检索结论停在公共 Service、fan-out、融合与结果组装层。
4. API 文档可能落后于源码。例如当前源码的单 Knowledge 删除已经返回异步 `task_id`。发生冲突时，应以本文锁定 SHA 的路由、Handler、Service 和测试为准，文档只作为辅助证据。
5. 官方 CLI 内建 MCP 明确是精选只读子集，但仓库同时存在独立 Python `mcp-server/`。本轮没有完成两者的产品主次、部署方式和工具契约差异审计；最终对比时不得把两者合并计数。
6. 未运行 WeKnora 服务、数据库迁移、Asynq/Redis、DocReader 或真实向量后端；状态、错误和性能结论来自固定提交的源码、测试与官方同仓文档，不是运行时实测。
7. `main` 会继续变化；本研究只代表访问日 2026-08-10 的 `355d161d644793e6d7613ab34ef0aad610f9f9bf`，最终交付必须沿用该 SHA，不能混入之后默认分支的新代码链接。
8. 本文件只研究外部 WeKnora；本地 MCP 的工具定义、实现链、工作树状态与逐项差距应由另一份本地研究材料完成，再在最终文档中合并。

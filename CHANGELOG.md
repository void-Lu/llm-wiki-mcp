# 变更记录

## 2026-09-29 — 检索上下文

- 可选 frontmatter `questions`（本页能回答的问题）进入检索：并入页面第一个 passage 的 FTS `aliases` 列。无 schema 变更；没有该字段的页面索引不变，已有 `questions` 的 vault 需要一次增量/全量重建才会生效。
- 查询结果正文按阅读顺序拼接同页 passage，并去掉相邻 chunk 的重叠（按 passage 单位比较，8–128 个单位）；同一次响应中已出现过的长段落（字母数字 ≥ 48，汉字计 2）在后续页面省略，结果页至少保留第一段正文。`PassageHit` 新增 `ordinal`（默认 -1），`ContextPassage` 新增可选 `ordinal`。排序不变，无需重建索引。

## 2026-09-29 — 向量嵌入文本与写入提示

- 向量索引改为嵌入“页面标题 + 标题路径 + passage 正文”（`embedding_text`；与标题相同的 H1 不重复），存储的 passage 文本与词法检索不变。向量记录的 content hash 同时覆盖嵌入文本，改标题或改小节标题会让对应向量变为 stale。向量索引 schema 升为 3，旧索引报告 `index_incompatible`，需要执行一次 `llm-wiki-mcp vector build`。
- `wiki_update(action="preview")` 也返回可选的 `link_suggestions`：按 apply 将写入的页面渲染结果（frontmatter 合并、标题行、脱敏）计算，页面自身的索引行替换为新标题/别名；同一 plan 的 preview 与 apply 返回相同提示（期间其他页面未变化时）。
- `wiki_write_note` 与 `wiki_update(action="apply")` 保存成功后返回可选的 `link_suggestions`：正文中提到其他 Wiki 页面标题/别名/多词文件名却未链接的位置（每个目标只报第一次，含 `target`、`title`、`mention`、`line`、`link`）。跳过代码、已有链接、页面自身、已链接目标、过短或指向多页的词；拉丁词按词边界、单词须大小写一致，中文按子串且长词优先。只读取已构建的检索投影，不改写页面；无提示时不出现该字段。
- 修复：没有 frontmatter `title` 的旧页面（标题来自正文 H1）经 `wiki_update` 纯正文更新后，标题被改成文件名（如 “Legacy Heading Title” → “legacy”）。现在按 frontmatter `title` → 原页面第一个 H1 → 文件名的顺序确定标题，并把该标题写入 frontmatter。
- `wiki_update` preview/apply：`incoming_body` 开头的 `# H1` 与将保留的页面标题不同时（该行会按原有行为被 frontmatter 标题替换），在 `warnings` 中返回 `title_heading_ignored: ...`，提示改用 `incoming_frontmatter.title`。标题行为不变。
- 修正默认 `schema.md` 模板（`DEFAULT_SCHEMA_TEXT`）对受控更新的描述，使其与代码一致：锁定字段为 `type`、`created`、`concept_id`、`entity_id`、`entity_type`、`source_path`、`source_hash`，`title` 不锁定（改名通过 `incoming_frontmatter.title`）；`incoming_frontmatter` 字段整体替换原值，数组不做去重合并。行为不变；已有 vault 的 `schema.md` 不会被改写。
- `wiki_write_note` 新增可选参数 `aliases: list[str] | None`：去空白、脱敏、大小写不敏感去重、去掉与标题相同项，≤ 20 项且每项 ≤ 120 字符（否则 `invalid_aliases`）；写入 frontmatter `aliases` 并参与新建时的重复标题检查。不传或结果为空时输出与此前逐字节相同。
- `duplicate_warnings` 每条新增可选的 `confidence`（`high`/`related`）：拼写变体与相同标题为 `high`；只多出整词/字（`title_contains`，或词集合互为真子集的 `similar_title`）为 `related`；`wiki/projects/` 下的新页面对 concept/knowledge 页的非相同标题匹配为 `related`。`high` 排前。
- `wiki_update` preview/apply 在 `incoming_frontmatter` 改动 `title`/`aliases` 时、`wiki_manage_shared_spec` upsert preview 在新建或改名时，对页面新增的标题/别名返回可选的 `duplicate_warnings`（排除页面自身；纯正文更新不返回）。
- `wiki_write_note` 新建页面后返回可选的 `duplicate_warnings`（最多 5 条，页面照常创建）：标题经 `ConceptRegistry.resolve` 与已有标题/别名归一化后相同（`same_title_or_alias`），或字符 bigram Jaccard ≥ 0.6（`similar_title`），或一方包含另一方且较短一方 ≥ 3 字符、占比 ≥ 0.5（`title_contains`）。`ConceptRegistry` 新增可直接传入记录的构造方式与 `resolve(..., collect_evidence=False)`（跳过 FTS 与全库 wikilink 扫描）。

## 2026-09-29 — 图扩展排序修复

- 排名版本升为 `query-v2-passage-rrf-11`；质量门控校准产物需按新版本重新生成（旧版本身份不匹配时按既有规则 fail-open）。
- 自然语言关系问题走 `wiki_relaxed` 恢复分支时也执行有界图扩展（active scope、同一快照/过滤边界/上限），不再只在 strict 种子阶段生效；同一请求内复用已构建的候选边界与图。
- 纯图候选不再统一截断到 0.75 后按路径排序：累计原始关系证据并以 `0.75 × E / (E + 3)` 单调映射，证据更强的页面排在前面；有词法/向量信号的候选仍按自身分数 15% 截断。
- frontmatter 类型关系参与图证据：`derived_from`（取 origin 的 `path`）权重 3.0、`related_objects` 权重 2.0，目标与 wikilink 一样按当次候选集解析到页面，并作为可遍历的一跳边；`applies_to` 是适用标签（`languages`/`frameworks`），不指向页面，只以 `key:label` 存储、不计分。检索库 schema 升为 4（边表中类型关系的解析字段变化），旧库需 `llm-wiki-mcp index build`。
- 纯图候选的上限改为相对种子：`0.7 × 最强贡献种子的相关分 × E / (E + 3)`（相关分取种子自身的 fusion/keyword/vector 最大值，不含图分），strict 与 relaxed 阶段一致；纯图页面始终低于把它带进来的种子，强种子的邻居可以越过弱词法候选。graph_v1 MRR@10 0.5057 → 0.5351、nDCG@10 0.5873 → 0.6189，R@1、direct R@1/MRR、无答案误命中、v2_40 与 CI 冒烟不变（详见 `tests/fixtures/retrieval/graph-eval-baseline.md`）。

## 2026-09-29 — 检索图边持久化

- 检索 store 新增 `links` 表（schema version 3）：wikilink、`sources` 来源边以及 `related_objects`/`applies_to`/`derived_from` 类型关系在页面投影时抽取，随 build、单页 update、rename 与 delete 同事务维护；类型关系暂只存储、不参与图评分。
- Query V2 图扩展改读持久化边，不再每次查询解析全部页面正文；wikilink 仍按当次已过滤候选集解析，图分数与排序保持逐位不变，边的 `source_hash` 与查询快照不一致时回退解析快照正文。
- 修复图扩展 `shared_source` 证据：查询快照把 frontmatter `sources` 列表冻结为 tuple 后被当作单个值比较，多来源页面之间无法匹配、标量与单元素列表也互不匹配；现在逐项比较来源。
- 升级后旧检索库报告 `index_incompatible`，写路径返回 `rebuild_required`，需要运行一次 `llm-wiki-mcp index build`；不做隐式迁移。

## 2026-09-20 — 规范镜像与公共规范维护

- 新增 core MCP 工具 `wiki_sync_specs` 与 `wiki_manage_shared_spec`（均为 preview/apply/discard 两阶段），core profile 从 9 个业务工具变为 11 个；README、CLAUDE.md 与 `.trellis/scripts/spec_lint.py` 的冻结集合同步。
- `wiki_sync_specs` 把调用方 `source_root` 指定的项目规范目录精确镜像到 `wiki/projects/<project>/specs/`（不假设 `.trellis/spec`）：源目录已删除的页面同步删除，不保留历史副本；`wiki_manage_shared_spec` 管理 `wiki/entities/shared-specs/<file>.md` 单页 upsert/delete，公共页必须声明 `applies_to`、可选 `conditions` 以及非空 `derived_from`（指向具体项目镜像页），并保留规则正文原文。
- formal adapter 新增统一 `delete` operation：页面删除由 `PageMutationCoordinator` 提交（`DELETED_PAGE_HASH` 哨兵、commit 时 unlink），依赖、检索、导航、overview 和审计投影按 formal profile 执行；投影失败按同一 `operation_id` 走 `repair()`，不重写页面、不使用 `project_existing`。
- 规范维护的预览计划是短期快照，只保存当前树指纹、CAS 哈希和 `page_path -> operation_id`，不保存审核状态和旧正文；每次导入重新审核。镜像与公共页正文先做 UTF-8 无 BOM 校验，再以原子写落盘。
- 规范写入保持与普通写入相同的索引边界：检索索引缺失返回 `rebuild_required`，不在 MCP 路径全量重建；`overview` 增量接受删除提示（`created=False`）只重写概览页，计数漂移继续由 `repair page-operation` 全量通道兜底。
- 新增用户级 skill `shared-spec-vault`（`sync`/`extract`/`import` 三个模式，逐项审核、不写审核记录），生成在仓库 `skills/` 下，安装需手动复制到用户 skills 目录。

## 2026-08-20 — 架构深化 r5 G1 恢复通道

- `repair page-operation` 重放遇到导航/overview 的已知结构缺口时，显式升级为无 hint 的全量投影，修复 G6 fail-loud 后原有 repair 死循环；首次页面写入仍保持 fail-loud。
- operation journal 安全记录升级标记和原始稳定错误码，增量失败提示改指向真实的 `repair page-operation` admin 命令；不新增普通 MCP 写入中的隐式全量检索重建。
- Query V2 收拢执行视图、请求投影选项、seed 统计和召回切片，`QueryTelemetry` 在数据库被外部重置后自动重建 schema 并重试；公共响应保持逐位不变。
- 初始化投影集的三文件判定统一由 `wiki_paths.py` 持有，navigation-first 的裸 vault 引导顺序改为显式契约并由回归断言守护。
- G4 收敛写路径：`wiki_update` 与 `wiki_write_note` 共用 `page_mutation.explain_stage`，移除 note 响应的死字段 `indexed: null` 和 chat 检索死回退；导航/概览生成页统一使用 `wiki_io.render_page`，概览也只在 UTF-8 字节变化时写入，增量 API 仅保留 `created` 提示。

## 2026-08-19 — discovery 资格与摄入投影防御收敛

- discovery 快照页面资格统一复用 `snapshot_page_eligible`，并清扫零消费兼容转口与校准 loader memo；既有公共结果行为保持不变。
- 摄入 profile 缺失投影阶段返回稳定 `projection_stage_missing` 错误，不再暴露裸 `KeyError`。

## 2026-08-19 — note_writer 接口与脱敏 owner 收缩

- 删除 `save_obsidian_note` 的无效内部参数与 overwrite 分支；页面脱敏和 `redacted_count` 统一由 `prepare_wiki_page` 计算并由 writer 透传，`wiki_write_note` 公共 schema 与响应字段不变。

## 2026-08-19 — Query V2 死代码与只读状态边界清扫

- 删除质量门禁与日志 operation index 的无调用方私有包装、评测死转口，并让评测/server 从各自 owner 导入查询类型与常量；G1 已落地的公开 report seam 与 MCP lexical adapter 保持不变。
- 修正查询取消诊断的 ranking/fallback 阶段标签；`wiki_status` 在没有查询时不再创建 `QueryExecutionRegistry`，只返回未初始化的执行量摘要。

## 2026-08-19 — KnowledgeDependencies 页面策略接口收敛

- 依赖投影内部 `update_page` 改为接收冻结 `PagePolicy`，并由存储边界统一展开 SQL 列与保留策略校验；正式页面、隐私审计和 provenance migration 不再重复展开五个策略字段。MCP 公开接口和表结构不变。

## 2026-08-19 — wiki_update 准备校验顺序统一

- 合并 `wiki_update` preview/apply 的 incoming 准备流水线，统一执行 `REMOVED_FIELDS`、sources、参考段/wikilink 归一化与校验、lifecycle 校验；同一非法 sources + inactive 页面现在返回一致的来源错误优先级。

## 2026-08-19 — Query V2 规则质量门禁接线

- 接通 vault 级可选校准 `artifact_path`：相对路径按 vault root 解析，shadow/enforce 共用一次校准视图；artifact 缺失或 policy 版本不匹配时 fail-open 保留候选并记录有界诊断，评测 engine 与 MCP 入口共用门禁设置。

## 2026-08-18 — Query V2 规则质量门禁

- 新增 Query V2 page-level 规则质量门禁，支持 runtime snapshot 下的 `off`、`shadow`、`enforce` 三态；shadow 不改变公共结果，enforce 全拒绝时 fail-open 返回 baseline 并记录 `gate_would_suppress_all`。
- 新增按 score family/分桶校准、阈值 backoff、质量门禁评测指标、ranking/config identity 与 JSON/Markdown 安全投影；评测 identity 或样本证据不足时保持 `unproven`。
- 当前 holdout 不满足正式放行条件，生产默认保持 `off`/`shadow`；不新增 `insufficient_evidence` 公共结果码，不改变 no-results、discovery-only、index unavailable、取消/超时语义。

## 2026-08-14 — 退役清理工具完成

- `wiki_ingest` 删除 `superseded_jobs`，保留 `stale_pages` 与 `generation`；`wiki_status` 删除 `queue`。
- 移除 `SupersedeRegistry`、`repair codegraph-removal` 清理命令、`wiki/sources` 一次性归档脚本及孤立 JS；真实 vault 已完成迁移，无需再次运行清理工具。
- ADR-0012 记录删除前提、公共契约收缩和对 ADR-0011 清理条款/cleanup 任务 PRD 字段闸门的 supersede 关系。

## 2026-08-13 — 移除 CodeGraph 摄入

- 删除 `wiki_codegraph_import`、CodeGraph 摄入包及其 retrieval/write policy 消费点；core MCP 工具从 10 个变为 9 个，`wiki_status` 不再返回 CodeGraph 字段。
- 新增 `repair codegraph-removal plan|apply`，可清理带完整 CodeGraph 标记的 architecture/archive 页面和 `raw/sources/projects/*/codegraph/` 目录；`architecture/` 目录本身、手工页面和其他 raw 内容保留。
- 升级后未清理的旧 CodeGraph 页面按普通 generated 页面处理，查询不再施加 `project_code` 专属隔离；旧 raw 文件按既有 raw scope 索引边界处理。清理后如有显式 vector index，需按返回的 `rebuild_required` 提示重建。

## 2026-08-12 — 架构整改与检索/写入管线收敛

- 统一 durable Markdown 写入：`atomic_write_text`、CAS、`PageMutationCoordinator` 和 page-operation journal 现在共同负责页面事实提交、幂等重试和可恢复投影；页面已提交但派生投影失败时返回 `repair_pending`，不重复创建页面。
- `wiki_write_note`、`wiki_update` 与 chat source 统一接入页面变更协调器；普通页面变更改为 RetrievalIndexStore 单页增量投影，导航与 overview 分离维护；索引缺失或不兼容时明确返回 `rebuild_required`，全量 build/update 仅通过 CLI/admin 显式执行。
- Query V2 增加不可变 `QueryCorpusSnapshot`、统一 `assemble_recovery` 装配边界和协作式取消检查，避免 discovery、回退、向量、图扩展与 context pack 在一次查询内读取漂移状态。
- retrieval evaluation 增加 `EvaluationRuntimeSnapshot` 与 `EvaluationQueryService`，engine、MCP 和 gold 评测复用同一服务边界；MCP 评测不再修改全局配置注册表，也不创建索引或写入 telemetry。
- 统一 `wiki_paths.safe_segment`/`slug` 路径与文件名 owner，并保留人工笔记旧文件名的显式兼容参数；不自动迁移或改名既有文件。
- 补充检索索引状态/构建/更新 CLI 文档、故障恢复边界、评测基线声明和 agent 约定。确定性测试与静态检查通过；真实 vault 生产基线仍需冻结数据集和 manifest 后单独验证。

## 2026-08-10 — 本地工具优化发布候选

- core MCP 工具集合固定为 10 个；新增 metadata-only `wiki_list` 与 opaque-reference `wiki_get`。
- 页面 provenance 使用真实 raw hash、CAS 和可重建 dependency projection；历史迁移改为 CLI/admin plan/apply。
- 新增 privacy audit plan/apply，默认阻断未审批的文件名与 wikilink 变化，支持 fault/CAS rollback。
- 查询增加协作式取消、有界并发、容量状态和 finish-once telemetry；不提供线程强杀承诺。
- error/logging/database 三份 Trellis spec 改为可执行规则，并增加 anchor/registry spec lint。
- P2-1 批量与 P2-4 URL 摄入继续延期：不注册 MCP 工具、不增加队列或网络依赖。

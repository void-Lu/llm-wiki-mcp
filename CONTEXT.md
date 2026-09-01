# LLM Wiki 知识生命周期

本上下文描述本地 LLM Wiki 中证据、正式知识、派生视图与公开定位边界。它用于避免把原始材料、人工整理后的知识和检索结果混称为“文档”。

## 知识对象

**原始来源（Raw Source）**：
由用户显式保存的证据快照；摄入原始来源不代表其内容已经成为正式知识。
_避免使用_：知识页、正式文档、来源胶囊

**原始来源树替换（Raw Source Tree Replacement）**：
以一份新的来源目录完整替换逻辑知识库中同一来源边界下的旧 Raw Source 集合；替换通过 Vault 外 staging 切换，最终旧来源不再留在 Vault 内，但正式知识页的来源引用和正文定位链接必须先完成确定性重绑定。历史 append-only 日志中的旧定位不回写。
_避免使用_：增量合并、仅刷新单个来源、未经校验的目录复制

**正式知识页（Formal Wiki Page）**：
经过人工或受控流程整理、可独立维护的知识单元；它可以声明所依据的原始来源。
_避免使用_：原始文档、分块、检索结果

**来源引用（Source Reference）**：
正式知识页对某个确切原始来源版本的声明，由公开定位符和内容身份共同组成。
_避免使用_：来源目录、来源胶囊、模糊来源

**正文 Raw 链接（Body Raw Link）**：
正式知识页正文中直接指向 `raw/sources/**` 的 Markdown 定位链接；它不是 Wiki 内部 `wikilink`，但在原始来源树替换导致定位符变化时必须按确定性映射更新，改写时只替换目标地址，不改变显示文本或正文语义。
_避免使用_：Wiki wikilink、来源引用、无身份的裸文件名

**来源验证状态（Provenance State）**：
正式知识页的全部来源引用能否对应到确切、可核验的原始来源版本；未验证状态不得被解释为来源不存在或内容已过期。
_避免使用_：新鲜度、正确性、权威性

**内容范围（Content Scope）**：
公共读取工具一次进入的一个物理内容范围，只能是 `active`、`raw` 或 `archive`；默认 `active`，不存在跨范围的 `all` 聚合。
_避免使用_：全库、混合目录、跨库搜索

**目录项（Catalog Item）**：
内容目录中的一个带类型对象，只能是 `formal_page`、`raw_source` 或 `archived_page`；对象类型不得仅从路径或范围名称猜测。
_避免使用_：无类型文档、搜索结果、数据库行

**内容目录（Content Catalog）**：
某个确切内容范围内目录项及其生命周期元数据的可枚举视图；它不读取正文、不按相关性排序，也不跨物理范围聚合。
_避免使用_：全文搜索、相关性查询、知识库搜索、页面目录

**内容引用（Content Reference）**：
公共读取工具用于精确标识某个目录项及其内容范围的稳定引用；它不暴露宿主机物理路径，也不能脱离绑定范围重新解释。
_避免使用_：绝对路径、模糊查询、数据库行号、裸相对路径

**正文预算（Body Budget）**：
一次精确读取允许返回的最大正文字数；服务端配置给出硬上限，调用方只能主动缩小，不能通过请求扩张。传入 `max_total_bytes` 时服务端隐式启用正文读取并按读取游标顺序自动续读拼接：默认总量 64 KiB、硬上限 256 KiB、单页仍受页预算约束；超过硬上限时确定性 clamp 并返回 `total_body_budget_clamped` 警告，省略该参数保持单页行为。
_避免使用_：完整文件保证、无限上下文、调用方上限

**读取游标（Read Cursor）**：
服务端签发的 opaque 续读位置，绑定内容范围、筛选条件或内容版本；绑定状态变化后必须返回 `cursor_stale`，不能在新状态上静默续读。
_避免使用_：页码、裸偏移量、永久书签

**查询期限（Query Deadline）**：
一次查询允许继续启动工作的最晚单调时钟时点；它必须传播到查询阶段和外部 provider，而不只是限制调用方等待结果的时长。
_避免使用_：等待超时、线程终止、墙上时钟截止日期

**协作式取消（Cooperative Cancellation）**：
各查询阶段主动观察期限并停止安排后续工作的取消保证；它不声称能够强制杀死已经进入不可取消阻塞调用的线程。
_避免使用_：强制终止、进程隔离、仅停止等待

**查询语料快照（QueryCorpusSnapshot）**：
`src/retrieval/query_snapshot.py` 为一次查询捕获的不可变 active/raw 页面 metadata、provenance 与候选视图；同一调用的过滤、向量、图扩展、发现和 fallback 必须复用对应快照，不重新读取可能漂移的检索 store。
_避免使用_：实时 store、全文正文、查询执行上下文

**召回策略（Query Recall Policy）**：
`src/retrieval/query_recall_policy.py` 是 Query V2 召回与回退启发式策略的唯一 owner，持有意图分类、查询扩展、relaxed/raw 候选生成、coverage 合并、自适应扩展和结构化步骤计数；它只复用检索 store、不可变快照、协作式取消和 recovery primitive，不持有可变执行状态或 outcome 冻结逻辑。
_避免使用_：查询执行上下文、查询流水线编排、重复实现回退策略

**查询回退（Fallback）**：
`src/retrieval/query_recovery.py` 中的回退计划、条件、阶梯、每页选择和 `assemble_recovery` 负责主召回不足时的有界分支选择及最终 fallback envelope 装配；召回候选和 coverage/扩展启发式由 `query_recall_policy.py` 提供，recovery 不持有查询 store、取消器或执行上下文的可变状态。
_避免使用_：全库扫描、重建索引、查询执行上下文、策略副本

**查询执行上下文（QueryExecutionContext）**：
`src/retrieval/query_execution_context.py` 中的 `QueryExecutionContext` 是一次 Query V2 调用的可变执行状态 owner，持有本次查询的 root、检索 store、取消状态、执行 status，以及按需创建并记忆化的 raw store 与 raw snapshot；`QueryRequestView` 是由 pipeline 构造的冻结请求值对象，承载 question、public/effective scope、filters、top_k、metadata、intent、effective RRF scale、投影选项及 discovery/batch 所需的请求配置。pipeline 只调用公开的 `execute(request_view)`，context 在其中封装 fallback → discovery → batch 的顺序，召回策略委托给 `query_recall_policy.py`。raw 分支把 raw index 规范化为 `fresh`、`stale`、`missing`，在 `coverage`、`all_coverage` 和 `raw_zero` 分支中迁移 selected、recovery/context_items、命中计数、warning、词法模式与 coverage 标记；非 fresh 状态使用空 raw snapshot，不伪造 raw 候选。公共投影在 `outcome()` 封存前完成，`outcome()` 随后只发布一次递归冻结的执行视图只读形态并封存上下文；它不是公开响应对象本身。
_避免使用_：召回/扩展启发式、紧凑正文、context pack、检索结果

**Context Pack（紧凑正文）**：
从已选 passage 投影出的有界正文集合与 token budget，供模型阅读；它只描述要发送的内容，不持有查询 store、取消器、raw 可用性或阶段编排状态。
_避免使用_：查询执行上下文、执行状态、完整页面

**目录发现（Catalog Discovery）**：
`src/retrieval/discovery.py` 是 Query V2 正交检索分支的纯发现 owner，从查询语料快照出发按锚点、通配符与别名模式有界枚举未解析实体与证据目录项；它产出冻结的发现结果，不持有执行状态、检索 store 或取消器。公共投影只暴露有界摘要（`candidate_entities` 至多 40 项、整个 discovery JSON 投影不超过 128 KiB，附 `total_count`/`returned_count`/`truncated`），内部候选全集仅供 batch/confirmation 消费；候选延续由 batch `continuation_token` 负责，不提供 discovery 候选 cursor。
_避免使用_：全库扫描、枚举工具、实时目录读取

**实体批处理（Entity Batch）**：
`src/retrieval/entity_batch.py` 是服务于目录发现的纯实体批处理 owner，把逐实体的批查询组织为带 token 与指纹缓存的有界执行机制；它不直接面向公共查询结果。
_避免使用_：并发扇出、批量重试、目录发现

**校准产物（Calibration Artifact）**：
CLI `quality-gate calibrate` 从冻结评测数据集生成的 branch-relative 阈值产物，身份由 dataset manifest 冻结；运行时经 vault 配置 `quality_gate.artifact_path`（vault 相对解析，绝对路径按原值）定位。产物缺失、policy_version 或身份不匹配时 fail-open（一律 keep）并携带诊断，不得静默降级为「无门禁」。
_避免使用_：默认阈值表、全局阈值配置、无门禁模式

**阈值视图（Threshold View）**：
校准产物在运行时的 per-feature 阈值投影，供质量门禁按分支/桶消费；无产物时视图为空，门禁判定一律 keep。它是校准产物的运行时读取形态，不持有校准生成、评测数据集或门禁决策本身。
_避免使用_：校准产物、阈值配置、门禁策略副本

## 派生与生命周期

**页面提交（Page Commit）**：
正式知识页的新版本成为知识事实的时点；页面提交与其派生投影完成更新是两个不同状态。
_避免使用_：索引完成、全事务提交、投影刷新

**修复待处理（Repair Pending）**：
页面已经提交，但至少一个派生投影尚未成功收敛的状态；它不表示页面写入失败。普通页面写入遇到导航/overview 结构缺口时继续 fail-loud 并保留该状态；CLI/admin 的 `repair page-operation` 重放可对这两类已知结构错误显式升级为无 hint 的全量导航/overview 重建，升级原因写入 operation journal。该升级不等于检索索引全量重建，也不会被藏入普通 MCP 写入。
_避免使用_：写入失败、页面回滚、未提交

**更新计划（Update Plan）**：
服务端签发的限时、单次使用变更许可，将某个页面基线版本绑定到一项确切的结构性更新意图。
_避免使用_：内容摘要、页面 hash、无限期 token

**维护计划（Repair Plan）**：
CLI/admin 边界签发的限时、单次使用批量维护许可，绑定页面集合与各页基线 hash，经 CAS 逐页执行并留下可回滚的审计记录；它与更新计划、归档计划介质和生命周期不同，互不通用。计划/审计与逐页 CAS、补偿回滚骨架由 `src/wiki/repair_plan.py` 唯一持有；`src/wiki/provenance_migration.py` 与 `src/wiki/privacy_audit.py` 只注入领域分类和写入/投影回调，admin 路径由 `src/wiki/wiki_paths.py:admin_wiki_page_file` 持有。
_避免使用_：更新计划、归档计划、无限期批量许可

**派生投影（Derived Projection）**：
从原始来源或正式知识页重建的检索、导航、依赖或状态视图；它不是知识事实本身。
_避免使用_：事实源、原始数据、正式知识

**Raw 辅助文件（Raw Auxiliary File）**：
随原始来源树保存、用于来源目录管理或历史留存、但不作为检索证据参与 Raw RetrievalIndexStore 的文件，例如 manifest 和 deprecated archive；它们仍属于 Raw 的保存范围，可由 Raw catalog 精确读取，不等同于正式知识页。
_避免使用_：可检索正文、正式来源引用、归档 Wiki 页面

**日志卷宗（Log Volume）**：
`src/wiki/log_volume.py` 是正式知识页日志的有界物理布局 owner，持有 UTF-8 字节分卷、轮转阈值、月度归档卷、`archives/log.md` 追加、归档索引与逐文件原子写；它不负责条目语义、脱敏或 operation journal。`src/wiki/wiki_log.py` 只负责日志条目渲染、脱敏、journal 去重与 operation index。
_避免使用_：跨文件事务、日志条目语义、脱敏策略、operation journal

**投影 profile（Projection Profile）**：
某类字节变更必须追上的有序派生投影清单，按变更 kind（正式页面提交、raw source 摄入、chat source、admin 改写、归档）区分；它是"字节变了要追什么投影"的唯一答案来源，不持有投影执行器、operation journal 或修复状态。
纯 registry 与阶段别名由 `src/wiki/projection_profile.py` 持有；page mutation 通过 `src/wiki/page_mutation_adapters.py` 的 formal/chat adapter 引用对应 profile，并在 adapter 内绑定路径、请求键和本地执行器；ingest、archive、provenance/privacy admin 仍各自绑定阶段，不跨边界共享执行状态。
_避免使用_：投影执行器、投影状态机、全量重建指令

**新鲜度（Freshness）**：
正式知识页相对于其所声明来源版本的当前一致性状态；它不表示页面内容本身的质量或权威性。
_避免使用_：来源验证状态、正确性、可信度、生命周期

**页面策略（Page Policy）**：
`src/wiki/page_policy.py` 中 frozen 的 `PagePolicy` 与纯函数 `derive_page_policy`/`derive` 是 frontmatter 读侧派生策略的单一 owner；写侧 `stamp_page_policy`（`stamp` 为兼容短别名）统一产生可持久化的 `freshness` 与 `provenance_unverified`。stamp 只校验而不接管调用方拥有的 `maintenance`/`replaced_by`，该模块不执行 I/O。非法 `lifecycle` 不向正式页面投影抛出异常，而是归一为 `review_required`；依赖存储层自己的 fail-closed 校验仍保持不变。
_避免使用_：页面提交、操作状态、索引状态

**PlanLifecycle**：
`src/wiki/page_mutation.py` 内部的 `PlanLifecycle` 是 durable `Update Plan` 的规则门面，负责 `issued`、`claimed`、`consumed`、`expired` 的解析，以及 claim/consume 与页面 operation 之间的协调和稳定结果映射。它与 `PageOperation` 的 operation 状态机正交：plan lifecycle 控制变更许可、过期和幂等重放，operation 状态机记录页面事实提交、投影与修复（`prepared`、`page_committed`、`repair_pending`、`completed`、`failed_precommit`、`conflict`）；两者不能合并成一个状态字段或由调用方各自重建。
_避免使用_：页面操作状态机、页面事实、无限期 token

## 边界与定位

**展示信息（Display Metadata）**：
面向人或模型阅读的标题、摘要、标签及类似内容；其可见性由逻辑知识库的隐私策略决定。
_避免使用_：定位符、内容身份、内部诊断

**身份字段（Identity Field）**：
必须保持精确值才能定位对象或验证完整性的字段；它接受校验或拒绝，但不接受通用文本脱敏改写。
_避免使用_：展示信息、自由文本、可脱敏 metadata

**逻辑知识库（Logical Vault）**：
一个命名的本地知识边界，关联其内容、策略和生命周期；它不是可由普通工具动态创建的 SaaS Knowledge Base。
_避免使用_：租户、Knowledge Base 记录、绝对目录

**进程读取边界（Process Read Boundary）**：
摄入流程可选择服务器进程能够读取的任意普通文件；逻辑知识库、workspace 或 MCP roots 不再形成额外的来源授权边界。
_避免使用_：可信读取根、来源 allowlist、workspace 授权

**公开定位符（Public Locator）**：
在公共工具契约中标识内容的位置，由逻辑知识库和知识库内相对路径组成，不包含宿主机绝对路径。
_避免使用_：绝对路径、物理数据库路径、用户主目录路径

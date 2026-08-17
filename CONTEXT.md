# LLM Wiki 知识生命周期

本上下文描述本地 LLM Wiki 中证据、正式知识、派生视图与公开定位边界。它用于避免把原始材料、人工整理后的知识和检索结果混称为“文档”。

## 知识对象

**原始来源（Raw Source）**：
由用户显式保存的证据快照；摄入原始来源不代表其内容已经成为正式知识。
_避免使用_：知识页、正式文档、来源胶囊

**正式知识页（Formal Wiki Page）**：
经过人工或受控流程整理、可独立维护的知识单元；它可以声明所依据的原始来源。
_避免使用_：原始文档、分块、检索结果

**来源引用（Source Reference）**：
正式知识页对某个确切原始来源版本的声明，由公开定位符和内容身份共同组成。
_避免使用_：来源目录、来源胶囊、模糊来源

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
一次精确读取允许返回的最大正文字数；服务端配置给出硬上限，调用方只能主动缩小，不能通过请求扩张。
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

**查询回退（Fallback）**：
`src/retrieval/query_recovery.py` 中的回退计划、条件和 `assemble_recovery` 负责主召回不足时的有界分支选择及最终 fallback envelope 装配；它不持有查询 store、取消器或执行上下文的可变状态。
_避免使用_：全库扫描、重建索引、查询执行上下文

**查询执行上下文（QueryExecutionContext）**：
`src/retrieval/query_execution_context.py` 中的 `QueryExecutionContext` 是一次 Query V2 调用的可变执行状态 owner，持有本次查询的 root、检索 store、取消状态、执行 status，以及按需创建并记忆化的 raw store 与 raw snapshot；它承接 fallback、发现和 batch 阶段的状态迁移。raw 分支把 raw index 规范化为 `fresh`、`stale`、`missing`，在 `coverage`、`all_coverage` 和 `raw_zero` 分支中按候选结果迁移 selected、recovery/context_items、命中计数、warning、词法模式与 coverage 标记；非 fresh 状态使用空 raw snapshot，不伪造 raw 候选。`outcome()` 只发布一次递归冻结的 `QueryExecutionOutcome` 只读视图，并在发布后封存上下文，不是公开响应对象本身。
_避免使用_：紧凑正文、context pack、检索结果

**Context Pack（紧凑正文）**：
从已选 passage 投影出的有界正文集合与 token budget，供模型阅读；它只描述要发送的内容，不持有查询 store、取消器、raw 可用性或阶段编排状态。
_避免使用_：查询执行上下文、执行状态、完整页面

**目录发现（Catalog Discovery）**：
Query V2 的正交检索分支，从查询语料快照出发按锚点、通配符与别名模式有界枚举未解析实体与证据目录项；它产出冻结的发现结果，不持有执行状态、检索 store 或取消器。
_避免使用_：全库扫描、枚举工具、实时目录读取

**实体批处理（Entity Batch）**：
把逐实体的批查询组织为带 token 与指纹缓存的有界执行机制，服务于目录发现；它不直接面向公共查询结果。
_避免使用_：并发扇出、批量重试、目录发现

## 派生与生命周期

**页面提交（Page Commit）**：
正式知识页的新版本成为知识事实的时点；页面提交与其派生投影完成更新是两个不同状态。
_避免使用_：索引完成、全事务提交、投影刷新

**修复待处理（Repair Pending）**：
页面已经提交，但至少一个派生投影尚未成功收敛的状态；它不表示页面写入失败。
_避免使用_：写入失败、页面回滚、未提交

**更新计划（Update Plan）**：
服务端签发的限时、单次使用变更许可，将某个页面基线版本绑定到一项确切的结构性更新意图。
_避免使用_：内容摘要、页面 hash、无限期 token

**维护计划（Repair Plan）**：
CLI/admin 边界签发的限时、单次使用批量维护许可，绑定页面集合与各页基线 hash，经 CAS 逐页执行并留下可回滚的审计记录；它与更新计划、归档计划介质和生命周期不同，互不通用。
_避免使用_：更新计划、归档计划、无限期批量许可

**派生投影（Derived Projection）**：
从原始来源或正式知识页重建的检索、导航、依赖或状态视图；它不是知识事实本身。
_避免使用_：事实源、原始数据、正式知识

**投影 profile（Projection Profile）**：
某类字节变更必须追上的有序派生投影清单，按变更 kind（正式页面提交、raw source 摄入、chat source、admin 改写、归档）区分；它是"字节变了要追什么投影"的唯一答案来源，不持有投影执行器、operation journal 或修复状态。
_避免使用_：投影执行器、投影状态机、全量重建指令

**新鲜度（Freshness）**：
正式知识页相对于其所声明来源版本的当前一致性状态；它不表示页面内容本身的质量或权威性。
_避免使用_：来源验证状态、正确性、可信度、生命周期

**页面策略（Page Policy）**：
`src/wiki/page_policy.py` 中 frozen 的 `PagePolicy` 与纯函数 `derive_page_policy` 是 frontmatter 派生策略的单一 owner，统一产生 `freshness`、`maintenance`、`lifecycle`、`generated` 与 `replaced_by`；该模块不执行 I/O。非法 `lifecycle` 不向正式页面投影抛出异常，而是归一为 `review_required`；依赖存储层自己的 fail-closed 校验仍保持不变。
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

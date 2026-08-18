---
status: accepted
date: 2026-08-10
---

# 内容枚举与精确读取使用两个窄工具

稳定内容发现能力由 `wiki_list` 与 `wiki_get` 两个公共工具提供，而不是一个带 `list|get` 动作的宽工具，也不继续让相关性检索承担管理查询。`wiki_list` 永久只返回 metadata；`wiki_get` 只按精确内容引用读取，并单独执行正文预算、范围校验和隐私策略。

## 后果

- 两个工具分别拥有强类型 input/output schema；list 的 filter/cursor 与 get 的 content reference/body budget 不形成条件可选字段超集。
- `wiki_query` 继续负责 relevance、citation、graph/fallback 和上下文预算，不增加稳定分页或完整枚举语义。
- 两个工具共享同一个内容目录领域服务和 metadata filter 所有者，P1-2 与 P2-2 不得实现为两套 read model。
- 实施验证必须比较窄工具与 action 工具的模型选择率、错参率、工具 token 和完成 update/archive preflight 的调用数；当前没有 A/B 数据，不把模型效果提升写成既定事实。
- 两个工具首版直接进入 core，公共工具数由 8 增至 10，不设置临时 read-only/extended profile；模型工具选择 A/B 评测是发布门槛，未通过则回到工具描述与接口设计阶段，而不是带病发布。
- `store_scope` 只允许 `active | raw | archive`，默认 `active`；每次调用只打开一个物理范围，不提供 `all` 或隐式跨范围聚合。
- `wiki_list` 返回显式 `object_kind`（`formal_page | raw_source | archived_page`）和绑定范围的稳定 `content_ref`；`wiki_get` 不接受脱离该范围重新解释的裸路径。
- `wiki_get` 默认只返回完整 metadata；正文必须显式设置 `include_body=true`，受服务端硬上限约束，调用方的 `max_chars` 只能调低该上限。
- 正文续读游标绑定 `content_ref`、完整内容 hash 与下一偏移量，内容变化后返回 `cursor_stale`；列表采用稳定 keyset 顺序，游标绑定 schema、scope、筛选条件和目录指纹，目录状态变化后同样显式失效。

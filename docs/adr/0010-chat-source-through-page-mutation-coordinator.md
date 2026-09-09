---
status: accepted
date: 2026-08-12
---

# Chat source 通过页面变更协调器提交和修复

chat source 仍属于 `raw/sources/chat/**` 的原始来源，不转换为 formal wiki page；但它的 durable revision、投影阶段、审计、幂等重试和故障恢复统一使用 `PageMutationCoordinator` 的 journal/lock/repair 模型。协调器扩展 source-specific projection profile，以复用状态机而不是复制第二套写入管线。

## 选择理由

`ChatMemoryService` 原来拥有自己的 temp + replace、日志 append 和全量索引刷新流程，缺少 overview、依赖 freshness 处理和可恢复的阶段 journal。另一方面，`PageMutationCoordinator` 已经实现页面事实先提交、投影失败进入 `repair_pending`、按阶段重放和 operation ID 去重。直接复用其深模块可以关闭 chat source 的新鲜度缺口，同时保留 raw/formal 领域边界。

## 后果

- chat source 的 source commit 与 formal page commit 共享稳定的 operation、CAS、repair 和审计语义，但 raw source 不会被写入 formal-page dependency row。
- source projection 在 commit 后通过 `KnowledgeDependencies.source_changed` 标记引用该来源的 formal page；生成页转为 `stale`，人工页转为 `review_required`，遵循既有 freshness 规则。
- 不适用的 formal-page projection 由 profile 返回安全的 `not_applicable` 结果，不创建第二个协调器或第二套 journal。
- 页面事实仍不因投影失败回滚；普通 MCP 只返回安全的 repair 描述，实际 repair 继续使用现有维护边界。

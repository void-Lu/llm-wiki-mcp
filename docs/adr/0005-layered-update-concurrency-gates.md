---
status: accepted
date: 2026-08-10
---

# 页面更新采用分层并发门

`wiki_update(action="apply")` 的纯正文更新必须提供完整页面 `expected_hash`；任何 frontmatter、sources 或结构性引用变更还必须提供由 preview 签发的持久 `plan_id`。所有路径都在原子替换前重新检查完整页面 hash；更新计划绑定 base hash 与规范化后的确切变更意图，并具有期限和单次消费语义。

## 后果

- 不带必要并发门的旧 apply 调用在下一次发布直接失败，不提供 legacy 执行模式。
- 持久 plan 使用随机 opaque ID，不能继续把可自行计算的确定性摘要当作已审阅能力；同一 plan 的并发消费只有一个成功。
- 页面已经提交后 plan 即视为 consumed；派生投影失败进入 ADR-0004 的 `repair_pending`，重试 repair 不得重新写页面或再次消费 plan。
- 纯正文更新避免强制 preview 往返，但调用方仍须先取得当前完整页面 hash。
- 并发保证采用有界的协作式 CAS：本工具写入者被串行化，并在原子替换前最后一次复核完整页面 hash；检测到变化立即拒绝。Obsidian 等不遵守本工具锁的外部编辑器仍可能在复核与替换之间竞争，公共文档不得将该模型描述为跨进程强 CAS。
- 默认不生成 conflict artifact；若最终 hash 不匹配，原页面保持不变并返回稳定冲突结果。

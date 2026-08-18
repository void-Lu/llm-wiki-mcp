---
status: accepted
date: 2026-08-10
---

# 页面提交不因派生投影失败而回滚

正式知识页以原子替换完成提交后即成为事实；dependency、retrieval、navigation、overview 或 audit log 的失败不会回滚已提交页面。页面变更使用小型持久 operation journal 记录提交点和逐阶段状态，返回 `ok=true, state=repair_pending`，再通过幂等 repair 使派生投影收敛。

## 后果

- 只有页面提交前的失败返回 `ok=false`；页面已经提交时，调用方不得因投影失败盲目重试整个 create/update。
- Journal 只保存 operation ID、逻辑路径、base/intended hash、阶段状态和稳定错误码，不保存正文或敏感 metadata。
- Repair 重放投影和审计阶段，不重写事实页；普通 MCP 只返回 repair 描述，实际 repair 先保留在 CLI/admin 维护边界。
- 复用 archive 的 reducer、故障注入和恢复原则，但不复用 archive 专属表、bundle 状态或回滚模型。

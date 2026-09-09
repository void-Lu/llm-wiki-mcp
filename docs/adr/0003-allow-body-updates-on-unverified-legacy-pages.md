---
status: accepted
date: 2026-08-10
---

# 允许未验证旧页面继续进行纯正文更新

新建正式知识页或显式变更 `sources` 时，所有来源必须是可验证的原始来源并记录确切内容身份；任一来源无效都会拒绝整次写入。对于历史上缺少 `source_hashes` 的页面，纯正文更新仍可执行，但页面必须保持 `provenance_unverified` 与 `review_required`，不得因一次正文保存而被标记为来源已验证或 fresh。

## 后果

- 旧页面不必在集中迁移完成前完全冻结，但任何来源字段变化都必须先通过新的严格来源解析。
- `provenance_unverified` 是显式业务状态，不是 warning 后继续写空 hash；依赖投影和公共状态必须保留该不确定性。
- CLI/admin 提供 provenance migration 的 plan/apply；缺失来源、hash 不匹配或页面 CAS 漂移都必须进入人工复核，不能猜测或静默修复。
- 纯正文更新不得改写或补造旧来源身份，也不得把未验证页面自动提升为 fresh。

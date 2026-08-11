# 变更记录

## 2026-08-10 — 本地工具优化发布候选

- core MCP 工具集合固定为 10 个；新增 metadata-only `wiki_list` 与 opaque-reference `wiki_get`。
- 页面 provenance 使用真实 raw hash、CAS 和可重建 dependency projection；历史迁移改为 CLI/admin plan/apply。
- 新增 privacy audit plan/apply，默认阻断未审批的文件名与 wikilink 变化，支持 fault/CAS rollback。
- 查询增加协作式取消、有界并发、容量状态和 finish-once telemetry；不提供线程强杀承诺。
- error/logging/database 三份 Trellis spec 改为可执行规则，并增加 anchor/registry spec lint。
- P2-1 批量与 P2-4 URL 摄入继续延期：不注册 MCP 工具、不增加队列或网络依赖。


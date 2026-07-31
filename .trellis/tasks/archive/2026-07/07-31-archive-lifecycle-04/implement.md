# 归档生命周期与恢复：实施计划

## 实施步骤

- [x] 1. 定义 archive manifest、plan、operation、event、tombstone 类型和稳定错误码。
- [x] 2. 实现顶层 archives 路径、不可变 bundle 布局、稳定 YAML/hash 和 containment 校验。
- [x] 3. 实现 archive operation SQLite schema、单一状态 reducer、幂等 resume/rollback/recovery。
- [x] 4. 实现 planner 的 lifecycle、人工页、反向引用、依赖闭包和 dry-run。
- [x] 5. 实现 staging/pending/detaching/final commit 与 active/archive index coordinator。
- [x] 6. 在每个状态转换加入 crash/fault injection，证明活动文件与索引可恢复。
- [x] 7. 实现 restore 的 hash/路径/冲突检查、活动 index 更新和历史事件。
- [x] 8. 实现显式/retention-policy purge、archive index/cache 清理和合规 tombstone。
- [x] 9. 实现 legacy migration dry-run/apply：queries 无内容迁移、chatlog 到 raw chat legacy、chat source-index 删除、wiki archives log 到顶层。
- [x] 10. 移除旧 archives/chatlog/queries path helper 和 runtime compatibility scan，更新 init/schema/navigation/log。
- [x] 11. 增加 `wiki_archive` / `wiki_restore` plan/apply MCP façade，并用 plan ID/expected hashes 防止 TOCTOU。
- [x] 12. 增加 CLI/admin archive status/recover/purge/migrate；普通 MCP 不暴露这些入口。
- [x] 13. 将 archive status 接入 `wiki_status`，archive search 接入 `wiki_query(scope=archive)`。
- [x] 14. 删除 `wiki_delete_source` public wrapper，并更新 README、迁移指南和 backend archive-lifecycle spec。

## 预计修改/新增文件

- `src/netsuite_llm_wiki_mcp/archive_models.py`
- `src/netsuite_llm_wiki_mcp/archive_manifest.py`
- `src/netsuite_llm_wiki_mcp/archive_planner.py`
- `src/netsuite_llm_wiki_mcp/archive_service.py`
- `src/netsuite_llm_wiki_mcp/archive_migration.py`
- `src/netsuite_llm_wiki_mcp/wiki_delete.py`
- `src/netsuite_llm_wiki_mcp/knowledge_dependencies.py`
- `src/netsuite_llm_wiki_mcp/retrieval_index.py`
- `src/netsuite_llm_wiki_mcp/wiki_paths.py`
- `src/netsuite_llm_wiki_mcp/wiki_files.py`
- `src/netsuite_llm_wiki_mcp/wiki_index.py`
- `src/netsuite_llm_wiki_mcp/wiki_log.py`
- `src/netsuite_llm_wiki_mcp/wiki_lint.py`
- `src/netsuite_llm_wiki_mcp/server.py`
- `src/netsuite_llm_wiki_mcp/cli.py`
- `tests/test_archive_manifest.py`
- `tests/test_archive_planner.py`
- `tests/test_archive_service.py`
- `tests/test_archive_recovery.py`
- `tests/test_archive_migration.py`
- `tests/test_wiki_delete.py`
- `tests/test_server_tools.py`

## 验证

```powershell
rtk pytest -q tests/test_archive_manifest.py tests/test_archive_planner.py tests/test_archive_service.py tests/test_archive_recovery.py tests/test_archive_migration.py
rtk pytest -q tests/test_wiki_paths.py tests/test_wiki_index.py tests/test_wiki_lint.py tests/test_wiki_query.py
rtk pytest -q
rtk python -m compileall -q src
rtk git diff --check
```

## Review Gates

- [x] Bundle/manifest 完成后不可修改，同一路径多版本并存。
- [x] State transition 只有一个 reducer；所有写操作可幂等 resume。
- [x] 文件系统/SQLite 故障点均有 recovery 证据，不宣称不存在的跨介质 ACID。
- [x] Raw 活动引用、人工页和 lifecycle eligibility 使用共享 dependency contract。
- [x] Restore 不静默覆盖，purge 不出现在普通 MCP 工具 schema。
- [x] Archive/restore 必须先 plan 再 apply；无 plan ID、过期 plan 和 hash drift 均被拒绝。
- [x] Archive query/status 复用 query/status，不增加重复工具。
- [x] 未提交/损坏 bundle 不进入 archive index。
- [x] Migration 不移动 query 内容，chatlog 索引前脱敏且重复执行安全。
- [x] 新 vault 不创建 wiki/queries、wiki/chatlog、wiki/sources/chatlog、wiki/archives。

## Rollback Point

Archive write feature flag 可以关闭 plan/apply/restore/purge，只保留 status、dry-run 和已提交 bundle 的只读 archive query。回滚不能删除 operation journal、pending recovery 数据或已提交 bundles；archive index 可删除后重建。

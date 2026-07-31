# 归档生命周期与恢复：技术设计

## 1. 模块边界

- `archive_models.py`：ArchiveReason、OperationState、Manifest、Plan、Tombstone 类型。
- `archive_planner.py`：资格检查、反向引用、依赖闭包和 dry-run。
- `archive_service.py`：archive/restore/purge 状态机与幂等恢复。
- `archive_manifest.py`：稳定 YAML、schema version、hash 和签名校验。
- `archive_migration.py`：旧 queries/chatlog/wiki-archives 一次性迁移。
- 复用 `knowledge_dependencies.py`、`retrieval_index.py`、`content_redaction.py`、`wiki_io.py` 和路径安全 helper。

Archive service 不复制 page type、hash、redaction 或 dependency 解析规则。

默认 MCP façade：

```text
wiki_archive(targets, reason, cascade=false, mode="plan|apply", plan_id?, vault?)
wiki_restore(archive_id, targets?, mode="plan|apply", plan_id?, vault?)
```

Plan 结果必须包含目标 hash、依赖闭包、阻断原因、过期时间和 plan ID。Apply 不接受绕过 plan 的 `force`，并在 commit 前重新校验。Purge/recover/migrate 只由 CLI/admin 调用 archive service。

## 2. Bundle 布局与 Manifest

```text
archives/
  .staging/<operation-id>/
  .pending/<operation-id>/
  bundles/<yyyy>/<mm>/<archive-id>/
    manifest.yaml
    wiki/<vault-relative payload>
    raw/<vault-relative payload>
  log.md
```

Manifest 使用稳定字段顺序和 vault-relative POSIX path：

```yaml
schema_version: 1
archive_id: 01...
operation_id: 01...
reason: superseded
archived_at: ...
replaced_by: wiki/concepts/...
items:
  - original_path: wiki/concepts/...
    archive_path: wiki/concepts/...
    content_hash: sha256...
    kind: knowledge
dependencies: []
passage_ids: []
restorable: true
```

最终 `bundles/` 下只有已提交版本；scanner 必须忽略 `.staging`、`.pending` 和 manifest 校验失败的目录。

## 3. Durable Operation Journal

在 `.llm-wiki/state.sqlite3` 增加：

```sql
archive_operations(
  operation_id TEXT PRIMARY KEY,
  archive_id TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  state TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error_code TEXT
);

archive_operation_items(
  operation_id TEXT NOT NULL,
  original_path TEXT NOT NULL,
  original_hash TEXT NOT NULL,
  staged_path TEXT NOT NULL,
  kind TEXT NOT NULL,
  PRIMARY KEY(operation_id, original_path)
);

archive_events(...);
tombstones(...);
```

状态只允许通过单一 reducer 前进：

```text
planned -> staged -> pending -> detaching -> committed
                         \-> rolling_back -> rolled_back
                         \-> failed_recoverable
```

重试同一 operation ID 必须 resume，不创建第二个 bundle。

## 4. Archive 提交流程

1. Planner 获取 vault 写锁，检查 lifecycle、人工页、路径和活动反向引用。
2. 生成稳定 plan/hash，在 state DB 写 `planned`。
3. 复制 payload 到 `.staging`，校验 byte/hash/manifest 后进入 `staged`；活动文件未变化。
4. 原子 rename 到 `.pending/<operation-id>`，记录 `pending`。
5. Coordinator 再次校验活动 hash，进入 `detaching`，删除活动 index rows，并将活动文件移动到 recovery-safe 临时区。
6. 原子 rename pending 为最终 bundle，记录 `committed`；之后才允许 archive scanner 看见。
7. 更新 archive index；失败只将 archive index 标记 stale，由显式 update/rebuild 修复。
8. 清理 recovery 临时区并追加顶层 archive log。

任何 crash 由 journal + pending/recovery 文件决定 resume 或 rollback。不能仅根据“文件是否存在”猜测状态。

## 5. 引用和生命周期

- Knowledge dependency store 提供 active reverse references。
- Raw 有活动引用时 planner 返回 `archive_dependency_blocked` 和页面清单。
- 显式 cascade 先构建完整闭包并检查所有人工页；只要一个目标不合格，整个 plan 拒绝。
- `stale/review_required` 不合格；`superseded` 需要有效 `replaced_by`；`deprecated` 需要明确确认；人工页只能显式选择。
- Archive commit 成功后才把 state projection 标为 `archived`。

## 6. Restore

Restore 创建新的 operation：

1. 校验 bundle/manifest/payload hash。
2. 计算目标路径和依赖，检查已有文件。
3. 目标不存在时 stage copy；目标 hash 相同时返回幂等成功。
4. 目标 hash 不同时返回 `restore_target_conflict`，不允许覆盖参数绕过人工页保护。
5. Commit 活动文件并增量更新 active FTS/vector dirty/lifecycle。
6. 记录 restore event；原 bundle 和 archive index 历史保持不变。

## 7. Purge

Purge 是独立状态机：

- 验证显式 management authorization 或匹配的 retention policy。
- 重新检查活动引用和 bundle dependency。
- 先从 archive index 删除/标脏，再 stage bundle 删除；失败时恢复 bundle 和 index projection。
- 默认 tombstone 只保留 archive ID、不可逆 path hash、时间和 reason。
- `forget=true` 时不保留任何可识别 metadata，日志只记录匿名计数和操作 ID。

普通 MCP 工具 schema 不暴露 purge。

## 8. Legacy Migration

Dry-run 输出稳定 migration plan：

- `wiki/queries`：仅允许删除空目录和 generated structural index；发现其他页面立即阻止并报告。
- `wiki/chatlog/**`：迁移至 `raw/sources/chat/legacy/**`，补 provenance/source kind，内容索引前使用共享 credential redactor。
- `wiki/sources/chatlog/**`：不迁移生成的 source-index 正文；确认其 raw target 存在后删除，否则报告孤儿引用。
- `wiki/archives/log.md` 和轮转页：迁移到顶层 log 或 legacy log bundle。
- 更新 schema/navigation/path helpers 后，新版本不再扫描旧路径。

Migration 记录版本与完成 marker，可安全重复执行；不能通过保留长期 runtime compatibility 掩盖未完成迁移。

## 9. Archive Index 与 Snapshot

- Archive service 只调用 archive index 的 update/delete/mark-stale API，不维护第二套 chunk/vector 实现。
- Scope=archive 只读取已提交 bundle 的投影。
- Archive status 合并到 `wiki_status(detail=archive)`；archive 内容检索合并到 `wiki_query(scope=archive)`。
- 完整 active/archive DB snapshot 保存于运行 cache 的短期 snapshot 区，不进入永久 bundle。
- Snapshot cleanup 由 vault policy/maintenance 执行，不由普通 query 触发。

## 10. 回滚

- Archive 写路径用 feature flag 启用；关闭后仍可读取已提交 bundles。
- 状态机出现未知状态时禁止新写操作，只开放 status/recovery dry-run。
- Archive index 可删除重建；state journal 和 bundle 不可作为普通缓存删除。

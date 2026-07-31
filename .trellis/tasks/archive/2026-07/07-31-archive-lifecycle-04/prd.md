# 归档生命周期与恢复

## Goal

将现有 `wiki/archives` 的目录约定扩展为与 `raw/`、`wiki/` 并列的顶层冷数据生命周期：以不可变归档包保存废弃、过期或被替代的知识页、source index 和 raw 内容，通过强引用检查、可恢复事务、独立归档索引、恢复与显式 purge 保证历史可审计且不会污染活动查询。

## Requirements

### R1. 顶层不可变归档包

- 归档根目录为 `archives/bundles/<yyyy>/<mm>/<archive-id>/`，每个 archive ID 唯一且可排序。
- Bundle 内使用 `wiki/<原路径>` 与 `raw/<原路径>` 保留活动区相对结构，并包含 `manifest.yaml`。
- Manifest 至少记录 archive ID、原/归档路径、reason、archived_at、replaced_by、内容 hash、依赖、passage ID、触发者、恢复能力和 schema version。
- 已提交 bundle 不得原地修改；同一原路径允许多版本并存。
- 相同 payload 可在后续版本按内容 hash 去重，但不能删除归档事件和 manifest。

### R2. 归档资格与强引用

- Archive planner 只接受 `superseded/deprecated/archive-ready` 页面或显式授权目标；`stale/review_required` 本身不能自动物理归档。
- `generated: false` 人工页永不自动归档。
- 归档知识页不自动带走仍被其他页面使用的 raw。
- Raw 归档前必须计算活动反向引用；存在活动引用时默认阻止，并返回依赖页面。
- 调用方只有完成来源替换和重编译，或明确选择依赖闭包级联归档后才能继续。

### R3. 可恢复归档事务

- 文件 staging、hash 校验、pending bundle、活动文件移除、活动索引删除、final bundle 提交和 archive index dirty/update 使用持久 operation journal 协调。
- 文件系统与 SQLite 不能被假定为单一 ACID transaction；实现必须提供确定性的 crash recovery 和幂等 resume/rollback。
- 未完成 bundle 不得被 `scope=archive` 看见；失败后活动内容和活动索引必须保持原状或由 recovery 恢复。
- Archive index 是可重建投影，更新失败只标记 stale，不得损坏已提交 bundle。

### R4. 恢复

- Restore 从指定 archive ID 复制内容回活动区，原 bundle 和历史事件保持不变。
- 目标路径已有不同内容时必须阻止并报告冲突，不得静默覆盖人工或生成页面。
- Restore 校验 payload hash、manifest、依赖和路径 containment，然后更新活动 index，并把 archive catalog 保持为历史记录。
- 恢复产生独立事件和新的活动 hash，不把旧 bundle 改写为“未归档”。

### R5. Archive 与 Purge

- `archive` 可恢复；`purge` 不可恢复，并删除 bundle payload、archive index rows 和相关 caches。
- Purge 只能由显式管理操作或 vault 级 retention policy 触发，默认不自动按时间执行。
- Purge 前必须检查活动引用、归档包内部依赖和法律/隐私策略。
- 普通 MCP query/note/ingest 工具不得触发 purge。
- Purge 后默认保留不含正文的最小 tombstone；彻底遗忘策略下 tombstone 也不得包含可识别内容。

### R6. 归档索引与快照

- `.llm-wiki/archive-index.sqlite3` 从已提交 bundles 重建，物理隔离于活动 retrieval store，只服务 `scope=archive`。
- Source index Markdown 页面作为内容归档；FTS/vector 数据库本身不属于永久归档事实。
- 完整运行索引快照只用于数据库升级或批量重建的短期回滚，并按 vault 配置自动过期。

### R7. 一次性目录迁移

- `wiki/queries` 没有使用记录，不迁移内容；删除空目录、结构页及相关 schema/writer/navigation。
- 旧 `wiki/chatlog` 内容迁移到 `raw/sources/chat/legacy/`，标记 `source_kind: legacy_chatlog` 和迁移 provenance，执行凭据脱敏索引后删除旧目录；旧 `wiki/sources/chatlog` source-index 页面不迁移内容，验证 raw 存在后删除。
- 旧 `wiki/archives/log.md` 与日志轮转内容迁移到顶层 archive log/bundle。
- Migration 必须先 dry-run 报告文件、hash、目标和冲突；发现意外的非结构性 query 文件时阻止删除并报告。
- 新版本不保留对 `wiki/queries`、`wiki/chatlog`、`wiki/sources/chatlog` 或 `wiki/archives` 的运行时兼容扫描。

### R8. Archive Public Tools

- 默认 MCP 只公开 `wiki_archive` 和 `wiki_restore` 两个归档类工具。
- 两者都采用 `plan|apply` 两阶段；默认调用只能生成 plan，apply 必须携带 plan ID 并重新校验 hash/依赖。
- `wiki_archive` 取代 `wiki_delete_source`，支持知识页或 source 目标，不提供不可恢复删除参数。
- Archive 查询统一使用 `wiki_query(scope=archive)`，不增加 `wiki_archive_query`。
- Archive operation/status 由 `wiki_status(detail=archive)` 聚合，不增加独立 status tool。
- Purge、recovery 和 migration 只保留 CLI/admin 入口，不进入默认 MCP schema。

## Acceptance Criteria

- [x] AC1：相同输入计划生成稳定 archive manifest，archive ID、文件排序和 hash 可审计。
- [x] AC2：同一路径多次归档产生独立不可变版本，restore 不修改任何既有 bundle。
- [x] AC3：Raw 存在活动引用时归档被阻止；替换、重编译和显式级联路径有完整测试。
- [x] AC4：在 staging、pending、活动文件移除、活动索引删除和 final commit 各故障点中断后，recovery 能恢复到确定状态。
- [x] AC5：未提交/损坏 bundle 不进入 archive catalog；archive index 失败不影响 bundle 或活动查询。
- [x] AC6：Restore 路径冲突、hash 错误、路径逃逸和人工页覆盖均被阻止。
- [x] AC7：Purge 只有显式授权或启用的 retention policy 可以触发，普通工具无法调用。
- [x] AC8：Purge 删除 payload/index/cache 并生成合规 tombstone；彻底遗忘模式无可识别残留。
- [x] AC9：普通 query 不打开 archive index；`scope=archive` 可检索已提交 bundle 且不与活动结果混排。
- [x] AC10：`wiki/queries` 不迁移；旧 chatlog dry-run、迁移、脱敏索引、验证和旧目录删除可重复执行。
- [x] AC11：新 vault 和运行时不再创建 `wiki/queries`、`wiki/chatlog`、`wiki/sources/chatlog`、`wiki/archives`。
- [x] AC12：目标测试、全量测试、migration fixture 和 `git diff --check` 通过。
- [x] AC13：Core MCP 只注册 archive/restore 两个归档写入口；delete-source/archive-query/archive-status/purge/recover/migrate 均不公开。
- [x] AC14：Archive/restore apply 缺 plan ID、plan 过期或目标 hash 变化时拒绝，文件与索引保持不变。

## Out of Scope

- 自动判断业务知识是否应该废弃；由 freshness/compiler 和人工决策提供状态。
- 永久保存每一代完整 FTS/vector 数据库。
- 云对象存储、远程备份、跨 vault archive replication。
- 普通查询自动混合活动与归档结果。
- 通过普通 MCP 执行 purge、recovery 或 migration。

## Constraints

- 依赖 `runtime-config-tool-contract`、`passage-hybrid-index` 和 `knowledge-compilation` 提供 retention policy、active/archive index coordinator、反向依赖与 lifecycle contract。
- 破坏性操作必须遵守路径 containment、人工页保护、显式授权和可恢复性要求。
- 不允许用“删除后依赖 Git 恢复”替代 archive transaction。

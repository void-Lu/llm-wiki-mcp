# 架构深化：合并引擎/注册器/协调器/filter共享（5候选）

## Goal

把架构审查（2026-08-11，/improve-codebase-architecture）识别的 5 个深化候选落地为 3 批独立提交：

1. **批 1** — 合并双检索引擎：删除 v1 `wiki_query` 私有引擎，测试重写为 v2 语义，符号迁移到 retrieval 层。
2. **批 2** — 深化公共工具边界：`_register` 注册器吸收五段调用约定（vault 解析/别名归一化/错误投影/warning 附加），vault 推断与 budget clamp 下沉为单一来源。
3. **批 3** — 提交序列收进 `PageMutationCoordinator`（含 note_writer 返回契约对齐）+ 评估器共享生产 filter 语义。

决策记录见会话 grilling（Q1–Q10 全部确认）。

## Requirements

### 批 1 — 合并双检索引擎（候选 1）

- R1.1 删除 `src/wiki/wiki_query.py` 的 v1 引擎：`wiki_query`（19 参）、`_execute_query`、私有引擎（`_keyword_signals`、`_apply_graph_expansion`、`_legacy_context`、`_context_pack`、`_vector_recall`、`_build_graph` 等仅 v1 使用的函数）。
- R1.2 符号迁移：`DEFAULT_TOP_K`、`vector_index_records`、`VectorRecord` 及 `_vector_records` 回退逻辑迁到 `src/retrieval/`；`src/app/cli.py:23/349` 导入路径同步；测试中 `wiki_query_module.vector_index_records` 引用同步。
- R1.3 整模块删除 `wiki_query.py`（Q8=a），CLAUDE.md 模块清单同步更新。
- R1.4 测试迁移：`tests/wiki/test_wiki_query.py` 27 个测试 + `tests/wiki/test_wiki_index.py:105` 逐迁移到 v2（`run_query_v2`）语义；v1 独有字段断言（`context`、`context_pack`、`budget.used` dict、`scores.keyword`、`title_match`、`images`、`pipeline.stage_1_5_*`、`max_graph_hops`/`include_content` 参数）删除；v1 行为在 v2 有等价物的，为 v2 补等价测试（如 archive 不作图桥、frontmatter tag 过滤、project scope 优先级、raw 显式包含）。
- R1.5 v2 不补 `max_graph_hops` 可配置性（Q10）；MCP 工具签名与响应不变（test_server_tools.py:240-243 的禁止断言保持）。

### 批 2 — 深化公共工具边界（候选 2 + 5）

- R2.1 增强 `_register`（src/app/server.py:230）：工具以声明式描述注册（schema + 参数别名 + vault 选择器 + content_ref 声明 + 纯函数），注册器统一执行「validate → resolve vault → 调用 → 错误投影 → attach warnings」五段调用约定。
- R2.2 camelCase 别名声明式化：wiki_list 的 storeScope/pageSize、wiki_get 的 contentRef/includeBody/maxBytes、wiki_write_note 的 noteType 共 6 处手写 if 块消除；别名语义不变。
- R2.3 vault 推断通用化钩子（Q9）：`_content_ref_vault`（server.py:111）逻辑进注册器；声明 `content_ref` 的工具缺省 vault 时统一推断；`_publicize_tool_result`（:205）与 `wiki_get`（:405）两处推断合并为单一来源。
- R2.4 budget clamp 单一来源：宽容 clamp 语义（超限钳制而非报错）保留，在注册器或 catalog 一处实现，重复实现消除。
- R2.5 工具函数体不再手写 vault try/except；wiki_query 的 registry/timeout/telemetry 执行编排保持工具级（不进注册器）。
- R2.6 行为边界：错误码、warning 形状、工具 schema 不变（契约固定；test_server_tools 断言保持通过）。

### 批 3a — 提交序列收进 coordinator（候选 3）

- R3a.1 `projections_for` 从 `PageRepairService`（src/wiki/page_repair.py:40）移入 `PageMutationCoordinator`；coordinator 新增深方法吸收 prepare→commit→run_projections 全序列。
- R3a.2 note_writer（src/wiki/note_writer.py:327-398）与 wiki_update 复用同一序列；修复 note_writer 第二次 result 构造覆盖（:376）导致 state/operation_id/page_hash/repair_action/failed_stage 丢失的缺陷。
- R3a.3 note_writer 返回对齐 wiki_update（Q6）：补回 `state`/`operation_id`/`page_hash`/`repair_action`/`failed_stage`；补测试断言（当前无测试覆盖这些字段）；README 工具说明同步。
- R3a.4 `PageRepairService` 保留 admin plan/apply 查询职责（src/app/cli.py repair 边界不受影响）。

### 批 3b — 评估器共享 filter 语义（候选 4）

- R3b.1 共享 filter 谓词进 `src/retrieval/metadata_filters.py`；生产 `_filters_allow_page`（query_pipeline.py:422）与评估器 `_results_match_filters`（retrieval_eval.py:902）同一实现。
- R3b.2 统一严格语义（Q7）：path_prefix 加目录边界检查 + 双侧规范化（v2 行为变化：`"wiki/concepts"` 不再匹配 `"wiki/concept-extras/"`）；type 保留 source_kind 回退；tags 全量子集且拒绝标量；project 用 frontmatter casefold。
- R3b.3 eval 的 `filter_type`/`filter_tags` 经归一化进入共享谓词；v2 侧补 path_prefix 边界测试。

## Acceptance Criteria

- [x] **批 1**：`wiki_query.py` 整模块删除；`git grep wiki_query` 在 src/ 无残留 v1 引用（CLAUDE.md 文档化引用除外）；`uv run python -m pytest tests/wiki/ tests/app/test_server_tools.py tests/retrieval/ -q` 全绿；`uv run ruff check src/` 通过。
- [x] **批 1**：v2 有等价物的 v1 行为全部有 v2 测试覆盖（archive 不作图桥、tag 过滤、project scope、raw 显式包含至少各 1 个）。
- [x] **批 2**：10 个工具的 schema、错误码、warning 形状不变（test_server_tools 断言原样通过）；server.py 中 `resolve_tool_vault` try/except 块数从 10 降到 0（工具函数体）；camelCase 别名行为不变（原有别名测试通过）。
- [x] **批 2**：`_publicize_tool_result` 与 `wiki_get` 的 vault 推断共用同一实现（单一来源，无重复逻辑）。
- [x] **批 3a**：note_writer 返回含 `state`/`operation_id`/`page_hash`，`repair_pending` 时含 `repair_action`/`failed_stage`；对应断言测试新增；wiki_update 与 note_writer 的提交序列走同一 coordinator 方法。
- [x] **批 3b**：`path_prefix="wiki/concepts"` 不匹配 `wiki/concept-extras/` 的测试在 v2 通过；评估器结果校验与生产过滤共用同一谓词函数（无两套实现）。
- [x] 全量 `uv run python -m pytest` 通过（每批合并前）。

## Notes

- 实施顺序：批 1 → 批 2 → 批 3，每批独立提交，不可合并。
- 行为变更仅两处，均已在 grilling 确认：v1 引擎删除（生产无调用者）；path_prefix 目录边界收紧（批 3b）。
- note_writer 新增返回字段为 additive 变化，不破坏现有调用者。
- 当前任务状态停留在 planning；实施在 `task.py start` 后进行。

# 设计：架构深化 5 候选（3 批）

## 批 1 — 合并双检索引擎

### 现状（facts）

- v1 `wiki_query`（src/wiki/wiki_query.py:103）生产零调用者；MCP 工具（server.py:560 同名）与评估（retrieval_eval.py:966）都走 v2 `run_query_v2`（query_pipeline.py:2051，82 调用者）。
- 依赖 `wiki_query.py` 的符号：`DEFAULT_TOP_K`（cli.py:23）、`vector_index_records`（cli.py:349、retrieval_eval.py:158/193、query_pipeline.py:1381）、`VectorRecord`、`_vector_records` 回退。
- v1 测试断言独有字段：`context`、`context_pack`（`budget.allocated` dict 形状）、`budget.used` dict、`scores.keyword`、`title_match`/`images`、`pipeline.stage_1_5_*`、`RANKING_VERSION`；参数 `max_graph_hops`/`include_content`。
- v2 响应键：`ok/question/scope/project/results/additional_results/expansion_suggestions/budget/pipeline`；pipeline 含 `ranking_version/scope/corpus/authority/intent/retrieval_mode/lexical_enabled/lexical/coverage/counters/warnings/fallback`；result item 键：`citation/path/heading/score/scores{fts,vector,rrf,graph}/source_kind/metadata(+content/tokens/evidence_kind)`。

### 目标形态

- `src/retrieval/retrieval_index.py`（或既有 vector 模块）新增：
  - `DEFAULT_TOP_K = 10`（query_pipeline 若已有则复用其一，另一处删除；统一为单一来源）
  - `VectorRecord` dataclass + `vector_index_records(vault_root, *, include_raw_sources)`：逻辑照搬（RetrievalIndexStore.vector_records() 主路径 + `_candidate_pages` 回退），回退依赖的 `_candidate_pages` 从 wiki 层迁入或按等价实现。
- `wiki_query.py` 整模块删除；cli.py 导入改 `from retrieval.<new> import DEFAULT_TOP_K, vector_index_records`。
- 测试迁移映射（tests/wiki/test_wiki_query.py → tests/retrieval/test_query_pipeline.py 或 test_wiki_query_v2 风格）：
  - 迁移为 v2 断言（路径/排名/图扩展/filter/scope 语义）：
    - keyword 匹配 + citation、retrieval store 读取（`include_content` 语义映射为 v2 的 `include_context_pack`）、project scope 优先/过滤、archive 排除、frontmatter tag、raw 显式包含、graph 扩展（sources/wikilinks/code-context/escaped-table/archive 不作桥）、top_k 默认值、vector 可选阶段（映射为 v2 的 pipeline 字段 + warnings）、path tie-break、结构页排除、retired namespace 排除。
  - 删除（v1 独有行为，v2 无对应物）：
    - `scores.keyword` IDF 权重断言、`title_match`/`images`、`context_pack.budget.allocated` 形状、`pipeline.stage_1_5_*`、`budget.used` dict 形状。
  - 补 v2 测试：archive 不作图桥、tag 过滤、project scope、raw 显式包含（若迁移后无对应断言）。
- test_wiki_index.py:105 的调用改为 v2 语义断言（`run_query_v2` 返回 results == [] 或等价）。

### 兼容性

- MCP 工具签名/响应零变化；test_server_tools.py 断言（含 :240-243 v1 参数禁止）保持原样。
- CLAUDE.md「查询与图谱能力」一节更新：`wiki_query.py` 从模块清单删除，说明由 `query_pipeline.py` 承接。

## 批 2 — 深化公共工具边界

### 现状（facts）

- 10 个工具各 1 段同构 `try: resolve_tool_vault(...) except RuntimeConfigError: return _tool_error(exc)`（共 10 段）+ 11 次 attach_warnings。
- camelCase 别名手写 if 块 6 处：wiki_list `storeScope`/`pageSize`、wiki_get `contentRef`/`includeBody`/`maxBytes`、wiki_write_note `noteType`（合并表达式）。
- `_content_ref_vault`（server.py:111-117）两处调用：`_publicize_tool_result`（:205）、`wiki_get`（:405）；wiki_get 的推断逻辑在 :403-413。
- budget clamp 两处：server.py:400-402（宽容钳制）+ content_catalog.py:217-218（严格校验报错）。
- `_register`（:230-257）与 `_publicize_tool_result`（:194-227）目前不处理 vault/别名。

### 目标形态

声明式工具描述（装饰器增强，不引入独立注册表类）：

```python
@_register(
    aliases={"storeScope": "store_scope", "pageSize": "page_size"},
    content_ref_param="content_ref",   # 声明后注册器在缺省 vault 时统一推断
)
def wiki_list(store_scope: str = "active", ..., vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict: ...
```

注册器统一执行五段：
1. **别名归一化**：声明式 alias 映射应用到 kwargs（冲突时按现有语义拒绝/后者优先，与手写 if 块行为一致）。
2. **vault 解析**：`resolve_tool_vault` + `RuntimeConfigError → _tool_error` 投影（从工具函数体移除）。
3. **content_ref 推断**（若声明）：缺省 vault 时 `_content_ref_vault` + 默认库比较逻辑（合并 :403-413 与 :205 为单一实现）。
4. **filter 归一化**（若工具声明 filter 参数）：`normalize_filter_aliases`/`normalize_metadata_filters` 按工具允许集执行；ValueError → 现有错误码。
5. **warning 附加**：`attach_warnings`（含 wiki_get 的 body_budget_clamped 条件 warning 声明式化）。

边界：
- wiki_query 的 `QueryExecutionRegistry.run`/timeout/telemetry/QueryCancelled 处理保持工具级（注册器只做通用五段；`_run_wiki_query` 内部结构不变）。
- budget clamp：宽容钳制移入注册器（参数预处理层，与 aliases 同处），catalog 严格校验保留（幂等）；server.py:400-402 删除。
- `_publicize_tool_result` 的 vault 推断复用注册器同一钩子函数。

### 兼容性

- 工具 schema（Pydantic arg model + extra=forbid）不变；错误码/warning 形状不变；test_server_tools 断言原样通过。
- 别名词典与现有手写 if 行为逐项对齐（含 wiki_get 的预算钳制 warning 附加顺序）。

## 批 3a — 提交序列收进 coordinator

### 现状（facts）

- `PageRepairService.projections_for`（page_repair.py:40-101）被 note_writer.py:344/346 与 wiki_update（经 PageRepairService）当常规工具使用；PageRepairService 是 admin 修复服务。
- note_writer.py:327-347 手工编排 prepare→commit→run_projections 状态机；:354-373 第一次 result 构造被 :376-399 完全覆盖，state/operation_id/page_hash/repair_action/failed_stage 丢失；无测试断言这些字段。
- wiki_update apply 返回含 `state/operation_id/hash/page_hash/repair_action/failed_stage` 且有测试依赖（test_wiki_update.py:96/:103）。
- coordinator 现有方法：prepare/commit/run_projections/recover/repair（page_mutation.py:44-189），`Projection = Callable[[], Mapping | None]`（:16）。

### 目标形态

- `PageMutationCoordinator` 新增深方法，吸收 prepare→commit→run_projections：
  - `projections_for(operation)` 移入 coordinator（frontmatter 解析、dependencies/retrieval/navigation/overview/audit_log 闭包构造）。
  - 新方法（如 `write_and_project(operation, text, *, expected_hash=None)` 或 `commit_with_projections`）：内部走现有 prepare/commit/run_projections 逻辑，返回含 `state/operation_id/page_hash/repair_action/failed_stage` 的统一响应；operation 状态机（prepared/page_committed/repair_pending 分支）收敛在方法内。
- `PageRepairService` 保留 plan/apply（admin 查询），apply 改调 coordinator 新方法（删除自身 projections_for）。
- note_writer：删除手工状态机与第二次 result 构造；返回键集 = 现有第二次构造的键 + `state`/`operation_id`/`page_hash`/`repair_action`/`failed_stage`（对齐 wiki_update，不引入 action/navigation 等 note 无关键）。
- 补测试：test_save_obsidian_note.py 新增 state/operation_id/page_hash/repair_action 断言（正常 completed 与注入投影失败 repair_pending 两路径）。

### 兼容性

- wiki_update 行为不变（返回键集不变）；note_writer additive 字段；README 工具说明补注。
- ADR-0004/0005 语义不变（提交点/投影分离、CAS、plan 单次消费）。

## 批 3b — 评估器共享 filter 语义

### 现状（facts）

- 两套谓词四字段全不一致：project（frontmatter casefold vs 路径前缀无 casefold）、type（source_kind 回退 vs 无回退）、tags（拒绝标量 vs 包裹标量）、path_prefix（v2 无边界 vs eval 有边界双侧规范化）。
- `_filters_allow_page`（query_pipeline.py:422-431）签名：`(frontmatter, source_kind, filters: QueryFilters, page_path="")`；project 在 `_project_page_allowed`（:411-419）。
- eval `_results_match_filters`（retrieval_eval.py:902-921）输入 case.filters（`filter_type`/`filter_tags`/`type`/`tags`/`path_prefix`/`pathPrefix`，_ALLOWED_FILTERS :26）。

### 目标形态

- `metadata_filters.py` 新增共享谓词（纯函数，无副作用）：
  ```python
  def page_matches_filters(frontmatter, source_kind, *, project=None, page_type=None, tags=(), path_prefix=None, page_path="") -> bool
  ```
  或等价接口，语义：
  - project：frontmatter["project"] casefold 比较（含 codegraph 页要求），沿用 `_project_page_allowed` 语义。
  - type：`str(frontmatter.get("type") or source_kind)` 比较（保留回退）。
  - tags：全量子集；frontmatter tags 非 list/tuple → False。
  - path_prefix：`path.replace("\\","/").lstrip("/")` 与规范化前缀（strip " /"、尾斜杠语义沿用 from_mapping）边界匹配：`==` 或 `startswith(prefix + "/")`（收紧 v2 的纯 startswith）。
- `query_pipeline`：`_filters_allow_page`/`_project_page_allowed` 改为调用共享谓词（或谓词在 pipeline 侧组装 QueryFilters 语义）；`QueryFilters` 保持。
- `retrieval_eval`：`_results_match_filters` 改为构造 QueryFilters（`filter_type`/`filter_tags` 归一化为 type/tags）→ 调共享谓词；删除 `_in_project_scope`/`_in_path_prefix` 私有实现。
- 测试：v2 侧补 path_prefix 边界测试（`"wiki/concepts"` 不匹配 `"wiki/concept-extras/"`）；eval 侧 filter 校验测试改用共享谓词后保持通过。

### 兼容性

- path_prefix 边界收紧是已确认行为变更（Q7）：影响 `path_prefix="wiki/concept"` 这类前缀命中；query 与 catalog 的既有 path_prefix 测试按新语义更新。
- 评估结果校验与生产同一谓词，评估可信度提升；`retrieval_eval.py` 对外函数签名不变。

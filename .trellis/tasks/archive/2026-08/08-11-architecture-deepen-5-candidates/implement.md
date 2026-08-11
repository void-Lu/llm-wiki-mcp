# 实施计划：架构深化 5 候选（3 批）

顺序：批 1 → 批 2 → 批 3，每批独立提交，验收通过才进入下一批。每批完成后运行全量 `uv run python -m pytest` 与 `uv run ruff check src/`。

## 批 1 — 合并双检索引擎

### 检查清单

1. **符号迁移**
   - [x] `DEFAULT_TOP_K`、`VectorRecord`、`vector_index_records`、`_vector_records` 回退迁入 `src/retrieval/`（确定目标模块：retrieval_index.py 或既有 vector 模块；回退依赖的 `_candidate_pages` 一并迁入或等价实现）。
   - [x] `src/app/cli.py:23/349` 导入路径更新。
   - [x] `tests/retrieval/test_retrieval_eval.py`、`tests/retrieval/test_query_pipeline.py` 的 `wiki_query_module.vector_index_records` 引用更新。
   - [x] 确认 `DEFAULT_TOP_K` 单一来源（query_pipeline 若有同名常量则统一）。
2. **测试迁移**
   - [x] `tests/wiki/test_wiki_query.py` 27 个测试迁移到 v2 语义（迁移到 `tests/retrieval/` 或保留文件名但导入 `run_query_v2`）：路径/排名/图扩展/filter/scope/vector 可选阶段断言按 v2 响应键改写。
   - [x] v1 独有字段断言删除（context/context_pack 形状/scores.keyword/title_match/images/stage_1_5_*/budget.used dict）。
   - [x] 补 v2 等价测试：archive 不作图桥、frontmatter tag 过滤、project scope 优先、raw 显式包含（至少各 1 个）。
   - [x] `tests/wiki/test_wiki_index.py:105` 改 v2 语义。
3. **模块删除**
   - [x] `src/wiki/wiki_query.py` 整模块删除。
   - [x] `git grep wiki_query src/` 无 v1 残留（server.py 的 MCP 工具 `wiki_query` 名保留）。
4. **文档**
   - [x] CLAUDE.md「查询与图谱能力」更新：wiki_query.py 从模块清单删除，检索由 query_pipeline.py 承接。
5. **验证**
   - [x] `uv run python -m pytest tests/wiki/ tests/retrieval/ tests/app/test_server_tools.py -q` 全绿。
   - [x] 全量 `uv run python -m pytest -q` + `uv run ruff check src/`。
   - [x] 提交（独立 commit，中文消息，如 `refactor: 合并检索入口到 query_pipeline 并删除 v1 wiki_query 引擎`）。

### 回滚点

- 提交前：git 状态干净点；符号迁移与模块删除可整体 revert。
- 测试迁移先行（先迁移断言再删模块），任何一步测试红即停。

## 批 2 — 深化公共工具边界

### 检查清单

1. **注册器扩展（server.py）**
   - [x] `_register` 接受声明式参数：`aliases` 映射、`content_ref_param`、`filter_param`/允许集、warning 声明。
   - [x] 五段统一执行：别名归一化 → vault 解析（RuntimeConfigError→_tool_error 投影）→ content_ref 推断（`_content_ref_vault` + 默认库比较，合并 :403-413 与 :205 单一实现）→ filter 归一化（ValueError→现有错误码）→ attach_warnings。
2. **工具改造**
   - [x] 10 个工具逐一改为声明式注册；工具函数体删除手写 vault try/except（10 段归零）、camelCase if 块（6 处归零）、wiki_get 的 budget clamp（:400-402，移入注册器参数预处理）。
   - [x] wiki_query 的 `_run_wiki_query`/registry/timeout/telemetry 保持工具级，不改。
   - [x] `_publicize_tool_result` 的 vault 推断改调注册器钩子。
3. **行为对齐**
   - [x] 别名行为与手写 if 逐项一致（含冲突拒绝语义：vault_root 与 vaultRoot 不一致、storeScope 覆盖顺序）。
   - [x] wiki_get 的 body_budget_clamped 条件 warning 顺序不变。
4. **验证**
   - [x] `uv run python -m pytest tests/app/test_server_tools.py tests/wiki/test_content_catalog.py -q` 全绿（schema/错误码/warning 断言原样通过）。
   - [x] `grep -n "resolve_tool_vault" src/app/server.py`：工具函数体无 try/except 调用（仅注册器内 1 处）。
   - [x] 全量 pytest + ruff。
   - [x] 提交。

### 回滚点

- 声明式改造逐步进行：先扩展 `_register`（保持现有工具不动）→ 再逐个迁移工具，每迁移一个跑对应测试。

## 批 3a — 提交序列收进 coordinator

### 检查清单

1. **coordinator 深化（page_mutation.py）**
   - [x] `projections_for(operation)` 从 page_repair.py 移入 coordinator（frontmatter 解析 + 5 个投影闭包）。
   - [x] 新深方法吸收 prepare→commit→run_projections（返回统一响应：state/operation_id/page_hash/repair_action/failed_stage）。
   - [x] `PageRepairService.apply` 改调 coordinator 新方法，删除自身 projections_for（保留 plan/apply 查询）。
2. **note_writer 修复**
   - [x] 删除手工状态机（:327-347）与第二次 result 构造（:376-399）；调用新方法。
   - [x] 返回键集 = 现有键 + state/operation_id/page_hash/repair_action/failed_stage。
3. **wiki_update 对齐**
   - [x] wiki_update 的提交序列改走同一 coordinator 方法（行为不变，键集不变）。
4. **测试**
   - [x] test_save_obsidian_note.py 新增断言：completed 路径含 state/operation_id/page_hash；注入投影失败路径含 repair_action/failed_stage（state=repair_pending）。
   - [x] test_wiki_update.py / test_page_repair.py 保持通过。
5. **文档与验证**
   - [x] README wiki_write_note 返回值说明补注新增字段。
   - [x] 全量 pytest + ruff；提交。

### 回滚点

- coordinator 新方法先行（旧方法保留）→ note_writer/wiki_update 逐个切换。

## 批 3b — 评估器共享 filter 语义

### 检查清单

1. **共享谓词（metadata_filters.py）**
   - [x] 新增纯函数谓词（project casefold / type source_kind 回退 / tags 全量子集拒绝标量 / path_prefix 边界+双侧规范化）。
2. **生产侧（query_pipeline.py）**
   - [x] `_filters_allow_page`/`_project_page_allowed` 改调共享谓词（QueryFilters 语义保持）。
   - [x] 补 path_prefix 边界测试（`"wiki/concepts"` 不匹配 `"wiki/concept-extras/"`）；既有 path_prefix 测试按新语义更新。
3. **评估侧（retrieval_eval.py）**
   - [x] `_results_match_filters` 改为构造 QueryFilters（filter_type/filter_tags 归一化）→ 共享谓词；删除 `_in_project_scope`/`_in_path_prefix` 私有实现。
   - [x] eval filter 校验测试保持通过。
4. **验证**
   - [x] `uv run python -m pytest tests/retrieval/test_query_pipeline.py tests/retrieval/test_retrieval_eval.py -q` 全绿。
   - [x] 全量 pytest + ruff；提交。

### 回滚点

- 谓词提取先行（生产/评估都调用新函数）→ 语义收紧（path_prefix 边界）独立提交，若回归可单点 revert 语义收紧。

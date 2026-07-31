# Journal - 陆乾Gino (Part 1)

> AI development session journal
> Started: 2026-07-27

---



## Session 1: 完成 source index lint 与分页一致性修复

**Date**: 2026-07-27
**Task**: 完成 source index lint 与分页一致性修复
**Branch**: `netsuite-llm-wiki-mcp-v0.9`

### Summary

完成轻量 source index 的 raw 目录 lint 契约与子节点导航统一分页；目标测试、全量测试和实际 vault 验收通过；六个范围外后续任务已解除父子关系并保留为独立 planning 任务。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `5befaa9` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: 实现 MCP 运行版本可追溯性

**Date**: 2026-07-27
**Task**: 实现 MCP 运行版本可追溯性
**Branch**: `netsuite-llm-wiki-mcp-v0.9.1`

### Summary

实现统一运行时溯源契约、正式构建元数据注入、MCP 握手与 wiki_status 共享身份，并完成目标测试、全量回归和 wheel 隔离验证。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `08eaacf` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: 修复 lint 语义并治理陈旧生成页

**Date**: 2026-07-27
**Task**: 修复 lint 语义并治理陈旧生成页
**Branch**: `netsuite-llm-wiki-mcp-v0.9.2`

### Summary

统一 Markdown wikilink 解析语义，新增可审计的 Wiki 修复生命周期、归档与 raw 防护；目标测试和全量测试通过。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `c18ab6b` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: 完成有界日志与 Sources 导航

**Date**: 2026-07-27
**Task**: 完成有界日志与 Sources 导航
**Branch**: `netsuite-llm-wiki-mcp-v0.9.2`

### Summary

实现共享 UTF-8 容量契约、有界日志轮转与归档索引、可回滚的分层 Sources 分页导航，并保护手工 index 与 source index 叶子查询。实施清单和验收标准均已逐项勾选；目标测试 56 项、Sources 专项 8 项、全量 pytest 493 项、compileall 与 diff 检查通过。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `c22d608` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 5: 完成关键词与图排序优化

**Date**: 2026-07-27
**Task**: 完成关键词与图排序优化
**Branch**: `netsuite-llm-wiki-mcp-v0.9.5`

### Summary

实现正文长度归一化、受限图融合和可解释调试输出；以冻结40条真实集完成候选与消融验证，完整测试505项通过。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `eee1f3d` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 6: 完成混合向量检索 ablation 并冻结 RRF 参数

**Date**: 2026-07-28
**Task**: 实现可选的本地混合向量检索（ablation 收尾）
**Branch**: `netsuite-llm-wiki-mcp-v0.9.6`

### Summary

在冻结的任务 04 数据集上完成 lexical/vector/hybrid ablation，发现并修复 RRF 融合的两个缺陷（分值缩放和弱匹配过滤），冻结 RRF k=60，全部验收标准通过。

### Main Changes

- RRF 融合从替换 `fusion_score` 改为缩放后叠加（`(rrf_k+1)` 因子，0–2 区间），保留词法信号供图扩展使用。
- 图上限基数改为 `max(fusion_score, keyword_score, vector_score)`，避免 RRF 缩放后图扩展候选压制词法/向量候选。
- 新增 `min_vector_score`（默认 0.5）过滤弱向量匹配，控制无答案误命中率。
- 排名版本升至 `lexical-vector-rrf-graph-capped-v2`。
- 新增 2 个测试：`min_vector_score` 阈值行为和 `VectorSettings` 默认值/边界。
- 更新 CLAUDE.md 和 spec 文档。

### Git Commits

| Hash | Message |
|------|---------|
| (pending) | feat: fix hybrid RRF scaling and add min_vector_score threshold |

### Testing

- `rtk uv run pytest -q`：516 passed
- `rtk git diff --check`：通过
- 真实 ablation（40 条冻结数据集，1,689 页 vault，BGE-M3 CPU）：
  - Hybrid Recall@10 0.9722 > Lexical 0.9444（严格提升）
  - MRR@10 0.8819 ≥ 0.8750；nDCG@10 0.9043 ≥ 0.8923
  - 无答案误命中率 0.5 = 0.5；过滤器正确性 1.0 = 1.0

### Status

[OK] **Completed**

### Next Steps

- 提交后归档任务


## Session 6: 完成混合向量检索 ablation 并冻结 RRF 参数

**Date**: 2026-07-28
**Task**: 完成混合向量检索 ablation 并冻结 RRF 参数
**Branch**: `netsuite-llm-wiki-mcp-v0.9.6`

### Summary

在冻结的任务 04 数据集上完成 lexical/vector/hybrid ablation，发现并修复 RRF 融合的两个缺陷：RRF 替换 fusion_score 导致图扩展候选压制词法/向量候选（修复：缩放后叠加），以及弱向量匹配导致无答案误命中率上升（修复：min_vector_score=0.5 阈值）。冻结 RRF k=60，全部验收标准通过：Hybrid Recall@10 0.9722 > Lexical 0.9444，MRR/nDCG 不下降，无答案误命中率和过滤器正确性不劣化。排名版本升至 v2，516 tests passed。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `65715a2` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 7: 运行配置与工具契约

**Date**: 2026-07-31
**Task**: 运行配置与工具契约
**Branch**: `v0.9.8`

### Summary

完成类型化 ConfigRegistry、逻辑 Vault 解析、core/worker MCP 工具 profile、配置快照驱动查询、配置 CLI 与脱敏状态；独立复核后全量 pytest 516 通过。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `359a7de` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 8: Passage hybrid index

**Date**: 2026-07-31
**Task**: Passage hybrid index
**Branch**: `v0.9.9`

### Summary

Implemented passage-level SQLite FTS retrieval, vector schema v2, isolated archive stores, explicit index CLI, and single-file ingest; verified targeted regression coverage.

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `b5ce90e` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 9: 完成知识编译任务

**Date**: 2026-07-31
**Task**: 完成知识编译任务
**Branch**: `v0.9.10`

### Summary

完成 Capsule 与 Concept 知识编译：持久化 generation queue、Capsule/Concept 编译、依赖新鲜度投影、受控更新与 worker profile；53 项任务相关回归测试通过。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `cdab0a6` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 10: 归档生命周期与恢复

**Date**: 2026-07-31
**Task**: 归档生命周期与恢复
**Branch**: `v0.9.10`

### Summary

完成顶层不可变归档 bundle、持久 journal recovery、archive/restore MCP 与 CLI 管理入口、archive scope 检索和 legacy migration；全量 pytest 544 项通过。

### Main Changes

- Detailed change bullets were not supplied; see the summary above.

### Git Commits

| Hash | Message |
|------|---------|
| `f0487c3` | (see git log) |

### Testing

- Validation was not recorded for this session.

### Status

[OK] **Completed**

### Next Steps

- None - task complete

# Retrieval P1: Widen Raw Ladder on Weak Wiki (No Alias)

**Date:** 2026-10-19  
**Branch:** v0.9.36  
**Status:** Draft for approval → implement after plan  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`  
**P0 baseline (post-reindex):** `.eval/gold-sample-2026-10-19/reports-p0/` — Recall@10 ≈ 0.74  
**Prior design:** `docs/specs/2026-10-19-retrieval-p0-project-qualified-abstain-design.md`

## Goal / 目标

放宽 **raw 升梯触发**：当 wiki 侧没有「合格的强回答」时，探测 raw（限额），而不仅限于「wiki 完全零结果」。

本轮 **只做升梯**，不做模块名粘连 / `Nui server widget` 硬编码别名（用户已取消：写死一种拼写无法推广到 `Nsrc…` 等）。

| 改动 | 意图 |
|------|------|
| 弱 wiki / 无 qualified 强信号 → 探测 raw（capped） | 金标答案在 Help Docs / raw，wiki 仅噪声或松散命中时仍能升梯 |
| 强 wiki 仍挡 raw | 保留 P0「strong relaxed 挡 `raw_zero`」锚点 |

成功标准（同一 `gold.jsonl`，**不必**为升梯重建索引；重跑 `retrieval-eval`）：

- 目标用例 **0008 / 0015 / 0018** 的 Recall@10（或至少 hit@10）相对 `reports-p0` **有改善**，或在「无法改善」时于报告/自检中写明残差原因（见 Risks）。
- `filter_correctness` 保持 ≈ 1.0。
- 不引入向量路径；不改 MCP 公共字段契约。

## Non-goals / 明确不做

- **粘连模块名 / 口语别名**（`Nui server widget`、`NuiserverWidget`、通用 CamelCase→`N/…` 规则）——用户取消
- 向量 / hybrid / Embedding
- 无答案强主召回噪声门（0003 / 0011：P0 弱弃权碰不到的那类）
- 改 `docs/superpowers` 流程文档（本文件是唯一设计落点）
- 重做 P0 三刀（project / active `qualified_code` / 弱 relaxed 真空弃权）

## Current baseline / 现状锚点（P0 之后）

| 机制 | 位置 | 现状 |
|------|------|------|
| `plan_fallback` | `query_recovery.py` | `raw_zero` 仅当 `not has_primary_recall` **且** `not wiki_relaxed_answered`，且 `effective_scope ∈ {knowledge, all}`（`auto`→knowledge） |
| 弱 relaxed 弃权 | `query_execution_context.py` + `relaxed_fill_is_strong` | 弱填充清空 `selected`，`wiki_relaxed_answered=False` → **已允许** `raw_zero` |
| 强 relaxed | 同上 | `wiki_relaxed_answered=True` → **挡住** `raw_zero`（测试锚） |
| coverage | `plan_fallback` `coverage` / `all_coverage` | primary 有命中且拉丁词未覆盖时合并 raw（与「零结果升梯」不同） |
| raw ladder | `query_recall_policy.raw_recovery_candidates` | qualified → phrase → prefix → relaxed；已有 limit |

**P0 后目标题快照（`reports-p0`）：**

| id | 现象 | 与「仅弱 relaxed→raw」的关系 |
|----|------|------------------------------|
| 0008 | 已走 coverage→raw（`wiki_primary_missing_latin_coverage`），`raw_fts_hits>0`，仍 miss | 升梯已触发；残差更像 **raw 内排序/覆盖合并** |
| 0015 | `qualified_fts_hits` 很大，corpus=active；亦有 `raw_fts_hits` | 有 **qualified 强信号**；本设计默认 **不**为压过 wiki 再开 `raw_zero` |
| 0018 | `fts_hits=3`、`qualified=0`、`raw_fts=0` | **典型缺口**：噪声 primary 挡住 `raw_zero` |
| 0030 | 已 `raw_zero` 仍 miss | 需别名；**本轮不做** |

因此本 P1 的「放宽」核心不是重复 P0 弃权，而是：

> **Primary 有命中，但没有 qualified / identifier / phrase / exact / title 级强信号时，不得再把 `has_primary_recall` 当作「wiki 已答」来永久挡住 raw。**

## Design / 设计（单刀：升梯资格）

### Change — `wiki_strong_answered` 门控 raw（替代「凡 primary 即挡」）

**意图：** 统一「什么叫 wiki 已真正回答」：

| Wiki 证据 | 行为 |
|-----------|------|
| Strong wiki_relaxed（现有 `relaxed_fill_is_strong`） | 返回 wiki；**仍阻止** `raw_zero` |
| Weak wiki_relaxed only | P0：弃权清空；`wiki_relaxed_answered=False` → 可 `raw_zero`（不变） |
| Primary（strict / qualified stage-one）**且** 强信号 | **阻止** `raw_zero`（与今日 `has_primary_recall` 挡升梯等价） |
| Primary **但** 无强信号（噪声 AND/bigram，`qualified_fts_hits==0` 且 `relaxed_fill_is_strong(question, primary_selected)==False`） | **不**算 wiki 已答 → 允许探测 `raw_zero`（本 P1 新增） |
| 完全无 primary、无强 relaxed | 已有 `raw_zero` |

**强信号定义（冻结，禁止绝对 BM25 阈值）：**

对当前 wiki 侧候选 / `selected`（primary 路径用 stage-one 选出的页；relaxed 路径用现有函数）：

1. **Qualified 强：** 本查询 stage-one `qualified_fts_hits > 0` **且** 至少一条入选/合并候选来自 qualified 阶梯命中；**或**
2. **`relaxed_fill_is_strong(question, items)` 为真**（已有：exact / phrase / identifier / title·path 重叠与 edit-distance-1 / expansion 别名仅 title·path）。

二者皆假 → **弱 wiki**，可升 raw。

**`plan_fallback` / 装配（推荐落点）：**

- 引入清晰布尔（名称可实现时微调，语义冻结）：
  - `wiki_strong_answered`: strong relaxed **或** strong primary（上表）
  - 保留 `wiki_relaxed_answered` 仅表示「强 relaxed 已填充」（兼容现有 strong-挡-raw 测试）
- `raw_zero` 条件改为：

```text
not wiki_strong_answered
and effective_scope in {knowledge, all}   # auto 已映射为 knowledge
and raw_available in {unknown, yes}
```

- **不要**在 `history` / `archive` / 纯 `raw` scope 上新开此门。
- **coverage / all_coverage**（拉丁未覆盖合并）**保持不变**；本 P1 不改 coverage 合并公式。

**限额（capped）：**

- 继续走现有 `raw_recovery_candidates` ladder（不新开无限扫描）。
- 最终公开 `results` 仍受 `top_k` / `adaptive_expand` 约束；raw 权威分已低于 formal wiki（`compose_score` / authority）——**不**为 P1 再引入绝对分阈值。
- 实现时可另加硬顶：`raw_zero` 写入候选池的 passage 上限与现有 raw ladder `limit` 一致（或 ≤ max(50, top_k)），避免弱 primary 场景下 raw 扫爆延迟。

**与 P0 弃权的关系（无矛盾）：**

| 情况 | 行为 |
|------|------|
| 弱 relaxed | 弃权 → 空 → `wiki_strong_answered=False` → `raw_zero` |
| 强 relaxed | 返 wiki → 挡 `raw_zero` |
| 强 primary（含 qualified） | 返 wiki → 挡 `raw_zero`；coverage 仍可按拉丁缺口补 raw |
| 弱 primary（本 P1） | 可在保留或清空弱 primary 的同时跑 `raw_zero`，再按现有 raw_only / merge 语义装配（见下） |

**弱 primary 时选果策略（冻结一种，避免实现分叉）：**

1. 跑 capped `raw_zero` ladder 得到 raw 候选；
2. 若 raw 非空：以 **`raw_only` 装配为主**（与今日 `raw_zero` 分支 `candidates_kind=raw_only` 一致），`fallback.level=raw`，reason 使用新理由如 `wiki_weak_primary`（或复用 `wiki_zero_results` 若想少改评测切片——**推荐新理由**便于 slice）；
3. 若 raw 为空：回退返回原弱 primary（fail-open，避免误伤「wiki 仅有弱命中但 raw 也没有」的可答 wiki 题）。

不把弱 primary 与 raw 做无界 RRF 大融合（那是更大改动）；本 P1 与现网 `raw_zero` 替换语义对齐。

## Files to touch / 改动面

| 文件 | 变更 |
|------|------|
| `src/retrieval/query_recovery.py` | `FallbackState` / `plan_fallback`：用 `wiki_strong_answered`（或等价）门控 `raw_zero` |
| `src/retrieval/query_execution_context.py` | primary 后计算强/弱；弱 primary 仍调度 `run_raw_branch(..., branch="raw_zero")` |
| `src/retrieval/query_recall_policy.py` | 可选：抽出 `primary_fill_is_strong` = 复用 `relaxed_fill_is_strong` + qualified 计数；避免复制信号定义 |
| `tests/retrieval/test_query_recovery.py` | 决策表：弱 primary → 计划 `raw_zero`；强 primary / 强 relaxed → 不计划 |
| `tests/retrieval/test_query_pipeline.py` 或 execution 集成测 | knowledge 噪声 primary + raw 有金标路径 → raw 进 top_k；强 relaxed 仍挡 raw |

不改：`lexical_analyzer` 别名、索引投影、`page_from_file`。

## Testing plan / 测试计划

1. **Unit — `plan_fallback`**
   - `wiki_strong_answered=False`, scope=knowledge, raw=yes → `raw_zero`
   - `wiki_strong_answered=True` → 无 `raw_zero`
   - 强 `wiki_relaxed_answered=True` → 无 `raw_zero`（回归）

2. **Unit / integration — 弱 primary**
   - active 上构造仅 OR/bigram 命中、无 title/identifier/qualified → 触发 raw ladder；raw 页可进入返回集
   - 同题若注入 qualified 命中 → 不触发 `raw_zero`

3. **Regression**
   - `test_v2_wiki_relaxed_answer_blocks_raw_fallback`（强 relaxed）继续通过
   - P0 弱弃权测试继续通过
   - `test_v2_raw_fallback_respects_project_and_type_filters` 继续通过

4. **Manual eval（实现后）**

```text
uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
  --output-dir .eval/gold-sample-2026-10-19/reports-p1
```

对比 `reports-p0`：关注 0008 / 0015 / 0018；`filter_correctness`；p95 不明显恶化。

## Rollout / 落地顺序

1. 单测锁定决策表（弱 primary → raw_zero；强信号挡）
2. 接线 `query_execution_context` + `plan_fallback`
3. 跑 retrieval 单测子集 + gold eval → `reports-p1`
4. 仅 docs 本文件先提交；实现另开 commit；**不**在本任务 push

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| 弱 primary 判定过宽 → 过多 raw 探测、延迟升 | 复用 `relaxed_fill_is_strong`；qualified>0 直接算强；raw ladder 硬顶 |
| 弱 primary 判定过严 → 0018 仍不升 | 验收以 0018 为准；必要时只放宽「无 qualified 且无 title overlap」 |
| 0008 / 0015 在 P0 已有 raw 计数仍 miss | 本设计可能 **无法**保证二者↑；属排序/coverage 合并残差，记入 eval 备注，不本轮用别名或向量硬扛 |
| 0030 仍 miss | 预期；别名不做 |
| 新 fallback reason 影响 slice | 更新评测切片或接受新 `fallback:raw` 计数；不改金标 |

## Self-review checklist

- [x] 无别名 / 无向量 / 无无答案强噪声门
- [x] 强 wiki 仍挡 raw；与 P0 弃权不矛盾
- [x] 触发条件可测试、无绝对分阈值
- [x] 成功标准可执行；对 0008/0015 残差诚实
- [x] 本 commit **只含本设计文档**

## Implementation note

实现 PR / commit 应引用：  
`docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`  
先测后码。本文件提交后需用户批准再写实现计划。

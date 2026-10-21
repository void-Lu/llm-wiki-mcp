# Retrieval P2: Cluster C — Strong-Primary Tighten, Coverage Rank, Latin Probe

**Date:** 2026-10-19  
**Branch:** v0.9.36  
**Status:** Draft for approval → implement after plan  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`  
**P1-fix baseline:** `.eval/gold-sample-2026-10-19/reports-p1-fix/`  
**Prior designs:**  
- `docs/specs/2026-10-19-retrieval-p0-project-qualified-abstain-design.md`  
- `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`

## Goal / 目标

修 **簇 C（0008 / 0015 / 0018）** 在 P1 之后仍 miss 的三类残差，仍不引入向量、不改 MCP 公共字段契约、不做模块名别名。

| 用例 | P1-fix 现象 | 本轮刀 |
|------|-------------|--------|
| 0018 | `qualified=0`、`raw_fts=0`、`fallback:none`；噪声 primary 被判强，未升 raw | 刀1：收紧 `primary_fill_is_strong` |
| 0008 | 已 `coverage→raw`（`raw_fts` 大）仍 miss | 刀2：coverage 合并后的 raw 排序/权威可见性 |
| 0015 | `qualified` 大、`fallback:none`，raw 有命中但不进主结果 | 刀3：拉丁未覆盖时旁路合 raw（不替换 wiki） |

成功标准（同一 `gold.jsonl`，重跑 eval → `reports-p2-c`）：

- 相对 `reports-p1-fix`，**0008 / 0015 / 0018** 的 hit@10 或 Recall@10 **至少有改善**，或无法改善时在报告/自检写明残差原因。
- `filter_correctness` 保持 ≈ 1.0。
- 无向量路径；不改 MCP 契约。

## Non-goals / 明确不做

- 簇 D / 粘连模块名别名（已放弃）
- 无答案强主召回 / 强 relaxed 噪声门（0003 / 0011）——另轮
- 向量 / Embedding / 新向量库
- 重做 P0 三刀（project / active `qualified_code` / 弱 relaxed 真空弃权）的语义
- 用绝对 BM25 分数阈值定义「强」

## Current baseline / 现状锚点（P1-fix）

| 机制 | 位置 | 现状 |
|------|------|------|
| `primary_fill_is_strong` | `query_recall_policy.py` | `qualified_fts_hits > 0` → 强；否则 `relaxed_fill_is_strong`（phrase/identifier **可匹配 body**） |
| `wiki_strong_answered` → `raw_zero` | `query_recovery.plan_fallback` | 不强才 `raw_zero`；弱 primary 用 `wiki_weak_primary` + `combined_active_raw` |
| `coverage` | 同上 | primary 有命中 + 拉丁未覆盖 → `combined_active_raw` |
| 强 wiki 挡替换 | P1 | `wiki_strong_answered` 时不跑 `raw_zero` 替换路径 |

**P1-fix 快照：**

| id | 关键计数 | fallback | 含义 |
|----|----------|----------|------|
| 0018 | fts=3, qualified=0, raw=0 | none | 应弱升 raw，未触发 |
| 0008 | qualified=5, raw=69 | raw / latin coverage | 升梯 OK，排序残差 |
| 0015 | qualified=50, raw=80 | none | 强 wiki 挡替换；需旁路合入 |

## Design / 设计（三刀）

### 刀1 — 收紧 `primary_fill_is_strong`（0018）

**意图：** 禁止「仅 OR/bigram + body 子串」的 primary 冒充强，从而挡住 `wiki_weak_primary`。

**冻结规则：**

1. `qualified_fts_hits > 0` **且** 仍有 primary items → 强（不变）。
2. 否则调用与 `relaxed_fill_is_strong` **同族**的信号，但对 **identifier / phrase**：**只认 title/path**（及 title token / edit-distance-1），**禁止仅 body 子串**算强——与现有 expansion alias「不看 body」对齐。
3. exact title↔question、title token 重叠、expansion 在 title/path：仍算强。
4. 皆假 → `wiki_strong_answered=False` → 走现有 P1 `raw_zero` / `wiki_weak_primary`（raw 非空则按现网装配；raw 空则 **fail-open** 回弱 primary）。

**实现提示：** 可给 `relaxed_fill_is_strong` 增加 `body_evidence: bool = True`（relaxed 路径默认 True 保持 P0；primary 路径传 `False`），或抽出共享 helper，避免两套漂移。

### 刀2 — coverage 合并后 raw 可见性（0008）

**意图：** 不改 coverage **触发条件**；让已合并的 raw 金标更容易进 top-10。

**冻结规则：**

1. 仅作用于 `candidates_kind=combined_active_raw` 且来自 coverage / 刀3 旁路的装配路径。
2. 给 raw corpus 命中与 wiki 同档的 **RRF 通道权重**（或在「拉丁覆盖补洞」场景略抬 `compose_score` 对 raw 的加成）。
3. 仍受 `top_k` / adaptive expand 约束；**formal wiki authority 仍优先于 raw chat**（不颠倒全局权威序）。
4. **禁止**绝对 BM25 阈值。

刀2 与刀3 **共用**同一套合并/打分改动，避免两套权重。

### 刀3 — 拉丁未覆盖旁路合 raw（0015）

**意图：** `wiki_strong_answered` 时仍 **不**用 `raw_zero` 整页替换 wiki；但当拉丁词未覆盖时，探测 raw 并合入。

**触发（全部满足）：**

- `wiki_strong_answered == True`
- `uncovered_latin_terms` 非空
- `effective_scope ∈ {knowledge, all}`（`auto`→knowledge）
- `raw_available ∈ {unknown, yes}`

**行为：**

- 跑 capped raw ladder（限额与现有 raw recovery 一致）。
- `candidates_kind=combined_active_raw`；reason 如 `wiki_strong_latin_probe`。
- **不清空** wiki selected；合并后走刀2 的排序/权威规则。

**与现有 `coverage` 关系：**

- `coverage` 已在 `has_primary_recall + uncovered_latin` 时触发；刀3 覆盖「强回答已成立、但未走 / 未等效于 coverage 合入」的缺口（例如 0015：`coverage.triggered=false` 却仍有未覆盖拉丁计数或等价缺口——实现时以 **同一 `uncovered_latin_terms` 证据** 为准，计划层 **互斥或合并为一条**，禁止重复跑两遍 raw ladder）。
- 推荐：若 `coverage` 已计划，不再另开刀3；若强回答导致未进 `coverage` 分支但拉丁未覆盖仍成立，则走刀3。具体互斥表在实现计划里写死单测。

## Files to touch / 改动面

| 文件 | 变更 |
|------|------|
| `src/retrieval/query_recall_policy.py` | 刀1：primary 强信号不认 body-only phrase/identifier |
| `src/retrieval/query_recovery.py` | 刀3：`plan_fallback` 旁路计划；与 coverage 互斥 |
| `src/retrieval/query_execution_context.py` | 调度旁路 raw；接线强/弱 |
| coverage 合并 / RRF / `compose_score` 相关 | 刀2（+刀3 共用） |
| `tests/retrieval/test_query_recovery.py` 等 | 决策表 + 回归 |
| 本 spec | 唯一设计落点 |

不改：`lexical_analyzer` 别名、索引投影、`page_from_file`、MCP 工具签名。

## Testing plan / 测试计划

1. **Unit — 刀1**
   - 仅 body 命中的 primary → 不强 → 计划 `raw_zero` / `wiki_weak_primary`
   - title 或 qualified → 仍强 → 不因刀1 单独升 `raw_zero`
2. **Unit — 刀3**
   - `wiki_strong_answered` + 拉丁未覆盖 → 计划旁路合 raw（非 `raw_only`）
   - 无未覆盖 → 不旁路
   - 已选 coverage → 不重复第二趟 raw ladder
3. **Unit / integration — 刀2**
   - coverage 合并后 raw 金标路径可进入 top_k（构造夹具）
4. **Regression**
   - P0 弱弃权；P1 强挡 `raw_zero` 替换；project/type filter
5. **Manual eval**

```text
uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-c
```

对比 `reports-p1-fix`：0008 / 0015 / 0018；`filter_correctness`；p95 不明显恶化。

## Rollout / 落地顺序

1. 写本设计文档并提交（本任务）
2. 用户批准 spec → `writing-plans` 出实现计划
3. 先单测决策表，再接线，再 eval → `reports-p2-c`
4. 默认不 push、不开 PR（除非用户另说）

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| 刀1 过严 → 过多 raw 探测 | qualified + title/path 保底；raw 空 fail-open |
| 刀1 仍过宽 → 0018 仍不升 | 验收盯 0018；必要时 primary 路径进一步去掉 body exact 以外的模糊匹配 |
| 刀2/3 抬 raw 伤优质 wiki 题 | 仅 coverage/旁路路径；formal > raw chat 权威不变 |
| 0015 金标拉丁不在 uncovered | eval 诚实备注；本轮不加别名 |
| coverage 与刀3 双跑 raw | 计划层互斥 / 合并为一条 |

## Self-review checklist

- [x] 范围仅簇 C；无答案门 / 别名 / 向量排除
- [x] 三刀对症 0018 / 0008 / 0015；与 P0/P1 门控不矛盾
- [x] 无绝对 BM25 阈值；触发可单测
- [x] 成功标准可执行；残差允许诚实记录
- [x] 本 commit **只含本设计文档**（实现另开）

## Implementation note

实现 commit / PR 应引用：  
`docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`  
先测后码。用户批准本 spec 后再写实现计划。

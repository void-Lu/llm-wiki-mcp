# Retrieval P2: Raw Pool Co-occurrence / Phrase Recall（0008）

**Date:** 2026-10-20  
**Branch:** v0.9.36  
**Status:** Approved design — implement after plan; **本 commit 只含本设计文档**  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`  
**Prior designs:**  
- `docs/specs/2026-10-20-retrieval-p2-coverage-page-union-design.md`（0015 已通；本刀不回改）  
- `docs/specs/2026-10-20-retrieval-p2-primary-strong-fts-slice-design.md`（0018）  
- `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md`  
- `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`  
- `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`  
**诊断锚点:** `/workspace/sdd-p2/diag-0008-0015.md`（设计上下文，非实现）

## Goal / 目标

**codingwork-0008**（`email capture plug-in implementation`）在 coverage **已触发** 的情况下，金标页

- `raw/.../Developing an Email Capture Plug-in Implementation.md`
- `raw/.../Email Capture Plug-in Interface Description.md`

**从未进入** `raw_recovery_candidates` → `merge_coverage_items` 的 raw 候选池（诊断时约 **42** 页；`RAW_FALLBACK_LIMIT` 后再 expand/merge）。池内幸存者被 Admin **Enable the Email Capture Plug-in Implementation** 与大量泛化 “\* Plug-in Implementation” 页占满 top-10 → recall@10 = **0**。

纯 SQLite OR/AND FTS（page-best）仍能把 Developing ≈#3、Interface ≈#14 排进「含 ECP 覆盖词」的集合——**索引里有金标**，但 **ladder 候选生成 / 打分进 capped 池** 时丢掉了它们。刀2（`authority = 0.35 × coverage_ratio`）只能重排**已在池内**的页，对池外金标无效。

本刀：**加强 raw ladder 多词实体共现 / 短语召回**（例如要求或抬升 `email`+`capture` 共现，或 phrase / NEAR），使 Developing / Interface 进入 capped 池，再让既有 coverage + 刀2 有机会竞争。

成功标准：

- 金标 **∈** raw coverage 候选池；eval recall@10 **> 0**。
- `filter_correctness` 不回退；**0015 / 0018 仍绿**。
- 单测：模拟 loose OR 丢金标、共现 / 短语后进池；不改 page-union / denylist / FTS-slice 行为。

## Problem / 问题（0008 残差）

| 事实 | 值 |
|------|-----|
| Query | `"email capture plug-in implementation"` |
| Gold | Developing…（g2）；Interface Description…（g2） |
| Coverage | **triggered=true**；`uncovered_latin_terms≈["capture","implementation"]` |
| Counters | fts=5, qualified=5, **raw_fts≈69**；corpus=**raw** |
| 池钩子 | `n_raw≈42`；**两金标 path 均 `gold: None`（不在池）** |
| 池内覆盖 | 仅约 10/42 页盖住 `{capture,implementation}` 之一；融合 top = Admin Enable + 泛化 plug-in |
| 索引 sanity | 独立 FTS 仍能排到 Developing / Interface → **非「库里没有」** |
| 刀2 | 已抬全覆盖 raw；**金标不在池 → 无效** |
| 对比 0015 | 0015 是池内 #1 被 passage 门误杀（**page-union 已修**）；0008 是 **池召回** |

引用（诊断结论）：

```text
Root cause (0008): raw recovery candidate generation
(qualified_code / BM25 on loose plug-in terms)
drops Email Capture Dev/Interface gold before merge;
residual = pool recall + neighbor pollution,
not “coverage didn’t run” and not knife2 alone.
```

## Decision / 冻结决策

**方向：** 聊天 `a08-cooccur` / 诊断 fork **A** — **提高池召回（共现 / 短语）**。

```text
# 语义（实现细节由 plan 选定）
对多词问句 / 实体片段（例：email + capture）：
  - 在 raw ladder FTS 中增加 AND / 短语 / NEAR 共现约束或加分分支，或
  - 与现有 loose OR 结果合并时，保证共现命中页进入 RAW_FALLBACK_LIMIT 池
目标：Developing / Interface 金标进入 merge_coverage_items 的 raw 输入集
不做：仅抬刀2；仅池内去重/配额（诊断 fork B，可另刀）
```

### Scope / 范围

| 项 | 本刀 |
|----|------|
| 作用点 | **`raw_recovery_candidates`**（或等价 ladder FTS / qualified_code 候选生成） |
| 机制 | 多 token **共现 / 短语**（例 `email`+`capture`）；实现可选 AND、phrase、NEAR、或共现加分后并入 capped 池 |
| Coverage page-union（0015） | **不动** |
| Denylist / FTS strong-slice（0018） | **不动** |
| 刀2 authority 公式 | **公式不变**（进池后自然生效） |
| 池内 diversify / 路径配额（fork B） | **不做**（非本刀） |
| 仅抬刀2（fork C） | **明确否决**（金标∉池时无效） |
| 向量库 | **不做** |

### 对齐语义

| 概念 | 本刀后 |
|------|--------|
| 金标能否进 coverage merge 输入 | 共现 / 短语召回后应 **∈ 池**（成功门） |
| 排序 / Admin Enable 仍可能压金标 | **允许**进池后的排序残差；成功门是 **池成员 + recall@10>0**，不要求 Admin 必输 |
| `RAW_FALLBACK_LIMIT` | 可保留；共现页须在 limit 逻辑下仍能进池（plan 写清合并策略） |

## Implementation landing / 落点（实现另开，本 commit 不改码）

| 位置 | 变更 |
|------|------|
| `raw_recovery_candidates`（或 ladder 等价 FTS 入口） | 多词问句加强共现 / 短语召回，保证 Email Capture 金标进 capped 池 |
| `merge_coverage_items` / page-union | **不改** |
| `primary_fill_is_strong` / denylist / FTS slice | **不改** |
| 刀2 `authority` | **不改公式** |

不改：向量库、GENERIC 名单、relaxed、簇 D、0018/0015 机制、模块名 aliases（除非另刀显式开）。

### Testing plan / 测试计划

文件：优先 `tests/retrieval/` 下 raw recovery / ladder 单测（实现 plan 挂点）。

1. **新增：** fixture 模拟 loose OR 金标落在 limit 外 / 未进池；共现或短语分支后 **金标 ∈ 候选集**。
2. **回归：** 单 token / 无共现对问句行为不无故收紧到空池；0015 page-union 单测仍绿。
3. **回归 eval：**

```text
uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-raw-pool-cooccur
```

盯：0008 金标∈池 + recall@10>0；0015 / 0018 仍绿；`filter_correctness`。

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| AND / 短语过严 → 漏召其它合法 raw | 与 loose OR **并集**或 fail-open；单测覆盖「无共现对」路径 |
| 共现噪声仍占池（泛化 plug-in） | 成功门只要求金标进池 + recall>0；去重配额 **另刀**（fork B） |
| Admin Enable 进池后仍压金标 | 进池是必要前提；排序残差可后续再刀，不挡本刀验收 |
| 误伤 0015 / 0018 | 不改 page-union / denylist / FTS-slice；eval 强制回归 |
| 调参依赖 Email Capture 特例 | 规则应对「多词实体片段」泛化；plan 避免硬编码单一路径前缀 |

## Non-goals / 明确不做

- **仅**抬刀2 authority / coverage 权重（fork C — 池外无效）
- 池内 diversify / cluster-cap / `03_ECP` 路径预留槽（fork B — 可另开）
- 回改 **0015** page-union、**0018** denylist / FTS strong-slice
- 改 GENERIC / relaxed / 簇 D
- 0003 / 0011 无答案门
- 向量库 / Embedding
- 绝对 BM25 分数阈值
- 模块名 aliases（除非另刀显式 scope）
- 重做 P0 project / 弱 relaxed 真空弃权

## Relation to prior P2

本设计接在 **0015 page-union 已合入**、**0018 denylist + FTS strong-slice 已合入** 之后，针对 **diag-0008-0015** 中的 **0008 fork A**：

> raw ladder 候选生成在 loose plug-in 词上丢掉 Email Capture Dev/Interface；残差是 **pool recall**，不是 coverage 没跑，也不是单靠刀2。

与 0015「池内 passage 门」正交；0015 先修页级门，本刀修 0008 池成员资格。

## Rollout / 落地顺序

1. 写本设计文档并本地提交（本任务）— **不 push**
2. 用户批准后 → `writing-plans` 出实现计划
3. 先单测（共现进池），再改 `raw_recovery_candidates`（或等价），再 eval `reports-p2-raw-pool-cooccur`
4. 默认不 push、不开 PR（除非用户另说；可叠 PR #8 或后续 PR）

## Self-review checklist

- [x] 问题 = 金标 **不进 ~42 页 raw 池**；Admin Enable + 泛化 plug-in 占 top
- [x] 决策 = **共现 / 短语池召回**（fork A）；否决仅刀2；diversify 非本刀
- [x] 落点 `raw_recovery_candidates`（或等价）；0015 / denylist / FTS-slice **不动**
- [x] 成功门：金标∈池 + recall@10>0；filter；0015/0018 绿
- [x] 风险：过严漏召、噪声占池、Admin 仍可能压金标
- [x] 诊断锚点 `diag-0008-0015.md`
- [x] 本 commit **只含本设计文档**（实现另开）

## Implementation note

实现 commit / PR 应引用：  
`docs/specs/2026-10-20-retrieval-p2-raw-pool-cooccur-design.md`  
先测后码。用户批准本 spec 后再写实现计划。

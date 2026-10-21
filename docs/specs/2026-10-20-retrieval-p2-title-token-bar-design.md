# Retrieval P2: Primary Title-Token Denylist Bar（0018 残差刀）

**Date:** 2026-10-20  
**Branch:** v0.9.36  
**Status:** Approved design — implement after plan; **本 commit 只含本设计文档**  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`  
**Prior designs:**  
- `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`（刀1 residual risk 的延续）  
- `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`  
- `docs/specs/2026-10-19-retrieval-p0-project-qualified-abstain-design.md`  
**诊断锚点:** `/workspace/sdd-p2/diag-0018.md`、`brief-0018-title-token-design.md`（设计上下文，非实现）

## Goal / 目标

P2 刀1（`body_evidence=False`）之后，**codingwork-0018** 仍被判 `wiki_strong`，挡住 `raw_zero`。根因是 primary 路径上 **title-token 单点泛化重叠**（`N/record 模块` ∩ 问句 → `{record}`），不是 body phrase。

本刀：**仅抬高 primary 的 title-token / ED-1 门槛**（泛化 denylist + 多词例外），使 `{record}` 不强，从而能排 `raw_zero`；**不动 relaxed**，不引入向量库。

成功标准：

- 单测：`N/record 模块` + `"Custom Record Type Object Custom Fields"` → `primary_fill_is_strong` **False**；SuiteScript 单 token 仍 **True**；双泛化 `{record,type}` → **True**（按批准例外）。
- Eval：0018 应出现 `raw_zero` / 不再因 title-token 单点挡掉（`fallback:none` 因强 primary 而跳过 raw 的路径应消失）；`filter_correctness` 不回退。
- **0008 / 0015 不是本刀成功标准**（排序 / qualified 短路 / merge 另属）。

## Problem / 问题（刀1 后残差）

| 事实 | 值 |
|------|-----|
| Query | `"Custom Record Type Object Custom Fields"` |
| `tokens(Q)` | `[custom, record, type, object, custom, fields]` |
| `qualified_fts_hits` | `0` |
| Phrase 在 stage-one title/path？ | **否** → 刀1 body 门无关 |
| 强命中 | `wiki/.../n-record.md` 标题 `N/record 模块` → tokens `{n, record, 模块}` |
| Overlap | **`{record}` only** → `title_tokens & evidence_tokens` → 强 |
| 效果 | `primary_strong` → `wiki_strong_answered` → `plan_fallback` 跳过 `raw_zero`（`fallback:none`，`raw_fts=0`） |
| Gold | raw Help Doc **title exact match** — 从未检索 |

刀1 明确**保留**了 exact title↔question、title-token 重叠、edit-distance-1；spec 风险表已预见「刀1 仍过宽 → 0018 仍不升」。

现状规则（刀1 后）：`title_tokens ∩ evidence_tokens` **任意非空即强**；`tokens()` stopwords 不含 `record` / `type` / `object` 等泛化名词。`body_evidence` **不**门控 title-token / ED-1。

## Decision / 冻结决策

**方向：** primary-only + **GENERIC denylist**，带 **多泛化 overlap 仍算强** 的例外（聊天 §1 修订：`s1-multi-generic-strong`）。

对应 brief 设计分叉 **(C)**，精确臂阈值冻结为：

> `strong` iff `len(content) ≥ 1` **OR** `len(ov) ≥ 2`

（非「仅要求非泛化」；用户明确要求全泛化多词 overlap 仍强。）

### Scope / 范围

| 项 | 本刀 |
|----|------|
| 作用路径 | **仅 primary**（`body_evidence=False` / `primary_fill_is_strong` 调用链） |
| Relaxed | **不变**（`body_evidence=True` 时 title-token 仍「任意非空」） |
| Exact title↔question | 不变 |
| Phrase / ident（title/path） | 不变（刀1 body 门保留） |
| `qualified_fts_hits > 0` 短路 | 不变 |
| Coverage merge / raw_zero 编排 | 不变（本刀只改变「是否算强」的输入） |

### Title-token exact arm（primary）

```text
ov      = title_tokens ∩ evidence_tokens
content = ov − GENERIC_TITLE_TOKENS
strong  iff  len(content) ≥ 1  OR  len(ov) ≥ 2
```

例：

| Overlap | Primary 判定 |
|---------|--------------|
| `{record}` | **弱**（`content=∅` 且 `\|ov\|=1`） |
| `{record, type}` | **强**（`\|ov\|≥2`，即使用户选的多泛化例外） |
| `{suitescript}` | **强**（非泛化，`len(content)≥1`） |

Relaxed（`body_evidence=True`）：保持现网「`ov` 任意非空即强」。

### GENERIC_TITLE_TOKENS（初值，与 `tokens()` casefold 对齐）

英文小写常量（可进 `query_recall_policy.py`）：

```text
record, type, object, field, fields, module,
guide, overview, helper, helpers, api, doc, docs
```

名单不全时：**加词，不改阈值**（见 Risks）。

### ED-1 arm（primary）

与精确臂 **同滤**：

- 候选 title token / query term 若落在 `GENERIC_TITLE_TOKENS` 中，**不能单独**靠 ED-1 判强。
- 与 exact 一致的精神：单点泛化（含拼写近似的泛化词）不足以 primary 变强；实现时与 exact 臂共用同一 content / multi-overlap 判定，避免两套漂移。

Relaxed 的 ED-1：**不变**。

## Implementation landing / 落点（实现另开，本 commit 不改码）

| 位置 | 变更 |
|------|------|
| `src/retrieval/query_recall_policy.py` | 抽出 `GENERIC_TITLE_TOKENS`；在 `relaxed_fill_is_strong` 的 title-token / ED-1 臂上，当 `body_evidence=False` 时套用 `len(content)≥1 or len(ov)≥2`；`body_evidence=True` 保持任意非空 |
| `query_execution_context` 调用约定 | **不改**（仍 `primary_fill_is_strong` → `body_evidence=False`） |
| coverage / merge / raw_zero 编排 | **不改** |

不改：`lexical_analyzer` 别名、索引投影、MCP 工具签名、relaxed 默认语义。

### Testing plan / 测试计划

文件：`tests/retrieval/test_query_recall_policy.py`

1. **新增：** title `N/record 模块` + query `"Custom Record Type Object Custom Fields"` → primary **False**（锁 0018）。
2. **新增：** 同 query 若 title 重叠 ≥2 泛化词（如含 `record`+`type`）→ primary **True**（锁住多词例外）。
3. **保留：** SuiteScript 单 token primary **True**；刀1 body-only phrase **False**；relaxed `Location` 单 token **True**。
4. **回归 eval：**

```text
uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-title-token-bar
```

盯：0018 应调度 `raw_zero`（或等价弱 primary → raw 路径）；`filter_correctness`；**不强行**把 0008/0015 当本刀成功标准。

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| GENERIC 名单不全 → 其它泛化单词仍挡 raw | 加词；**不改** `len(content)≥1 or len(ov)≥2` 阈值 |
| `{record, type}` 仍强 | **按设计接受**；若某问句与 wiki 标题碰巧双泛化重叠，仍可能挡 raw |
| 更多 `wiki_weak_primary` → raw 探针变多 | 空 raw 仍 fail-open（P1 既有）；qualified + 非泛化 title 保底 |
| ED-1 同滤后，拼写近似的泛化词也不会单点变强 | 与 exact 一致，避免旁路 |

## Non-goals / 明确不做

- 动 **relaxed** title-token / ED-1
- 簇 D / 模块名 aliases
- 0003 / 0011 无答案门
- 向量库 / Embedding
- 绝对 BM25 分数阈值
- 0008 ranking / 权威可见性（刀2）
- 0015 qualified 短路旁路 / latin merge（刀3）
- 重做 P0 project / qualified_code / 弱 relaxed 真空弃权

## Relation to prior P2

本设计 **延续** `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md` 刀1 风险表中的残差项：

> 刀1 仍过宽 → 0018 仍不升 … 必要时 primary 路径进一步去掉 body exact 以外的模糊匹配

刀1 已禁止 body-only phrase/ident；本刀专门收紧 **primary title-token / ED-1**，不重开刀2/刀3。

## Rollout / 落地顺序

1. 写本设计文档并本地提交（本任务）— **不 push**
2. 用户批准后 → `writing-plans` 出实现计划
3. 先单测（0018 False / multi-generic True / SuiteScript True），再改 `query_recall_policy.py`，再 eval
4. 默认不 push、不开 PR（除非用户另说）

## Self-review checklist

- [x] 仅 primary；relaxed 明确不动
- [x] 阈值冻结：`len(content)≥1 or len(ov)≥2`；`{record}` 弱、`{record,type}` 强、`{suitescript}` 强
- [x] GENERIC 初值已列；ED-1 同滤
- [x] 非目标含 0008/0015/向量/别名/BM25
- [x] 与 P2 刀1 / P1 fail-open / qualified 短路不矛盾
- [x] 本 commit **只含本设计文档**（实现另开）

## Implementation note

实现 commit / PR 应引用：  
`docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md`  
先测后码。用户批准本 spec 后再写实现计划。

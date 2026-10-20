# Retrieval P2: Coverage Page-Union for Uncovered Latin（0015）

**Date:** 2026-10-20  
**Branch:** v0.9.36  
**Status:** Approved design — implement after plan; **本 commit 只含本设计文档**  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`  
**Prior designs:**  
- `docs/specs/2026-10-20-retrieval-p2-primary-strong-fts-slice-design.md`（0018 已通；本刀不回改）  
- `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md`  
- `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`  
- `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`  
**诊断锚点:** `/workspace/sdd-p2/diag-0008-0015.md`（设计上下文，非实现）

## Goal / 目标

**codingwork-0015**（`unauthenticated client-side contexts N/search module`）在 coverage 分支已跑、金标 raw 页已在候选池 **page_rank #1** 的情况下，仍因 **passage 级** uncovered-latin 检查失败而 `n_covering=0` → merge 早退 → `triggered=false` / `fallback=none`，wiki `n-search.md`（缺限制句）占 top。

根因：`merge_coverage_items` 先 `select_best_per_page`，再对**选中那一块**调用 `_coverage_terms(title+text)`；金标页高 BM25 块是 URL/表格垃圾，真正含 `unauthenticated…` 的兄弟 ordinal 从不被咨询。

本刀：**coverage 判定「raw 是否盖住 uncovered latin」时，按 `page_path` 汇总该页全部 passage（title+text）token 并集（或等价 any-passage 命中）**；选中 passage 仍可作展示/融合载体，但不能单独否决整页。

成功标准：

- 单测：多 ordinal 页 — 选中块无 uncovered、兄弟块有 → 页仍算覆盖；金标式 URL 垃圾 + 真文 fixture。
- Eval（`reports-p2-coverage-page-union`）：0015 coverage `triggered=true` / raw 可见，recall@10 相对现状改善；`filter_correctness` 不回退；**0018 仍绿**。
- **0008 不是本刀成功标准**（池召回残差另刀）。

## Problem / 问题（0015 残差）

| 事实 | 值 |
|------|-----|
| Query | `"unauthenticated client-side contexts N/search module"` |
| Coverage 计划 | 有 primary + uncovered latin + knowledge → **会跑** |
| 刀3 `wiki_strong_latin_probe` | **未实现**；**不是**本残差阻塞点（coverage 已尝试） |
| Uncovered | `["unauthenticated","side","contexts"]`（`side` 来自 `client-side`，次要） |
| Raw 池 | `raw_fts=80`；金标 `…/Nsearch Module.md` **page_rank #1** |
| 选中 passage | 空格 URL 垃圾：`'/ docs . oracle . com /…'`，`cov=[]` |
| `_coverage_terms` | 只看该 passage 的 `title+text` |
| 结果 | `n_covering_before_merge=0` → 早退 → `triggered=false`，`fallback=none` |
| 对比 0008 | 0008 是金标**不在池**；0015 是金标**在池却被 passage 门误杀** |

引用（`query_recall_policy.merge_coverage_items` / `_coverage_terms`）：

```text
raw_pages = select_best_per_page(raw_items)
…
covered = _coverage_terms(item, uncovered_terms)   # item = page-best passage only
if is_raw and not covered:
    continue
```

## Decision / 冻结决策

**方向：** 聊天 `a15-page-union` / 诊断 fork **A** — **页级 union**。

```text
# 语义（实现细节由 plan 选定）
page_tokens = union over all passages P with P.page_path == page:
                 tokens(P.title + " " + P.text)
covered = { t in uncovered_terms | t in page_tokens }
# 或等价：any passage on page that alone covers ≥1 / all required terms
# 展示仍可用 select_best_per_page 选出的 item 作为融合载体
```

### Scope / 范围

| 项 | 本刀 |
|----|------|
| 作用点 | **`merge_coverage_items` / `_coverage_terms`**（或紧邻助手）：raw 覆盖判定改 **page 级** |
| 证据来源 | 同 `page_path` 的 **全部 passage**（需从候选列表或索引补齐兄弟 ordinal） |
| Active/wiki 侧 | **不改** covered 语义（仍可只看选中块），除非实现为对称且无害 |
| 刀2 `authority = 0.35 × coverage_ratio` | **公式不变**；`coverage_ratio` 改用页级 covered 集合后自然生效 |
| Denylist / FTS strong-slice / qualified 短路 | **不动** |
| 0008 池召回 / 多 token 实体 | **不动**（非目标） |
| 刀3 latin-probe | **不做** |
| 向量库 / BM25 绝对阈值 / `side` 分词 | **不做**（`side` 另刀可选） |

### 对齐语义

| 概念 | 本刀后 |
|------|--------|
| Raw 能否进 coverage merge | 该 **page** 任一（或并集）passage 盖住 uncovered → **可进** |
| 哪块进结果列表 | 仍可用 page-best（BM25）作代表；页级 covered 只决定 **admission** |
| `triggered` | 有至少一页 raw 过页级门 → 可 `true`（与现有编排一致） |

## Implementation landing / 落点（实现另开，本 commit 不改码）

| 位置 | 变更 |
|------|------|
| `src/retrieval/query_recall_policy.py` — `_coverage_terms` / `merge_coverage_items` | Raw 覆盖：按 `page_path` 拉齐该页 passages 再算 covered；可缓存；缺兄弟索引时 **fail-safe 退回旧 passage 行为**（或明确从传入 `raw_items` 同 path 并集，实现 plan 选定） |
| `select_best_per_page` | **可不改**（仍选展示/排序载体） |
| `query_execution_context` / denylist / FTS slice | **不改** |

不改：`lexical_analyzer` 默认分词（含 `side`）、索引投影、MCP 工具签名、relaxed、GENERIC、`raw_recovery_candidates` 池生成。

### Testing plan / 测试计划

文件：优先 `tests/retrieval/` 下 coverage / recall_policy 单测（实现 plan 挂点）。

1. **新增：** 同 `page_path` 两 ordinal — best 无 uncovered、兄弟有 → merge **保留**该 raw 页；`coverage_ratio` 反映页级 covered。
2. **新增：** 金标式：best=URL 垃圾、兄弟含 `unauthenticated` → 过门。
3. **回归：** 单 passage 页行为与旧一致；无 uncovered 的 raw 仍过滤；刀2 authority 比例语义保留。
4. **回归 eval：**

```text
uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-coverage-page-union
```

盯：0015 `triggered` / raw 可见 / recall；0018 仍绿；`filter_correctness`；**不强行**把 0008 当本刀成功标准。

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| 更多 raw 页因兄弟段过关 → 噪声略增 | 仍要求盖住 uncovered；刀2 全覆盖权威偏移不变；eval 盯 filter |
| 缺 passage 索引 / 候选列表只有 page-best | **退回旧 passage-local 行为**或从同 path 传入 items 并集；实现 plan 写清数据来源 |
| `side` 仍在 uncovered → 过宽 | 次要；页级证据到位后通常可过；收紧分词 **另刀** |
| 额外 DB/CPU（每 raw 页拉全 passage） | 按 page 缓存；可限同 path 已在 `raw_items` 内的 ordinal |
| 误以为修了 0008 | 文档与验收明确：**0008 非成功标准** |

## Non-goals / 明确不做

- **0008** 池召回 / email+capture 共现 / NEAR / 路径预留槽
- 实现 **刀3** `wiki_strong_latin_probe`
- 改 **GENERIC** / denylist 公式 / FTS strong-slice
- 动 **relaxed** / 簇 D / aliases
- 0003 / 0011 无答案门
- 向量库 / Embedding
- 绝对 BM25 分数阈值
- 改 `side` / hyphen 分词为 coverage 专用规则（除非另开刀）
- 重做 P0 project / qualified_code / 弱 relaxed 真空弃权

## Relation to prior P2

本设计接在 **0018 denylist + FTS strong-slice 已合入** 之后，针对 **diag-0008-0015** 中的 **0015 fork A**：

> coverage raw admission 是 passage-local，uncovered 证据常在同 Help 页的兄弟 passage → false-negative filter

与 0008「金标不进池」正交；先修页级门，再开 0008 池召回刀。

## Rollout / 落地顺序

1. 写本设计文档并本地提交（本任务）— **不 push**
2. 用户批准后 → `writing-plans` 出实现计划
3. 先单测（页级 union），再改 `merge_coverage_items` / `_coverage_terms`，再 eval `reports-p2-coverage-page-union`
4. 默认不 push、不开 PR（除非用户另说；可叠 PR #8 或后续 PR）

## Self-review checklist

- [x] Raw uncovered 判定 = **page_path 全部 passage 并集**（或 any-passage 等价）
- [x] 落点 `merge_coverage_items` / `_coverage_terms`；denylist / FTS-slice / 刀3 / 0008 **不做**
- [x] 0015 成功门；0008 非成功标准；0018 回归绿；eval 目录 `reports-p2-coverage-page-union`
- [x] 风险含噪声、缺索引退回、`side` 次要
- [x] 诊断锚点 `diag-0008-0015.md`；明确非刀3
- [x] 本 commit **只含本设计文档**（实现另开）

## Implementation note

实现 commit / PR 应引用：  
`docs/specs/2026-10-20-retrieval-p2-coverage-page-union-design.md`  
先测后码。用户批准本 spec 后再写实现计划。

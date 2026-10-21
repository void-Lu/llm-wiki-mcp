# Retrieval P2: Primary Strong Evidence = FTS Slice（0018 denylist 后残差）

**Date:** 2026-10-20  
**Branch:** v0.9.36  
**Status:** Approved design — implement after plan; **本 commit 只含本设计文档**  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`  
**Prior designs:**  
- `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md`（本刀直接延续的残差）  
- `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`  
- `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`  
**诊断锚点:** `/workspace/sdd-p2/brief-0018-custom-seed-design.md`、`diag-0018.md`、`eval-0018-title-token-bar.md`（设计上下文，非实现）

## Goal / 目标

Title-token denylist 落地后，**codingwork-0018** 对 FTS 命中 `N/record 模块`（`{record}`）已不再判强，但仍被判 `wiki_strong`，挡住 `raw_zero`。

残差根因：**`primary_fill_is_strong` 的证据切片是整份 post-graph `seed_selected`**，其中混入了 **title-candidate** `Custom Tool Script`（`title_rank=9`），与问句重叠 `{custom}`（不在 `GENERIC_TITLE_TOKENS`）→ title-token 臂仍强。这与管道既有约定 **`has_primary_recall = bool(ranked)`（title merge 之前、仅 FTS）** 不对称：title-only 故意不算 primary recall，却仍能单独把 `wiki_strong_answered` 拉成 True。

本刀：**把 `primary_fill_is_strong` 的证据限制为带 FTS 排名的项**（`fts_rank is not None`）；**不改** denylist 公式与 GENERIC 名单；不动 relaxed / coverage / qualified / 刀1 body 门。

成功标准：

- 单测：证据仅含 FTS 弱命中（如 `{record}`）+ 混入 title-only `{custom}` 页时，若 strong 只看 FTS 切片 → primary **False**；纯 FTS 非泛化（SuiteScript）仍 **True**；denylist / 刀1 回归保留。
- Eval（`reports-p2-fts-strong-slice`）：0018 应出现 `raw_zero` / `wiki_weak_primary`（不再因 title-candidate 挡 raw）；`filter_correctness` 不回退。
- **0008 / 0015 不是本刀成功标准**。

## Problem / 问题（denylist 后残差）

| 事实 | 值 |
|------|-----|
| Query | `"Custom Record Type Object Custom Fields"` |
| Denylist 对 FTS | `N/record 模块` → `{record}` → **弱**（本刀前提已成立） |
| 仍强的页 | **title-candidate** `…/Custom-Tool-Script.md`，标题含 Custom Tool Script，`title_rank=9`，`ov={custom}` |
| `custom ∈ GENERIC`？ | **否** → `content={custom}` → denylist 公式下仍 **强** |
| Graph？ | **不是** graph 扩展页；graph-off 时该 title 页仍 alone-strong；无 graph 时另有 SDF 页 `{custom,type}` 可双泛化仍强 |
| Live ablation | Full `seed_selected` → **True**；`fts_rank is not None` only → **False**；title_rank-only → **True** |
| 效果 | `primary_strong` → `wiki_strong_answered` → 跳过 `raw_zero`（`fallback:none`，`raw_fts=0`） |
| 不对称 | `has_primary_recall` 仅 FTS；`primary_fill_is_strong(seed_selected)` 含 title/graph |

引用（`query_execution_context._run_fallback_recovery`）：

```text
primary_strong = seed_has_primary_recall and primary_fill_is_strong(
    question, self.seed_selected, qualified_fts_hits=self.seed_qualified_fts_hits
)
wiki_strong_answered = wiki_relaxed_answered or primary_strong or coverage_fallback
```

`plan_fallback` 的 `raw_zero` 在 **`not wiki_strong_answered`** 时触发（有 primary recall 时 reason 可为 `wiki_weak_primary`）。

## Decision / 冻结决策

**方向：** refined B / E（聊天 `approach-fts-only`）— **`primary_fill_is_strong` 的证据 = FTS-ranked items only**。

```text
fts_evidence = [item for item in seed_selected if item.fts_rank is not None]
primary_strong = seed_has_primary_recall and primary_fill_is_strong(
    question, fts_evidence, qualified_fts_hits=...
)
```

（实现可用等价切片：排除 `title_rank`-only / 无 `fts_rank` 的项；语义以 **`fts_rank is not None`** 为准。）

### Scope / 范围

| 项 | 本刀 |
|----|------|
| 作用点 | **`query_execution_context._run_fallback_recovery`**：调用 `primary_fill_is_strong` **之前**过滤证据 |
| `query_recall_policy` denylist / `GENERIC_TITLE_TOKENS` / 公式 | **不变** |
| Relaxed / `relaxed_fill_is_strong` | **不变** |
| Exact title↔question、phrase/ident、刀1 body 门 | **不变** |
| `qualified_fts_hits > 0` 短路 | **不变** |
| Coverage merge / raw_zero 编排逻辑 | **不变**（只改变传入 strong 的证据集合） |
| 结果展示 | title-candidate / graph 页 **仍可**进入最终结果；只是不能单独把 primary 判强挡 raw |

### 对齐语义

| 概念 | 本刀后 |
|------|--------|
| `has_primary_recall` | 仍 = stage-one FTS `bool(ranked)`（title merge 前） |
| Primary strong 证据 | **仅 FTS-ranked**（与「title-only ≠ primary retrieval」注释一致） |
| Title-only / graph-only 页 | 可进结果；**不能**单独设 `wiki_strong_answered` via primary_strong |

## Implementation landing / 落点（实现另开，本 commit 不改码）

| 位置 | 变更 |
|------|------|
| `src/retrieval/query_execution_context.py`（`_run_fallback_recovery`） | 调 `primary_fill_is_strong` 前把 `seed_selected` 滤成 `fts_rank is not None` 的切片（或传入已标 FTS 的列表） |
| `src/retrieval/query_recall_policy.py` | **公式可不改**；denylist / GENERIC / ED-1 保持上一刀 |
| coverage / merge / raw_zero 编排 | **不改** |

不改：`lexical_analyzer`、索引投影、MCP 工具签名、relaxed 默认语义、GENERIC 名单（本刀 **不加** `custom`）。

### Testing plan / 测试计划

文件：优先 `tests/retrieval/test_query_recall_policy.py` 与/或 execution-context 层单测（实现 plan 选定挂点，保持可测性）。

1. **新增：** 证据混入 title-only `{custom}` 页 + FTS 仅弱命中（如 `{record}`）→ 若 strong 只收 FTS 切片 → primary **False**（锁 0018 残差）。
2. **保留：** 纯 FTS 非泛化（SuiteScript）primary **True**；denylist `{record}` → False；双泛化例外；刀1 body-only phrase **False**；relaxed Location 单 token **True**。
3. **回归 eval：**

```text
uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-fts-strong-slice
```

盯：0018 `raw_zero` / `wiki_weak_primary`；`filter_correctness`；**不强行**把 0008/0015 当本刀成功标准。

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| title-only 正确页不再靠 strong 挡 raw → 更多 `wiki_weak_primary` | 空 raw 仍 fail-open（P1 既有）；exact/phrase 若落在 **FTS 命中** 上仍可强 |
| 依赖 `fts_rank` 标注可靠 | 约定：**无 `fts_rank` / `None` = 非 FTS 证据**；缺标不得默当 FTS |
| `filter_correctness`：曾靠 title-candidate strong 压 raw 误召的 case | Eval 盯金标；回退则修切片边界或另开刀，不偷偷加 GENERIC |
| Graph 邻居本就不会单独进 FTS 切片 | 顺带消除 graph 污染 strong；本刀成功标准仍以 0018 title-candidate 为准 |

## Non-goals / 明确不做

- 改 `GENERIC_TITLE_TOKENS` / denylist 公式；**不加** `custom` / `script` / `tool`
- 动 **relaxed** title-token / ED-1
- 仅「排除 graph、仍含 title-candidate」的半吊子切片（**修不了 0018**）
- 簇 D / 模块名 aliases
- 0003 / 0011 无答案门
- 向量库 / Embedding
- 绝对 BM25 分数阈值
- 0008 ranking / 权威可见性
- 0015 qualified 短路旁路 / latin merge
- 重做 P0 project / qualified_code / 弱 relaxed 真空弃权

## Relation to prior P2

本设计 **延续** `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md` 落地后的残差：

> denylist 修好 FTS `{record}` 后，title-candidate `{custom}` 仍经全量 `seed_selected` 把 primary 判强

上一刀规则：**名单不全 → 加词不改公式**。本残差用 **结构切片**（FTS-only）修 scope bug，避免 Product 词 whack-a-mole，并与 `has_primary_recall` 语义对齐。

## Rollout / 落地顺序

1. 写本设计文档并本地提交（本任务）— **不 push**
2. 用户批准后 → `writing-plans` 出实现计划
3. 先单测（FTS-only 切片下 0018 False / SuiteScript True），再改 `query_execution_context`，再 eval `reports-p2-fts-strong-slice`
4. 默认不 push、不开 PR（除非用户另说）

## Self-review checklist

- [x] Evidence = `fts_rank is not None`；title-only / graph-only 不挡 raw via primary_strong
- [x] 过滤落在 `query_execution_context`；denylist / GENERIC / relaxed / 刀1 / qualified / coverage **不变**
- [x] 0018 成功门；0008/0015 非成功标准；eval 目录名 `reports-p2-fts-strong-slice`
- [x] 风险与非目标含「不加 custom」、标注依赖、fail-open
- [x] 明确延续 title-token bar 残差，非重开刀2/刀3
- [x] 本 commit **只含本设计文档**（实现另开）

## Implementation note

实现 commit / PR 应引用：  
`docs/specs/2026-10-20-retrieval-p2-primary-strong-fts-slice-design.md`  
先测后码。用户批准本 spec 后再写实现计划。

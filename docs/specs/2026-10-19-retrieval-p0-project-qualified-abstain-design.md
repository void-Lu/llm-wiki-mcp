# Retrieval P0: Project Filter, Qualified Code on Active, Weak-Relaxed Abstain

**Date:** 2026-10-19  
**Branch:** v0.9.36  
**Status:** Approved for implementation  
**Eval context:** codingwork vault gold sample  
`C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl`

## Goal / 目标

Fix three high-confidence lexical retrieval defects without vector DB, without widening the raw recovery ladder, and without new alias spelling rules:

1. **Vendor Help docs must survive `project=codingwork`.**  
   `raw/sources/file/...` pages must not inherit a path-segment `project` that FTS then treats as a hard mismatch.
2. **Qualified identifiers must work on active/auto/knowledge/all stage-one**, not only on raw scope.  
   Chinese/mixed questions that already parse `N/workflow` / `n/record` should hit `qualified_code` before falling into noisy `wiki_relaxed`.
3. **True abstain on weak wiki_relaxed-only fills.**  
   When strict and qualified recall are empty and the only fillers are weak relaxed hits, return an empty result list (no absolute BM25 threshold).

成功标准（同一 `gold.jsonl`，codingwork 在 raw 重索引后重跑 `retrieval-eval`）：

- Recall@10 相对当前基线提升（尤其 miss 簇 B：0024–0029；簇 A：0004/0022）。
- 无答案误命中率下降（0003/0011 类「仅弱 relaxed 仍返 10 条」）。
- 过滤器正确率保持 1.0；不引入向量路径。

## Non-goals / 明确不做

- 放宽 `plan_fallback` 的 raw 升梯（title 探测、非 `wiki_zero` 即搜 raw）
- `Nui server widget` 等新别名 / compact 拆词规则
- 向量 / hybrid / Embedding 索引
- 改 MCP 公共字段契约（本 P0 只改召回与空结果行为）
- 改 `docs/superpowers` 流程文档（本文件是唯一设计落点）

## Current baseline / 现状锚点

| 机制 | 位置 | 现状 |
|------|------|------|
| raw `project` 推断 | `src/retrieval/retrieval_index.py` `page_from_file` | `raw/sources/<type>/<seg>/...` → `setdefault("project", parts[3])`。Help Docs 变成 `project=NetSuite Help Docs` |
| FTS project SQL | 同文件 `search_fts` | `(project='' OR project=?)`：有值则必须相等 |
| Python project 过滤 | `src/retrieval/metadata_filters.py` `page_matches_filters` | 无 project 元数据放行；有且不等则拒绝 |
| active stage-one | `src/retrieval/query_pipeline.py` `_stage_one_fts_hits` | 仅 `strict`；**仅 raw scope** 才追加 `qualified_code` |
| raw recovery ladder | `src/retrieval/query_recall_policy.py` | 已含 `qualified_code` → phrase → prefix → relaxed |
| fallback | `src/retrieval/query_recovery.py` `plan_fallback` | 无 primary → 优先 `wiki_relaxed`；relaxed 有答则 **挡住** `raw_zero` |
| CJK strict | `src/retrieval/lexical_analyzer.py` `fts_query` | 拉丁 + CJK bigram **全部 AND** |
| 质量门 | `src/retrieval/query_quality_policy.py` | 无 threshold view 时 v0 **keep-all**；禁止绝对分阈值 |
| 评测 abstention | `retrieval_eval` + manifest | 只影响无答案 FP 统计，不改变线上返回 |

评测复现结论（只读，已记录于会话分析）：  
0024–0029 去掉 `project` 后 raw gold@1；0004/0022 纯 `N/x` 时 wiki gold@1；0003/0011 为 `fts=0` 仅 relaxed 仍返回。

## Design / 设计（Approach A — 仅三处）

### Change 1 — `page_from_file`：file 树不推断 project

**File:** `src/retrieval/retrieval_index.py` → `page_from_file`

**规则（完整、无歧义）：**

| 路径 | `project` 行为 |
|------|----------------|
| `raw/sources/file/...` | **不要** `setdefault` 自 `parts[3]`。无 frontmatter `project` 则保持空字符串写入 `pages.project` |
| `raw/sources/chat/...` | 不变：`setdefault("project", "unknown")` |
| 其它 `raw/sources/<type>/...`（如 `projects/` 快照） | **保留**现有 `len(parts) >= 5` → `setdefault("project", parts[3])` |
| `wiki/projects/<name>/...` | 不变：`parts[2]` |
| 任意路径已有 frontmatter `project` | **尊重**显式值（`setdefault` 不覆盖） |

**意图：** vendor 文件树（NetSuite Help Docs 等）进入「空 project → FTS 放行」语义，使 `project=codingwork` 的 vault 查询仍能命中共享 Help；真正的按项目 raw 快照（`raw/sources/projects/<project>/...`）继续被过滤。

**索引后果：** 必须对 codingwork **重建 raw store**（`llm-wiki-mcp index build --vault ... --scope raw` 或等价 update），否则旧投影仍带错误 project。active 索引可不强制重建（本条只影响 raw 投影）。

**不改：** `search_fts` SQL 形态、`page_matches_filters` 语义（继续空放行 / 有值匹配）。

### Change 2 — `_stage_one_fts_hits`：active 族启用 `qualified_code`

**File:** `src/retrieval/query_pipeline.py` → `_stage_one_fts_hits`

**现逻辑：**

```text
steps = [strict]
if effective_scope == "raw" and has_qualified_identifier(question):
    steps.append(qualified_code)
```

**新逻辑：**

```text
steps = [strict]
if has_qualified_identifier(question) and effective_scope in {
    "raw", "auto", "knowledge", "all"
    # 以及管线把 auto 解析后的等价 active 作用域
}:
    steps.append(qualified_code)
```

精确集合以管线里 `effective_scope` 的实际取值为准，但必须覆盖：**raw、auto、knowledge、all**（以及解析后仍表示 active wiki 语料的值）。**不要**在 `history` / `archive` 上新增 qualified 阶梯（除非现有代码路径已等同 raw；本设计不要求扩展 archive/history）。

**合并：** 继续 `search_ladder(..., merge="merge_by_passage", limit=50)`；`qualified_fts_hits` 计数语义保持。

**不改：**

- `fts_query` / CJK AND 本身（qualified 子查询已不含中文，从侧路绕开）
- raw recovery ladder 步骤顺序
- `lexical_analyzer` 别名表

**预期效应：** `N/workflow 模块…` / `n/record模块…` 在 active 上先出限定符命中，gold 页进入 top-10，而不掉进 200 条 relaxed 噪声。

### Change 3 — True abstain：仅弱 wiki_relaxed 时返回空列表

**意图：** 区分「强 relaxed 仍可作为 wiki 回答并挡住 raw_zero」与「strict+qualified 皆空、仅弱噪声填充」。

**判定（相对信号，禁止绝对 BM25 阈值）：**

在 fallback / 结果装配边界（优先落在 `plan_fallback` 消费之后、或 quality / assembly 在选定 `wiki_relaxed` 候选之后的单一决策点），当同时满足：

1. stage-one **strict 命中为空**，且 **qualified_code 命中为空**（或未跑 qualified）；
2. 当前填充来源为 **`wiki_relaxed` / `relaxed` / `active_relaxed`** 分支；
3. 候选被判定为 **weak**（见下）；

则：**公开 `results` = `[]`**（true abstain）。  
不得用全局绝对分阈值；不得把 calibration 缺失变成 keep-all 回退到「弱噪声也返回」。

**Weak vs strong（实现必须二选一并写进测试；本设计冻结如下）：**

- **Strong relaxed（保持现状：可返回，且继续挡住 `raw_zero`）：**  
  存在至少一条候选带有 **identifier / phrase / exact / title-overlap** 类独立信号（复用 `query_quality_policy` 已有 `identifier_signal` / `phrase_signal` / `exact_signal`，或与 title-candidate 命中等价的现有标志）。  
- **Weak relaxed（abstain → 空列表）：**  
  无上述独立信号；仅靠 OR-token / CJK bigram 的宽松匹配凑满 top_k。

若实现时特征对象尚未在该边界构造：允许在同一 PR 内用最小只读特征抽取（与质量门同一套定义），但 **不得**引入绝对 score cut。

**与 `raw_zero` 的关系（无矛盾）：**

| 情况 | 行为 |
|------|------|
| Strong wiki_relaxed 有答 | 返回结果；**仍阻止** `raw_zero`（现有测试保留） |
| Weak wiki_relaxed only | **abstain 空列表**；此时视为「wiki 未真正回答」，允许后续按现有 `plan_fallback` 矩阵进入 `raw_zero`（若 scope∈{knowledge,all} 且 raw 可用）。即：弱噪声不再伪装成 `wiki_relaxed_answered=true` |
| Strict 或 qualified 非空 | 不走本 abstain |

**质量门：** 默认未 enforce 时，本 abstain 是 **召回装配策略**，不是 shadow gate 观测。不依赖用户开启 quality-gate artifact。

**评测：** 线上空结果 ⇒ 无答案用例不再计 FP（在现有 `top_score >= abstention_threshold` 逻辑下自然成立）。

## Files to touch / 改动面

| 文件 | 变更 |
|------|------|
| `src/retrieval/retrieval_index.py` | Change 1 |
| `src/retrieval/query_pipeline.py` | Change 2；Change 3 的装配挂钩（若决策放在投影前） |
| `src/retrieval/query_recovery.py` 和/或 `query_recall_policy.py` / `query_quality_policy.py` | Change 3：`wiki_relaxed_answered` 仅在 strong 时为真；weak → 空结果并允许 raw_zero |
| `tests/retrieval/test_retrieval_index.py` | file 树不推断 project；projects 快照仍推断；frontmatter 仍尊重 |
| `tests/retrieval/test_query_pipeline.py` | active/knowledge 上 qualified 主路径（对称现有 raw 别名测试） |
| `tests/retrieval/test_query_recovery.py` / `test_query_recall_policy.py` | strong relaxed 仍挡 raw；weak 空结果且可进 raw_zero；无绝对阈值 |

可选只读辅助：更新评测说明注释；**不**改 gold 标注。

## Testing plan / 测试计划

1. **Unit — project 投影**  
   - `raw/sources/file/NetSuite Help Docs/.../x.md` → indexed `project == ""`  
   - `raw/sources/projects/demo/...` → `project == "demo"`  
   - chat → `unknown`  
   - file 页 YAML `project: codingwork` → 保留 `codingwork`

2. **Unit / integration — qualified on active**  
   - knowledge/auto：`N/record模块有哪些方法` 或 `N/workflow 模块…` → `qualified_fts_hits > 0`，目标 wiki 模块页进入返回集  
   - history/archive：不新增回归义务（保持既有「不开 raw store」类测试）

3. **Fallback / abstain**  
   - **Strong：** 现有 `test_v2_wiki_relaxed_answer_blocks_raw_fallback` 继续通过  
   - **Weak：** 构造仅 OR-bigram 噪声、无 identifier/phrase/exact → `results == []`；在 knowledge/all + raw 可用时，弱 abstain 后允许 raw_zero（新测试）  
   - 断言决策不读取绝对 BM25 常数阈值

4. **Regression**  
   - `test_v2_raw_fallback_respects_project_and_type_filters`：项目快照过滤仍成立  
   - 新增：file Help + `project=codingwork` 在 raw scope 可命中（与 Change 1 对齐）

5. **Manual eval（实现后）**  
   ```text
   uv run llm-wiki-mcp index build --vault <codingwork> --scope raw
   uv run llm-wiki-mcp retrieval-eval --vault <codingwork> \
     --dataset .eval/gold-sample-2026-10-19/gold.jsonl \
     --manifest .eval/gold-sample-2026-10-19/gold.manifest.json \
     --output-dir .eval/gold-sample-2026-10-19/reports-p0
   ```
   对比原 `reports/retrieval-eval.json`：Recall@10↑，no_answer_false_positive_rate↓。

## Rollout / 落地顺序

1. 实现 Change 1 + 索引测试 → 本地 raw rebuild（codingwork）  
2. 实现 Change 2 + active qualified 测试  
3. 实现 Change 3 + fallback 测试矩阵更新  
4. 跑 retrieval 单测子集与 gold eval  
5. 仅在用户要求时再 push；本设计提交单独 docs commit

## Risks / 风险与缓解

| 风险 | 缓解 |
|------|------|
| 其它 `raw/sources/<type>/` 也误用 parts[3] | 本设计 **只豁免 `file`**；`projects/` 等保持推断 |
| qualified 在 active 增加噪声 | 限定符查询本身较窄；与 strict merge_by_passage；用 gold 0004/0022 验收 |
| 弱/强判定过严 → 误杀有用 relaxed | 强信号复用质量门已有 feature；保留 strong 挡 raw 测试作锚 |
| 弱 abstain 后 raw_zero 变多、延迟上升 | raw ladder 已有 limit；P0 不放宽升梯条件 |
| 旧 raw 索引残留错误 project | 成功标准强制 raw reindex |

## Self-review checklist

- [x] 无 TBD / 无开放选项与正文冲突  
- [x] 三处改动边界清晰，非目标已排除  
- [x] Change 3 与「strong relaxed 挡 raw」不矛盾（weak 不再算 answered）  
- [x] 禁止绝对分阈值，与 `query_quality_calibration` 约定一致  
- [x] 成功标准可执行（同一 gold + raw rebuild + eval）

## Implementation note

实现 PR / commit 应引用本文件：  
`docs/specs/2026-10-19-retrieval-p0-project-qualified-abstain-design.md`  
先测后码；本 commit **只含本设计文档**。

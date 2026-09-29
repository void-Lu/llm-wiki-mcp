# Graph on/off 检索评测基线（CI/dev fixture，非生产基线）

> 本文件记录合成与 CI fixture 上的 Query V2 图扩展开/关对比，只用于开发期观察检索逻辑行为，**不能当作真实 vault 的生产基线**（见 README “仓库内 `tests/fixtures/retrieval/` 只用于 CI 的确定性框架验证”）。

## 运行条件

- 代码：`c991b35`（branch `feat/graph-eval-fixture`，基于 `691492e` / v0.9.34；dirty=False；package_version 字段=0.9.0）
- 排名版本：`query-v2-passage-rrf-10`；入口 `engine`，`retrieval_mode=lexical`，`top_k=10`，`repeats=3`（排名跨重复一致），`measure_context_budget=False`
- 图扩展开关：`run_retrieval_evaluation(..., graph_expansion=True|False)`（CLI：`--no-graph-expansion`）；MCP `wiki_query` 接口未改动
- 环境：Linux 容器，Python 3.13.5，8 vCPU；每次评测前复制 fixture 到临时目录并构建 active 索引；v2_40 先提交为不可恢复 archive bundle 再以 `scope=archive` 评测（与 CI 测试一致）
- 记录时间：2026-09-29（UTC+8）；只读副作用检查全部 clean
- 指标口径：Recall@k/MRR@10/nDCG@10 为可回答 case 的 macro 平均；无答案误命中率 = 无答案 case 中 top1 分数 ≥ manifest `abstention_threshold`（均为 0.5）的比例；P95 为每次主查询耗时（含 3 次重复）的 95 分位，单位 ms，受机器负载影响，仅作相对参考。

## 总表

| Fixture | Graph | Cases | R@1 | R@3 | R@5 | R@10 | MRR@10 | nDCG@10 | 无答案误命中率 | P95 ms | 有 graph 命中的 case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| graph_v1（active，52） | on | 52 | 0.302 | 0.562 | 0.625 | 0.740 | 0.489 | 0.548 | 0.500 | 23.8 | 26 |
| graph_v1（active，52） | off | 52 | 0.302 | 0.521 | 0.552 | 0.604 | 0.459 | 0.489 | 0.500 | 13.3 | 0 |
| v2_40（archive，40） | on | 40 | 0.750 | 0.889 | 0.917 | 0.944 | 0.826 | 0.856 | 0.000 | 6.9 | 0 |
| v2_40（archive，40） | off | 40 | 0.750 | 0.889 | 0.917 | 0.944 | 0.826 | 0.856 | 0.000 | 6.9 | 0 |
| CI 冒烟 fixture.jsonl（active，6） | on | 6 | 0.700 | 0.800 | 0.800 | 0.800 | 0.800 | 0.800 | 0.000 | 2.5 | 0 |
| CI 冒烟 fixture.jsonl（active，6） | off | 6 | 0.700 | 0.800 | 0.800 | 0.800 | 0.800 | 0.800 | 0.000 | 2.4 | 0 |

v2_40 与 1baeb3a 上的早期记录一致（0.750/0.889/0.917/0.944，MRR 0.826，nDCG 0.856）。v2_40 在 archive scope 评测，而 `_graph_expand` 对 archive/raw scope 直接返回空，因此开/关结果按设计相同；CI 冒烟 vault 没有任何 wikilink，也没有 graph 命中。

## graph_v1 分组（slice）

| Slice | Cases | Recall@10 on / off | MRR@10 on / off | nDCG@10 on / off |
| --- | --- | --- | --- | --- |
| `tag:natural-language` | 20 | 0.600 / 0.600 | 0.277 / 0.277 | 0.354 / 0.354 |
| `tag:keyword-anchor` | 11 | 0.591 / 0.000 | 0.148 / 0.000 | 0.262 / 0.000 |
| `tag:direct` | 17 | 1.000 / 1.000 | 0.961 / 0.971 | 0.961 / 0.963 |
| `tag:frontmatter-only` | 2 | 0.500 / 0.500 | 0.250 / 0.250 | 0.315 / 0.315 |
| `tag:no-link` | 2 | 0.000 / 0.000 | 0.000 / 0.000 | 0.000 / 0.000 |
| `tag:shared-source` | 6 | 0.500 / 0.333 | 0.190 / 0.167 | 0.266 / 0.210 |
| `tag:hub-competition` | 2 | 1.000 / 1.000 | 0.667 / 0.750 | 0.700 / 0.760 |

无答案（4 条）：误命中率 on 0.500 / off 0.500；`none-marketing`（“budget” 词命中 retry-budget）与 `none-customs-near-miss` 两种模式下都误命中，图扩展没有改变无答案结果。

## graph_v1 中排序发生变化的 case

金标页面在 top-10 中的名次（`-` 表示未进入 top-10）。

| Case | Tags | 金标名次 on | 金标名次 off | nDCG@10 on / off | 结论 |
| --- | --- | --- | --- | --- | --- |
| `kw-tariff-calculator-consumers` | keyword-anchor, inbound-link | [10, -] | [-, -] | 0.177 / 0.000 | graph 有帮助 |
| `kw-invoice-assembly-dependents` | keyword-anchor, inbound-link | [7, 5] | [-, -] | 0.393 / 0.000 | graph 有帮助 |
| `kw-pickup-queue-dedup` | keyword-anchor, outbound-link | [3] | [-] | 0.500 / 0.000 | graph 有帮助 |
| `kw-lane-catalog-owner` | keyword-anchor, inbound-link | [10] | [-] | 0.289 / 0.000 | graph 有帮助 |
| `kw-journal-exporter-owner` | keyword-anchor, inbound-link | [-] | [-] | 0.000 / 0.000 | 金标名次不变（只追加/重排非金标） |
| `kw-throttler-incident` | keyword-anchor, inbound-link, shared-source | [7] | [-] | 0.333 / 0.000 | graph 有帮助 |
| `kw-event-store-writers` | keyword-anchor, inbound-link | [4, 5] | [-, -] | 0.501 / 0.000 | graph 有帮助 |
| `kw-exporter-concepts` | keyword-anchor, outbound-link | [3, 2] | [-, -] | 0.693 / 0.000 | graph 有帮助 |
| `kw-label-timeouts-research` | keyword-anchor, shared-source, no-link | [-] | [-] | 0.000 / 0.000 | 金标名次不变（只追加/重排非金标） |
| `kw-harbor-errors-shared` | keyword-anchor, typed-relation, frontmatter-only | [-] | [-] | 0.000 / 0.000 | 金标名次不变（只追加/重排非金标） |
| `kw-retry-budget-origins` | keyword-anchor, typed-relation | [-, -] | [-, -] | 0.000 / 0.000 | 金标名次不变（只追加/重排非金标） |
| `direct-parcel-router` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-dead-letter` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-circuit-breaker` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-outbox` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-multi-carrier` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-invoice-dates` | direct | [1, 3] | [1, 3] | 0.983 / 0.983 | 金标名次不变（只追加/重排非金标） |
| `direct-cut-off` | direct | [1, 3] | [1, 3] | 0.956 / 0.956 | 金标名次不变（只追加/重排非金标） |
| `direct-duplicate-journal` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-schema-versioning` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-key-rotation` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-dimensional-weight` | direct | [1, 2] | [1, 2] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-fanout` | direct | [1] | [1] | 1.000 / 1.000 | 金标名次不变（只追加/重排非金标） |
| `direct-carrier-credentials` | direct, hub-competition | [3] | [2] | 0.500 / 0.631 | graph 有害 |
| `direct-carrier-latency` | direct, hub-competition | [1, 7] | [1, 9] | 0.900 / 0.889 | graph 有帮助 |
| `direct-token-bucket` | direct | [1, 2] | [1, 3] | 1.000 / 0.920 | graph 有帮助 |

排序完全相同的 case（26 条）：`dep-tariff-consumers`、`dep-tariff-consumers-zh`、`dep-invoice-assembly-downstream`、`dep-pickup-queue-channels`、`dep-event-store-feeders`、`dep-throttler-troubleshooting`、`dep-tariff-calculator-feed`、`dep-label-printer-limits`、`dep-pickup-queue-double-dispatch`、`dep-exporter-concepts`、`dep-shipment-charges-plan`、`owner-lane-catalog`、`owner-journal-exporter`、`owner-lane-picker-heuristic`、`spec-carrier-retry-shared`、`spec-retry-origins`、`spec-invoice-date-shared`、`spec-invoice-total-shared`、`spec-harbor-errors-shared`、`source-label-incident-research`、`direct-sms-lag`、`direct-message-templates`、`none-autoscaling`、`none-marketing`、`none-payroll`、`none-customs-near-miss`。

## 观察（基于上表与 debug 排查）

1. **自然语言关系问题完全不触发图扩展。** graph_v1 的 20 条 `natural-language` case 开/关排序逐条相同。排查发现：这些问题含有语料中不存在的功能词（“which / consume / depends”等），stage-one strict FTS 为 0 命中，随后由 `QueryExecutionContext` 的 `wiki_relaxed` 恢复分支给出结果，而图扩展只在 strict 种子阶段（`query_pipeline.run_query_v2` → `_graph_expand`）执行，relaxed 候选不会再做扩展。本次运行中 20 条自然语言 case 全部走 `relaxed`，11 条 keyword-anchor 与 17 条 direct 全部走 `strict`。也就是说，fixture 已在 active scope、金标主要靠链接可达，但当前引擎对最需要图的查询形态不生效；这是引擎行为，不是 fixture 缺陷，fixture 未为此改写。
2. **短锚点查询时图扩展明显有帮助。** keyword-anchor 分组 Recall@10 从 0.000 升到 0.591，MRR@10 从 0.000 到 0.148；整体 graph_v1 Recall@10 0.604 → 0.740、nDCG@10 0.489 → 0.548，R@1 不变（纯图页面最高 0.75 分，永远排在词法命中之后，只能填充尾部名次）。
3. **纯图页面的排序基本是字母序。** 纯图候选分数上限 0.75，直接链接（3.0）、同类型（1.0）、两跳公共邻居都足以饱和到 0.75，之后按路径排序（`wiki/concepts/` < `wiki/entities/` < `wiki/projects/`）。例如 `kw-retry-budget-origins`：retry-budget 正文直接链接两个项目 spec，但两跳的同类型 entity（audit-trail、customer-portal……）同样 0.75 且路径更靠前，把金标挤出 top-10；`kw-journal-exporter-owner`、`kw-tariff-calculator-consumers`（第二个金标）同理。
4. **共享来源与 typed 关系不是图的边。** `shared_source` 只给已经通过 wikilink 遍历到的邻居加分，不会把只共享来源的页面拉进候选（`kw-label-timeouts-research`、`source-label-incident-research` 开/关均 0）。另外 `graph_retrieval._as_list` 不识别 `QueryCorpusSnapshot` 冻结后的 tuple，`sources` 以整个 tuple 的字符串比较：单来源页面之间恰好能匹配，多来源页面（如 routing-overview 与 lane-registry 共享 routing review）则匹配不上。`derived_from`/`applies_to`/`related_objects` 只在 frontmatter 时完全不参与（`frontmatter-only` 开/关相同）。
5. **图扩展也会伤害直查。** 15% 加分会重排词法命中：`direct-carrier-credentials` 中高连接度的 carrier-gateway 被加分，金标 api-key-rotation 从第 2 名降到第 3 名（direct 分组 MRR@10 0.971 → 0.961）。图扩展还会把几乎所有直查结果补满到 10 条（如 `direct-parcel-router` 从 1 条变 10 条），Recall 不惩罚这些噪声，但会占用调用方上下文。
6. **延迟与无答案。** 56 页 vault 上 graph on 的 P95 约为 off 的 1.8 倍（23.8 ms vs 13.3 ms，单机单次测量）。无答案误命中率开/关均为 0.500，图扩展没有改变无答案结果；但 manifest 阈值 0.5 远低于词法分数量级（本 fixture 可回答 top1 约 1.3–20），任何词法命中都会计为误命中，该指标目前区分度有限。

## 未决 / 注意

- 所有数字来自合成或 CI fixture，只反映检索逻辑，不代表真实 vault 效果；真实 vault 仍需冻结数据集，否则 gate 保持 `unproven`。
- P95 为单次运行的墙钟时间，未做多次采样或隔离负载。
- `graph_v1` 的 keyword-anchor 组是“短锚点 + 关系意图写在 `notes`”的建模，查询文本本身不含关系词；解读时需与自然语言组分开看。

## 排序修复记录（branch `feat/graph-ranking-fixes`）

在 step-2（`54dbf08`，边表持久化 + `sources` tuple 修复）之上按顺序做三项排序修复，每项一个提交，提交后重跑同一套评测（`retrieval_mode=lexical`、`top_k=10`、`repeats=3`，graph_v1 开/关、v2_40 archive、CI 冒烟）。分组按 case 标签：NL relationship = `natural-language`（20），keyword-anchor（11），direct（17），no-answer（4）。R@k/MRR 为该组可回答 case 的 macro 平均；无答案误命中口径同上。排名版本自修复 1 起为 `query-v2-passage-rrf-11`。

| 阶段 | NL R@1 / R@3 / R@10 / MRR | keyword-anchor R@1 / R@10 / MRR | direct R@1 / MRR | 无答案误命中 | graph_v1 on 总体 R@1 / R@3 / R@10 / MRR / nDCG | graph_v1 off 总体 R@10 / MRR | v2_40 R@1 / MRR | CI R@1 / MRR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| step-2 基线（54dbf08） | 0.050 / 0.425 / 0.600 / 0.277 | 0.000 / 0.591 / 0.146 | 0.794 / 0.961 | 0.500 | 0.302 / 0.562 / 0.740 / 0.489 / 0.548 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 修复 1：relaxed 路径图扩展 | 0.050 / 0.400 / 0.675 / 0.279 | 0.000 / 0.591 / 0.146 | 0.794 / 0.961 | 0.500 | 0.302 / 0.552 / 0.771 / 0.490 / 0.555 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 修复 2：纯图分数按证据缩放 | 0.050 / 0.425 / 0.700 / 0.299 | 0.000 / 0.818 / 0.179 | 0.794 / 0.961 | 0.500 | 0.302 / 0.562 / 0.833 / 0.506 / 0.587 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 修复 3：typed 关系计分 | 0.050 / 0.425 / 0.700 / 0.299 | 0.000 / 0.818 / 0.178 | 0.794 / 0.961 | 0.500 | 0.302 / 0.562 / 0.833 / 0.506 / 0.587 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |

v2_40 与 CI 冒烟在每一步的全部指标（R@1/3/5/10、MRR、nDCG、无答案误命中）都与 step-2 基线逐位相同；graph_v1 graph off 也不变。

### 修复 1：relaxed 恢复路径也做图扩展

- 做法：`run_query_v2` 把种子阶段的图扩展封装为同一个闭包，交给 `QueryExecutionContext`；`wiki_relaxed` 分支在 relaxed 候选打分后、排序前调用它。仍只在 active 非 raw scope 执行，沿用同一快照、过滤边界、两跳 / 每种子 64 个扩展、15% 比例上限与 0.75 纯图上限；第二次调用复用种子阶段已建好的候选边界与图。
- 结果：NL 组 R@10 0.600 → 0.675，但 R@3 0.425 → 0.400，MRR 基本持平（0.277 → 0.279）；direct、keyword-anchor、无答案不变。只有 5 条 NL case 名次变化：`dep-tariff-consumers-zh`（未命中 → 第 9）、`dep-pickup-queue-double-dispatch`（未命中 → 第 10）、`spec-retry-origins`（第 8 → 第 6）变好；`dep-pickup-queue-channels`（第 3 → 第 4）、`owner-journal-exporter`（第 4 → 第 6）变差。
- 原因：relaxed 候选的 BM25 + 加分量级通常为 2–12，而纯图页面上限固定为 0.75，所以图只能对已在 relaxed 候选里的页面按其自身分数加 ≤15%，高连接度页面（如 routing-overview、ledger-sync）获益更多，会越过金标；纯图页面只能落在尾部。没有为此放宽上限。
- 延迟：2000 页合成 vault（`scale.py` 同源数据），走 relaxed 的三条自然语言查询中位数 170 ms → 243 ms（graph off 93 ms）；strict 查询 166/164 ms 不变。新增开销主要是对大量 relaxed 种子执行 `apply_graph_expansion`。

### 修复 2：纯图页面按原始证据排序

- 规则：有词法/向量信号的候选仍按自身分数的 15% 截断（不变）。纯图页面（没有词法/向量分）改为累计未截断的原始证据 `E = Σ 关系分 × 1/hop`（跨种子、跨跳数累加，关系分权重不变：直接链接 3.0、每个共享来源 4.0、公共邻居 1.5/ln(度+1)、同类型 1.0），再单调映射 `0.75 × E / (E + 3.0)`。一个种子的单条直接链接为 0.375，证据越多越接近但永不等于 0.75；只有证据完全相同时才落到路径序。
- 为什么不直接换成字典序分级（直接链接 > typed > 共享来源 > 公共邻居 > 同类型）：试过把共享来源降为 2.0、同类型降为 0.5 使权重符合该顺序，graph_v1 总体只从 MRR 0.506 变到 0.507、有涨有跌（`owner-journal-exporter`、`kw-invoice-assembly-dependents` 各升 1 名，`kw-journal-exporter-owner` 降 1 名），不足以支持多改两个权重，因此保持原权重，只改截断方式。
- 结果：keyword-anchor R@10 0.591 → 0.818、MRR 0.146 → 0.179；NL R@10 0.675 → 0.700、MRR 0.279 → 0.299；总体 R@1 不变、R@10 0.771 → 0.833、MRR 0.490 → 0.506；direct 与无答案不变。
- 变差的 case（相对修复 1）：`kw-exporter-concepts`（第 3、2 → 第 8、7）、`kw-pickup-queue-dedup`（第 3 → 第 6）、`kw-invoice-assembly-dependents`（第 7、5 → 第 6、10）、`dep-pickup-queue-double-dispatch`（第 10 → 未进 top-10）。原因相同：这些金标以前靠路径序排在前面（`wiki/concepts/` 字母序最靠前），而它们与种子只有一条直接链接；现在同样直接相连、又有公共邻居 / 同类型 / 多种子证据的 entity 页面分数更高。这是去掉字母序偶然优势后的真实排序，不是 fixture 可以修正的问题；图是无向的，也区分不了“依赖方 / 被依赖方”。

### 修复 3：typed 关系作为图证据

- 规则：`derived_from`（shared-spec origin 的 `path`，或纯路径）权重 3.0，与正文直接链接相同，因为它是经 `wiki_manage_shared_spec` 校验、指向具体项目 spec 的显式来源声明；`related_objects` 权重 2.0，是较松的关联（值可能是对象名而非页面）。目标与 wikilink 用同一套解析（页面相对 → `wiki/` 相对 → vault 相对 → 候选集内唯一 stem），双向，并作为一跳可遍历边；不参与公共邻居度数。`applies_to` 在本仓库是适用标签（`languages`/`frameworks`），不指向任何页面，因此只以 `key:label` 存储、不计分。检索库 schema 升为 4。
- 结果：graph_v1 基本持平、略负：总体 MRR 0.50581 → 0.50569、nDCG 0.58737 → 0.58728，其余指标与各组 R@1/R@10 不变。变化的只有三条：`kw-retry-budget-origins` 变好（第 8、9 → 第 7、8，retry-budget 与两个项目 spec 的 `derived_from` 边生效）；`kw-invoice-assembly-dependents` 变差（ledger-sync 第 6 → 第 7：money-decimal、utc-timestamps 通过 `derived_from` 连到同为种子邻居的 invoice-totals/invoice-dates，证据增加后排到金标前面）；`direct-carrier-latency` 第二个金标第 7 → 第 8（首位不变）。
- 为什么 `frontmatter-only` 组没有改善：两条 case 中 `spec-harbor-errors-shared` 在修复前已由 relaxed 词法排在第 2；`kw-harbor-errors-shared` 中 error-envelope 现在确实经 `derived_from` 边从 api-errors 扩展到（纯图 0.375 分），但 top-10 其余位置被词法 / 标题分约 1.2–1.5 的候选占满，纯图页面整体低于任何词法候选，这是修复 2 保留的上限设计，不是 typed 边缺失。没有为提高该组分数调权重。

## 复现

```bash
# graph_v1（active）：复制 vault、建索引，再分别跑开/关
cp -r tests/fixtures/retrieval/graph_v1/vault /tmp/graph_v1 && uv run llm-wiki-mcp index build --vault /tmp/graph_v1
uv run llm-wiki-mcp retrieval-eval --vault /tmp/graph_v1 --dataset tests/fixtures/retrieval/graph_v1/cases.jsonl --output-dir /tmp/g-on --no-context-budget --repeats 3
uv run llm-wiki-mcp retrieval-eval --vault /tmp/graph_v1 --dataset tests/fixtures/retrieval/graph_v1/cases.jsonl --output-dir /tmp/g-off --no-context-budget --repeats 3 --no-graph-expansion
```

已用上述 CLI 命令在 `c991b35` 上复核 graph_v1：R@1/R@10/MRR@10/nDCG@10/无答案误命中率与总表一致（CLI `index build` 同时构建 raw 索引，但本 fixture 的 raw 来源桩未改变任何结果）。

v2_40 需要先提交为 archive bundle 并改写标签路径，步骤与 `tests/retrieval/test_retrieval_eval.py::test_reviewed_v2_forty_case_fixture_is_archive_only` 相同；本表由等价的 Python 驱动（同一 `run_retrieval_evaluation` 接口）生成。

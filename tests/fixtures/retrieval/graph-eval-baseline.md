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

## 实验：纯图页面相对种子上限（branch `exp/graph-relative-cap`）

在修复 3（`5836c58`）之上，把纯图页面的固定 0.75 上限换成相对上限：`纯图分 = ratio × 最强贡献种子的相关分 × E / (E + 3)`，其中“相关分”取该种子的 `max(fusion, keyword, vector)`（不含图分），“贡献种子”是对该页原始证据 > 0 的种子，E 与修复 2 相同（跨种子、跨跳累加）。有词法/向量信号的候选仍按自身分数 15% 截断；typed 关系权重、fixture 与金标均未改动。评测口径同上（`repeats=3`）。“仅 strict 种子”变体只在 strict 种子阶段用相对上限，relaxed 恢复阶段保留修复 2 的固定上限。

| 阶段 | NL R@1 / R@3 / R@10 / MRR | keyword-anchor R@1 / R@10 / MRR | direct R@1 / MRR | 无答案误命中 | graph_v1 on 总体 R@1 / R@3 / R@10 / MRR / nDCG | graph_v1 off 总体 R@10 / MRR | v2_40 R@1 / MRR | CI R@1 / MRR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 修复 3（5836c58，固定 0.75） | 0.050 / 0.425 / 0.700 / 0.299 | 0.000 / 0.818 / 0.178 | 0.794 / 0.961 | 0.500 | 0.302 / 0.562 / 0.833 / 0.506 / 0.587 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 相对 0.3 | 0.050 / 0.425 / 0.675 / 0.297 | 0.000 / 0.818 / 0.184 | 0.794 / 0.961 | 0.500 | 0.302 / 0.562 / 0.823 / 0.506 / 0.586 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 相对 0.5 | 0.050 / 0.425 / 0.725 / 0.306 | 0.000 / 0.909 / 0.220 | 0.794 / 0.961 | 0.500 | 0.302 / 0.573 / 0.865 / 0.518 / 0.603 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| **相对 0.7（采用）** | 0.050 / 0.475 / 0.750 / 0.327 | 0.000 / 0.909 / 0.256 | 0.794 / 0.961 | 0.500 | 0.302 / 0.604 / 0.875 / 0.535 / 0.619 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 相对 0.9 | 0.050 / 0.425 / 0.750 / 0.320 | 0.000 / 0.909 / 0.266 | 0.794 / 0.961 | 0.500 | 0.302 / 0.583 / 0.875 / 0.535 / 0.617 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 相对 0.5，仅 strict 种子 | 0.050 / 0.425 / 0.700 / 0.299 | 0.000 / 0.909 / 0.220 | 0.794 / 0.961 | 0.500 | 0.302 / 0.573 / 0.854 / 0.515 / 0.597 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
| 相对 0.7，仅 strict 种子 | 0.050 / 0.425 / 0.700 / 0.299 | 0.000 / 0.909 / 0.256 | 0.794 / 0.961 | 0.500 | 0.302 / 0.583 / 0.854 / 0.524 / 0.603 | 0.604 / 0.459 | 0.750 / 0.826 | 0.700 / 0.800 |
graph_v1 总体四位小数（R@1 / R@3 / R@5 / R@10 / MRR / nDCG）：修复 3 0.3021 / 0.5625 / 0.6354 / 0.8333 / 0.5057 / 0.5873；0.3 0.3021 / 0.5625 / 0.6458 / 0.8229 / 0.5060 / 0.5861；0.5 0.3021 / 0.5729 / 0.6771 / 0.8646 / 0.5181 / 0.6025；0.7 0.3021 / 0.6042 / 0.6979 / 0.8750 / 0.5351 / 0.6189；0.9 0.3021 / 0.5833 / 0.7292 / 0.8750 / 0.5345 / 0.6166；0.5 仅 strict 0.3021 / 0.5729 / 0.6562 / 0.8542 / 0.5152 / 0.5973；0.7 仅 strict 0.3021 / 0.5833 / 0.6771 / 0.8542 / 0.5235 / 0.6032。所有变体的 v2_40、CI 冒烟全部指标及 graph_v1 graph off 与修复 3 逐位相同，无答案误命中均为 0.500。0.9 是在 0.3/0.5/0.7 之后额外补跑、用于确认 0.7 不是网格边界上的单调趋势：它的 MRR/nDCG 略低于 0.7，因此不再往上试。

- 采用：ratio = 0.7，strict 与 relaxed 两个阶段都用（`PURE_GRAPH_SEED_RATIO`；传 `pure_seed_ratio=None` 可回到固定上限）。它满足全部验收条件：graph_v1 R@1 不降（0.3021），MRR 0.5057 → 0.5351、nDCG 0.5873 → 0.6189；direct R@1 0.794、MRR 0.961 不变；无答案误命中 0.500 不变；v2_40 与 CI 逐位相同。0.3 使 MRR 基本不变、nDCG 下降，未通过；两个“仅 strict 种子”变体通过但弱于全阶段 0.7（NL 组不变，收益只来自 keyword-anchor）。
- 相对修复 3 变好的 case：`kw-harbor-errors-shared`（未命中 → 第 2，error-envelope 经 `derived_from` 扩展到后不再被上限压在所有词法候选之下）、`kw-retry-budget-origins`（第 7、8 → 第 2、3）、`dep-event-store-feeders`（第 9、10 → 第 2、3）、`dep-throttler-troubleshooting`（未命中 → 第 6）、`dep-tariff-consumers`（未命中 → 第一个金标第 10）。
- 变差的 case：`dep-invoice-assembly-downstream`（第二个金标第 10 → 未进 top-10）；`direct-cut-off` 第二个金标第 3 → 第 5、`direct-token-bucket` 第二个金标第 2 → 第 4（首位不变，所以 direct R@1/MRR 不变，但 direct R@3 0.971 → 0.912、nDCG 0.961 → 0.951）；`dep-exporter-concepts`（两个金标第 —、10 → 10、—）与 `spec-retry-origins`（第 6、4 → 第 7、4）MRR 不变。原因：强种子的纯图邻居现在可以排到弱词法候选前面，direct 查询里首位种子的图邻居因此插到了第二个金标之前。
- 延迟：2000 页合成 vault，同一会话交替运行 3 次（固定上限 / 0.7）：strict graph_on 中位数 166/176/172 ms vs 178/171/170 ms，relaxed NL 查询 278/279/280 ms vs 288/287/283 ms（约 +7 ms）。本次会话整体比修复 3 当时的测量（约 170 / 260 ms）慢，比较以同一会话交替运行为准。

## 向量嵌入文本：标题 + 标题路径前缀（branch `feat/embed-and-write-hints`）

在 `b1aec05` 之上，向量记录的嵌入文本从 passage 正文改为 `标题\n标题路径\n正文`（WeKnora `Chunk.EmbeddingContent` 思路）。本机可运行本地 BGE-M3（`BAAI/bge-m3`，`max_sequence_length=256`，CPU，模型下载到盒子内 `/workspace/vec/bge-m3`，仅用于评测，未改动依赖），因此做了实测。前后两次都用同一模型、同一 fixture 副本、`repeats=1`（向量排序是确定的），graph expansion 开。graph_v1 73 个向量 passage，CI 冒烟 4 个。

| fixture / 模式 | 前：R@1 / R@3 / R@5 / R@10 / MRR / nDCG / 无答案误命中 | 后：R@1 / R@3 / R@5 / R@10 / MRR / nDCG / 无答案误命中 |
| --- | --- | --- |
| graph_v1 lexical | 0.3021 / 0.6042 / 0.6979 / 0.8750 / 0.5351 / 0.6189 / 0.500 | 同左（逐位相同） |
| graph_v1 vector | 0.3021 / 0.4479 / 0.6146 / 0.9062 / 0.4990 / 0.5895 / 0.750 | 0.3229 / 0.4583 / 0.6458 / 0.8958 / 0.5151 / 0.6046 / 0.500 |
| graph_v1 hybrid | 0.3438 / 0.5000 / 0.6458 / 0.9062 / 0.5309 / 0.6163 / 0.750 | 0.3438 / 0.5000 / 0.6875 / 0.8958 / 0.5326 / 0.6197 / 0.500 |
| CI lexical | 0.7000 / 0.8000 / 0.8000 / 0.8000 / 0.8000 / 0.8000 / 0.000 | 同左 |
| CI vector | 0.5000 / 0.8000 / 0.8000 / 0.8000 / 0.7000 / 0.7262 / 0.000 | 0.7000 / 0.8000 / 0.8000 / 0.8000 / 0.8000 / 0.8000 / 0.000 |
| CI hybrid | 0.7000 / 0.8000 / 0.8000 / 0.8000 / 0.8000 / 0.8000 / 0.000 | 同左 |

- v2_40 只在 archive scope 评测，而 Query V2 的向量召回只对 active scope 生效（archive/raw 直接跳过），`vector_index_records` 也排除 `wiki/sources/`，所以 v2_40 的 vector/hybrid 结果不受该改动影响、未测量；其 lexical 指标与改动前逐位相同。
- graph_v1 vector：13 条 case 变好、4 条变差（22 条名次有变化）。变好如 `spec-retry-origins`（第 7、4 → 第 2、3）、`kw-tariff-calculator-consumers`（第 7、8 → 第 4、5）、`kw-throttler-incident`（未进 top-10 → 第 6）、`owner-lane-picker-heuristic`（第 2 → 第 1），无答案 `none-payroll` 不再误命中；变差为 `dep-pickup-queue-double-dispatch`（第 8 → 未进 top-10）、`owner-lane-catalog`（第 3 → 第 6）、`kw-exporter-concepts`（第二个金标第 4 → 未进 top-10）、`kw-retry-budget-origins`（第二个金标第 6 → 第 7）。
- graph_v1 hybrid：11 条变好、6 条变差；除上述外 `kw-lane-catalog-owner`（第 3 → 第 4）与 `direct-carrier-credentials`（第 1 → 第 3）变差，因此 hybrid 下 direct 组 MRR 1.000 → 0.961。
- 结论：两套 fixture 语料都很小（73 / 4 个 passage），变化方向总体为正（vector MRR +0.016、nDCG +0.015，无答案误命中 0.75 → 0.50），但 R@10 −0.010，hybrid direct 有一条回退；不足以作为大语料上的收益证据。
- 延迟：查询路径未改动（query embedding 不变）；单次运行的 vector p95 为 105 → 132 ms（graph_v1）、49 → 46 ms（CI），属于单次 CPU 测量噪声范围，未做重复测量。全量建库耗时 9.1 s → 10.1 s（graph_v1，73 passage，嵌入文本变长）。

## 写入提示：未链接提及（`link_suggestions`）

不影响检索：graph_v1 / v2_40 / CI 的 lexical 指标（graph 开/关）与 `b1aec05` 逐位相同。评测脚本在 `/workspace/graph-eval-work/mention_eval.py`、`mention_zh.py`（不入库）。

- graph_v1 剥链测试：对 33 个含正文 wikilink 的页面，把每个能唯一解析到其他页面的 `[[target]]` 替换为纯文本（变体 A：目标页标题；变体 B：文件名短语，如 `retry-budget` → “retry budget”），再对剥链后的正文生成提示，与被剥掉的目标比较（每页每目标计一次）。两种变体结果相同：TP 70、FP 1、FN 0，precision 0.986、recall 1.000。唯一的 FP 是 `invoice-builder` 里原有的 “Invoice dates follow …” → `invoice-dates`（Invoice Dates Spec），属于正文中本来就存在的未链接提及，按严格口径计为 FP。对未剥链的原始页面运行，也只产生这一条提示。
- 局限：替换文本就是标题或文件名短语，所以 recall 1.000 只说明匹配机制可靠，不代表真实写作中的召回（复数、改写、缩写不会被匹配）。graph_v1 页面标题都是英文，fixture 中没有别名。
- 中文/混排合成检查（13 个页面，含单字标题“票”、两个页面共享别名“队列”、“发票” ⊂ “发票明细”、代码块与行内代码、中英别名；4 个正文剥链）：TP 12、FP 2、FN 0，precision 0.857、recall 1.000。两条 FP 都是正文中真实存在但未作为金标的提及（“每张发票都带…” → 发票；“与 Circuit Breaker 无关…” → 熔断器）。单字标题、共享别名、代码中的名字、`carrier gatewayed` 这类非词边界都没有产生提示。该集合是为验证规则而构造的，不能当作真实精度估计。
- 写入延迟（2000 页合成 vault，单次 `save_obsidian_note` 与 `apply_update`，每轮 6 次写入/5 次 apply 取中位数，同一会话前后交替 3 轮）：write_note 中位数 415/426/430 ms → 443/426/454 ms，apply 424/420/413 ms → 436/430/429 ms。单独测量提示本身：读取 2000 个标题/别名约 9–10 ms，匹配约 6–7 ms，合计约 16 ms。

## 写入提示：近似重复标题（`duplicate_warnings`）

不影响检索：三套 fixture 的 lexical 指标（graph 开/关）与 `b1aec05` 逐位相同。评测脚本 `/workspace/graph-eval-work/dup_eval.py`（不入库）。

- 数据：graph_v1 vault 加 14 个中文标题页面（含别名“承运商接入”“DLQ”，以及 “发票”/“发票明细”、“调度队列”/“消息队列” 这类共享字的不同页面）。正例 40 条：28 条 graph_v1 标题变体（复数、连字符、加后缀如 “Service/Job/Strategy/Rules”、单字母拼写错误、去掉 “Study/Rule” 等尾词）与 12 条中文变体（加 “组件/服务/设计/模式/规则/原则/策略/表”、删字 “承运网关”、别名 “DLQ”、完全相同的 “幂等键”）。负例：67 个已有标题逐一对其余页面做 leave-one-out（都是不同页面，任何警告都计 FP），外加 16 条同主题但不同的新标题（如 “Rate Limiting Policy”、“Invoice Numbering Spec”、“发票作废”、“限速策略”）。正例只看期望页面是否出现在警告中，警告里的其他页面也计 FP。

| 阈值 | 仅 Jaccard：TP / FP / FN，P / R | Jaccard + 包含关系（采用）：TP / FP / FN，P / R |
| --- | --- | --- |
| 0.4 | 未测 | 40 / 5 / 0，0.889 / 1.000 |
| 0.5 | 39 / 1 / 1，0.975 / 0.975 | 39 / 1 / 1，0.975 / 0.975 |
| **0.6** | 36 / 1 / 4，0.973 / 0.900 | **39 / 1 / 1，0.975 / 0.975** |
| 0.7 | 未测 | 39 / 1 / 1，0.975 / 0.975 |
| 0.8 | 未测 | 38 / 1 / 2，0.974 / 0.950 |

- 0.6 下唯一 FP 是负例 “Rate Limiting Policy” → Rate Limiting（包含关系，得分 0.688）：按预先标注计为 FP，但实际很可能就是同一主题。唯一 FN 是 “承运网关” → 承运商网关（Jaccard 0.4，也不是子串）。0.4 时 leave-one-out 出现 “Billing Team”/“Routing Team”、“Invoice Dates Spec”/“Invoice Totals Spec” 互相告警，所以不取更低阈值。仅用 Jaccard 时漏掉的 3 条都是短标题加后缀（“Rate Engine Service”、“限流器组件”、“熔断器模式”），因此加了包含关系规则（较短一方 ≥ 3 个归一化字符、占较长一方 ≥ 0.5；因此 “发票” 与 “发票明细” 不会互相告警）。
- 置信度分层（`confidence`，评测脚本 `/workspace/graph-eval-work/dup_eval2.py`，不入库）：更正上面的归因——“Rate Limiting Policy” → Rate Limiting 实际是 `similar_title`（bigram Jaccard 0.688 ≥ 0.6），包含比 0.667。该例改标为“有歧义/相关”（不再当作纯负例），三种口径（阈值 0.6，同一次运行）：

  | 口径 | 全部警告：TP / FP / FN，P / R | 仅 `high`：TP / FP / FN，P / R |
  | --- | --- | --- |
  | 按原标注计负例 | 39 / 1 / 1，0.975 / 0.975 | 30 / 0 / 10，1.000 / 0.750 |
  | 计为正例 | 40 / 0 / 1，1.000 / 0.976 | 30 / 0 / 11，1.000 / 0.732 |
  | 排除（有歧义，采用） | 39 / 0 / 1，1.000 / 0.975 | 30 / 0 / 10，1.000 / 0.750 |

  该例现为 `related`。40 条警告中 `high` 30 条、`related` 10 条（该例 + 9 条被标为正例的“加词/加字”变体：“Rate Engine Service”“The Rate Engine”“Circuit-Breaker Pattern”“Exponential Backoff Strategy”“Lane Registry Service”“Ledger Sync Job”“Revenue Recognition Rules”“限流器组件”“熔断器模式”）；`high` 无误报。评测页面路径不在 `wiki/projects/` 下，项目页降级规则未被该评测覆盖（由单元测试覆盖）。
- 过程说明：包含关系规则和数据集是同一轮看结果后调整的，没有独立的留出集，数字偏乐观。第一轮数据里给 “承运商网关” 写的别名是 “Carrier Gateway 中文”，导致英文 “Carrier Gateway” 相关的正例和 leave-one-out 各多出与该中文页的告警（0.6 时 FP 4）；这是造数据时的别名重叠错误，改为 “承运商接入” 后重跑，上表为重跑结果。
- 没有对小 vault 跳过：精确/别名匹配在任何规模都有用，检查成本也很低（见下），误报率与 vault 大小无关。
- 写入延迟（2000 页合成 vault，同一会话前后交替 3 轮）：write_note 中位数 421/418/416 ms → 455/455/455 ms（约 +35–40 ms，其中重复检查约 20 ms、未链接提及约 16 ms，读取标题/别名只做一次）；apply 418/448/418 ms → 440/431/432 ms（apply 只做未链接提及）。

## 检索上下文（branch `feat/retrieval-context`）

排序指标：三套 fixture 的 lexical 指标（graph 开/关）与 `d6bb043`（= `b1aec05`）逐位相同，逐 case 排名相同（除非另有说明）。上下文评测脚本 `/workspace/graph-eval-work/ctx_eval.py`（不入库）：lexical、graph 开、`top_k=10`、带 context pack，统计 `results[].content`。
`longdoc` 是评测时临时构造的长文档 vault（WeKnora 仓库中 86 个 > 6 KB 的 Markdown 与本仓库 `docs/` 21 篇，共 107 页），查询为 120 个随机抽取、全库唯一的 H2/H3 小节标题，金标为该页；“小节覆盖”指该小节全部 passage 是否都出现在该页正文中（去掉空白与标点后按前 200 字符比对）。重复率 = 结果正文中已在同一响应前文出现过的 8 词 shingle 占比。

### 1. 上下文去重（相邻 chunk 重叠 + 跨页重复段落）

- 现状：结果按页去重（每页一个结果），但页内正文先放最佳 passage、再按阅读顺序补充；chunker 把上一块末尾最多 64 个 passage 单位用空格重新拼接后放在下一块开头，原先的 `_deduplicate_overlap` 按空白分词、最多比较 48 个词，碰到标点/中文时基本识别不到，并且只和“前一个打包的 passage”比较，最佳 passage 与其前后邻居的重叠从不去掉。模板化来源胶囊中完全相同的段落在每个结果页重复出现。
- 改动：按页内 `ordinal` 与已打包的前后邻居比较 passage 单位（8–128 个单位）并去掉重叠，最后按阅读顺序拼接；同一响应中已出现过的长段落（字母数字 ≥ 48，汉字计 2）在后续页省略，但页面的第一段 passage 永不因此变空。

| fixture | 正文 token（前 → 后） | 重复率（前 → 后） | 有正文的结果页 | 小节覆盖（passage / 全覆盖小节） |
| --- | --- | --- | --- | --- |
| graph_v1 | 11732 → 11732 | 0 → 0 | 476 → 476 | — |
| CI | 54 → 54 | 0 → 0 | 5 → 5 | — |
| v2_40（archive） | 4007 → 3548（−11.5%） | 16.8% → 3.6% | 249 → 249 | — |
| longdoc | 285130 → 264362（−7.3%） | 3.59% → 2.83% | 587 → 587 | 139/158、109/120 → 139/158、109/120 |

- 延迟：`pack_context` 在 longdoc 上每次查询 2.6 → 4.3 ms（端到端约 37 → 40 ms）；2000 页合成 vault（每页 1 个 passage）端到端中位数交替两轮 231/220 ms → 226/228 ms，噪声范围内。
- 实验（未采用）：对按页选出的结果做 MMR 重排（λ·相关度 − (1−λ)·与已选页的词集合 Jaccard），graph_v1 lexical nDCG@10 0.6189 → 0.6171（λ=0.9）/ 0.6136（0.7）/ 0.6120（0.5），MRR 0.5351 → 0.5358 / 0.5302 / 0.5285；CI 不变。nDCG 下降，未采用；上下文去重不改排序。

### 2. frontmatter `questions:` 进入检索索引

- 改动：可选 `questions`（字符串列表/单个字符串）并入页面第一个 passage 的 FTS `aliases` 列（bm25 权重 4）。没有该字段的页面 FTS 文本逐字节不变，现有三套 fixture 排序与 `d6bb043` 完全相同；无需 schema/索引版本升级。问题不进入正文与向量嵌入。
- 新增评测（不入库、不计入三套 fixture，脚本 `/workspace/graph-eval-work/qeval/q_eval.py`）：复制 graph_v1 vault，给 12 个页面各加 2 个问题（共 24 个，含 4 个中文），另写 24 条用户式查询（与存储的问题措辞不同但词汇相近，每页 2 条，金标为该页）。问题与查询都由同一人编写，结果对该功能有利，只说明机制有效，不代表真实分布。

| 查询集 / 模式 | 无 questions 的 vault | 加 questions 后 |
| --- | --- | --- |
| 问题查询 24 条 lexical | R@1 0.667 / MRR 0.714 / nDCG 0.743 | R@1 1.000 / MRR 1.000 / nDCG 1.000 |
| 问题查询 24 条 vector（BGE-M3） | R@1 0.708 / MRR 0.791 / nDCG 0.841 | R@1 0.750 / MRR 0.819 / nDCG 0.862 |
| 问题查询 24 条 hybrid | R@1 0.708 / MRR 0.791 / nDCG 0.841 | R@1 0.792 / MRR 0.846 / nDCG 0.883 |
| 原 graph_v1 52 条 lexical | MRR 0.5351 / nDCG 0.6189 / R@3 0.6042 | MRR 0.5323 / nDCG 0.6169 / R@3 0.5938 |
| 原 graph_v1 52 条 vector | MRR 0.5151 / nDCG 0.6046 | 不变 |
| 原 graph_v1 52 条 hybrid | MRR 0.5326 / nDCG 0.6197 | MRR 0.5316 / nDCG 0.6188 |

- 负面影响（内容变化引起，代码对无 questions 的 vault 无影响）：lexical 下 `dep-event-store-feeders` 首个金标 2 → 3，`owner-journal-exporter` 6 → 5（变好），另有多金标 case 的 nDCG 小幅变化；R@1 与 no-answer FP 不变。
- 列位置实验：问题并入**每个** passage 的 aliases 列时，原 graph_v1 lexical MRR 0.5306 / nDCG 0.6106（`dep-throttler-troubleshooting` 金标从第 6 掉出前 10）；并入 keywords 列（权重 3）时 0.5327 / 0.6127；只并入第一个 passage 的 aliases 列 0.5323 / 0.6169，问题查询三种方案都是 1.000。采用最后一种。
- 延迟：2000 页合成 vault（无 questions）交替两轮中位数 base 227/229 ms、head 229/235 ms，噪声范围内；建索引 2.88/2.81 s → 2.84/2.83 s。

### 3. 命中 passage 的相邻 passage 扩展（带预算）

- 现状：结果页分强/弱两类（页面最高分 ≥ 首页 0.6 倍为强）。强页面先放最佳 passage，再从页首按阅读顺序补全到每页 2400 token；弱页面只放最多 3 个候选 passage。longdoc 的 11 个小节覆盖不全的 case 中，9 个是金标页为强页面、正文约 2000 token、命中小节位于预算之外（从页首补全把预算用在了开头）；1 个金标页未进入结果，1 个金标页为弱页面。
- 改动：强页面未读 passage 放得下预算时行为完全不变；超出预算时先保留页首 passage（成本 ≤ 预算 1/4 时），再从最佳 passage（以及该页其它候选 passage）开始交替向后、向前扩展相邻 passage，直到预算用完，最后按阅读顺序输出。
- 结果（lexical，graph 开，top_k=10）：

| fixture | 正文 token | 重复率 | 小节 passage 覆盖 | 小节全覆盖 | 金标页页首 passage 覆盖 |
| --- | --- | --- | --- | --- | --- |
| longdoc 前（item 2 提交） | 264362 | 2.83% | 139/158 | 109/120 | 117/120 |
| longdoc 后 | 265178（+0.3%） | 2.76% | 156/158 | 118/120 | 117/120 |
| graph_v1 / CI / v2_40 | 不变 | 不变 | — | — | — |

- hybrid（BGE-M3）longdoc：小节 passage 覆盖 141/158 → 155/158，小节全覆盖 111/120 → 117/120，页首覆盖 118 → 119，正文 token 360385 → 361485（+0.3%）；graph_v1/CI/v2_40 hybrid 上下文不变。（本行在第 4 项提交中补记。）
- 变体实验：不保留页首、只围绕命中扩展时小节覆盖同为 156/118，但金标页页首覆盖从 117 降到 72/120，因此加入页首保留；只向后扩展（未交替）的实现因测试发现窗口偏移而改为交替。
- 弱页面的邻居扩展未实现：本次各评测中只有 1 个弱金标页 case（ld-17），没有可测的收益，额外 token 会加到低相关页面上。
- 注意：longdoc 查询是小节标题，天然偏向“命中小节”的覆盖；这个指标不衡量页首以外、命中位置以外的内容（例如长页面中部的其它小节）是否被挤出。
- 延迟：longdoc（107 页）每次查询中位数 item 2 为 31.4/31.9/30.9/31.3/31.8/30.4 ms，本项 31.4/31.2/31.9/31.1/30.4/30.1 ms；p90 为 74–77 ms vs 79–85 ms（60 条查询取第 54 个值，波动较大）。cProfile 下 65 条查询 `_build_page_ordered_context` 累计 0.150 s → 0.155 s、`run_query_v2` 累计 4.89 s → 4.82 s，差异在上下文构建上约 0.08 ms/查询。2000 页合成 vault 中位数 228.6/226.9 ms → 224.9/230.2 ms。

### 4. 可配置 RRF 权重

- 改动：`retrieval.ranking.rrf_weights`（`fts`/`title`/`vector`，0–10，默认 1.0）与环境变量 `LLM_WIKI_RRF_WEIGHTS`（部分覆盖，优先于配置文件）；`fusion_score` 的三项分别乘以权重。默认权重下 `1.0 / (k + rank)` 与原来的 `1 / (k + rank)` 是同一个浮点数，三套 fixture 排序逐位相同。`merge_coverage_items` 的来源 RRF 不加权；graph 扩展使用融合后的分数，因此间接受权重影响。
- 权重扫描（实验，默认值不变；脚本 `/workspace/graph-eval-work/sweep*.py`，不入库）。lexical，graph 开：

| 权重 (fts, title, vector) | graph_v1 R@1 / MRR / nDCG | graph_v1 direct R@1 | graph_v1 off MRR | v2_40 R@1 / MRR / nDCG | CI |
| --- | --- | --- | --- | --- | --- |
| 1, 1, 1（默认） | 0.3021 / 0.5351 / 0.6189 | 0.794 | 0.4590 | 0.750 / 0.8264 / 0.8559 | 0.7 / 0.8 |
| 1, 0, 1 | 0.3021 / 0.5502 / 0.6326 | 0.794 | 0.4590 | 0.917 / 0.9259 / 0.9306 | 不变 |
| 1, 0.5, 1 | 0.3021 / 0.5374 / 0.6219 | 0.794 | 0.4590 | 0.861 / 0.8981 / 0.9101 | 不变 |
| 1, 1.5, 1 | 0.3021 / 0.5245 / 0.6062 | — | — | — | — |
| 1, 2, 1 | 0.3021 / 0.5219 / 0.5996 | — | — | — | — |
| 0.5, 1, 1 | 0.3021 / 0.5261 / 0.6077 | — | — | — | — |
| 1.5, 1, 1 | 0.3021 / 0.5356 / 0.6205 | 0.794 | 0.4590 | 不变 | 不变 |
| 2, 1, 1 | 0.3021 / 0.5408 / 0.6264 | 0.794（off 0.853） | 0.4694 | 不变 | 不变 |

  所有配置 no-answer FP 不变（graph_v1 0.5，v2_40 0）。`title=0`（关闭标题候选的独立 RRF 项，FTS 的 title 列 bm25 权重 8 仍保留）逐 case：graph_v1 graph 开 9 个变好、1 个变差（`direct-carrier-latency` 第二个金标 8 → 9）；v2_40 6 个变好（`help-activities` 6 → 1、`help-workbook-builder` 4 → 1 等）、0 个变差；graph 关 3 个变好。

  hybrid（BGE-M3），graph_v1：

| 权重 (fts, title, vector) | R@1 / R@3 / MRR / nDCG | direct R@1 |
| --- | --- | --- |
| 1, 1, 1（默认） | 0.3438 / 0.5000 / 0.5326 / 0.6197 | 0.794 |
| 1, 1, 0 | 0.3125 / 0.5417 / 0.5163 / 0.5852 | — |
| 1, 1, 0.5 | 0.3438 / 0.4896 / 0.5225 / 0.6056 | — |
| 1, 1, 1.5 | 0.3438 / 0.6146 / 0.5610 / 0.6443 | 0.794 |
| 1, 1, 2 | 0.3438 / 0.6146 / 0.5640 / 0.6500 | — |
| 1, 0, 1 | 0.3229 / 0.6250 / 0.5603 / 0.6447 | 0.794 |
| 1, 0, 1.5 | 0.3229 / 0.6458 / 0.5648 / 0.6487 | 0.794 |

  CI 在所有 hybrid 配置下不变（R@1 0.7，MRR 0.8）。
- 延迟（默认权重）：2000 页合成 vault 中位数 item 3 231.8/227.5 ms → 233.3/226.9 ms，longdoc 30.4/30.6 ms → 31.3/30.8 ms，噪声范围内。
- 结论：按任务要求默认值保持 1.0。`title=0` 在 lexical 的三套 fixture 上都不变差（graph_v1 MRR +0.015、v2_40 R@1 +0.167），是修改默认值的候选，但权重是在这三套 fixture 上挑出来的，存在过拟合风险；hybrid 下 `title=0` 使 graph_v1 R@1 下降 0.021（一个 case）。是否改默认值需另行决定，最好先在真实查询集上确认。

## 复现

```bash
# graph_v1（active）：复制 vault、建索引，再分别跑开/关
cp -r tests/fixtures/retrieval/graph_v1/vault /tmp/graph_v1 && uv run llm-wiki-mcp index build --vault /tmp/graph_v1
uv run llm-wiki-mcp retrieval-eval --vault /tmp/graph_v1 --dataset tests/fixtures/retrieval/graph_v1/cases.jsonl --output-dir /tmp/g-on --no-context-budget --repeats 3
uv run llm-wiki-mcp retrieval-eval --vault /tmp/graph_v1 --dataset tests/fixtures/retrieval/graph_v1/cases.jsonl --output-dir /tmp/g-off --no-context-budget --repeats 3 --no-graph-expansion
```

已用上述 CLI 命令在 `c991b35` 上复核 graph_v1：R@1/R@10/MRR@10/nDCG@10/无答案误命中率与总表一致（CLI `index build` 同时构建 raw 索引，但本 fixture 的 raw 来源桩未改变任何结果）。

v2_40 需要先提交为 archive bundle 并改写标签路径，步骤与 `tests/retrieval/test_retrieval_eval.py::test_reviewed_v2_forty_case_fixture_is_archive_only` 相同；本表由等价的 Python 驱动（同一 `run_retrieval_evaluation` 接口）生成。

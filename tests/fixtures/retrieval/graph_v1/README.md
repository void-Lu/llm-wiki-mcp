# Query V2 图扩展敏感 Fixture（graph_v1，合成 CI/dev 用）

本 fixture 是**完全合成**的 active scope 互链 Wiki，用于在 CI 中验证评测链路，并在开发时对比 Query V2 图扩展开/关（graph on/off）的排序差异。它**不是生产 vault 基线**，指标只说明检索逻辑在该合成图上的行为，不能外推为真实知识库效果。

- `vault/`：56 个 `wiki/` 页面（entities、shared-specs、concepts、harbor/ledger/beacon 三个项目的 index/specs/architecture/troubleshooting/plans/researches），带正文 `[[wikilink]]`、共享 `sources`，以及 `related_objects`、`applies_to`、`derived_from` 等 typed frontmatter 关系；另有 5 个极短 `raw/sources/documents/...` 来源桩，仅用于让 `sources` 引用可解析，不参与 active 检索。
- `cases.jsonl`：52 条标注（48 条可回答 + 4 条无答案），全部使用默认 `knowledge`（active）scope。
- `cases.manifest.json`：数据集版本、阈值与分组计数；`status` 为 `synthetic_ci_fixture`。

## Case 分组（按 `tags`）

- `graph` + `natural-language`（20）：自然语言关系问题（“what depends on X”“which shared spec applies to Y”），金标页面主要通过链接/共享来源/typed 关系与查询所描述的种子页相连，查询词大多不出现在金标页面上。
- `graph` + `keyword-anchor`（11）：同类关系意图，但调用方只发送种子实体的短锚点短语；`notes` 记录原始意图。
- `direct`（17）：词法直查对照组，其中 `hub-competition` 表示金标叶子页与高连接度 hub 页竞争。
- `no-answer`（4）：含一条 `near-miss`（部分词命中但主题不存在）。
- 辅助标签：`inbound-link`/`outbound-link`/`two-hop`、`shared-source`、`typed-relation`/`shared-spec`、`frontmatter-only`（关系只在 frontmatter，正文无链接）、`no-link`（仅共享来源相连）、`chinese`。

标注刻意包含图扩展**可能帮不上或有害**的情形（`frontmatter-only`、`no-link`、`hub-competition`、无答案），不要为了让图扩展指标好看而调整标签或正文。

## 运行

CI 测试见 `tests/retrieval/test_retrieval_eval.py::test_graph_v1_*`。手工对比时先复制 vault 并构建 active 索引，再分别运行：

```bash
uv run llm-wiki-mcp index build --vault <copy-of-vault>
uv run llm-wiki-mcp retrieval-eval --vault <copy-of-vault> --dataset tests/fixtures/retrieval/graph_v1/cases.jsonl --output-dir <reports-on> --no-context-budget
uv run llm-wiki-mcp retrieval-eval --vault <copy-of-vault> --dataset tests/fixtures/retrieval/graph_v1/cases.jsonl --output-dir <reports-off> --no-context-budget --no-graph-expansion
```

`--no-graph-expansion` 只作用于 engine 入口的离线消融，不改变 MCP `wiki_query` 公共接口；graph on/off 报告不能互为 baseline（gate 检查 `graph_expansion_matches`）。已记录的开/关对比见 `../graph-eval-baseline.md`。

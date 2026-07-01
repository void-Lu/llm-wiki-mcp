---
title: wiki_build_source_index 改为按 frontmatter tags 分级嵌套索引（设计）
date: 2026-07-01
status: draft
owners: user
related_files:
  - src/netsuite_llm_wiki_mcp/wiki_source_index.py
  - tests/test_wiki_source_index.py
  - src/netsuite_llm_wiki_mcp/server.py
---

# wiki_build_source_index 改为按 frontmatter tags 生成分级嵌套索引

## 1. 背景与目标

当前 `wiki_build_source_index` 按 `_toc_manifest.json::tree[url].toc_path` 取一级/二级路径做扁平分组，把每个分组渲染成一份独立页（命名 `01-{slug}-NN.md` 平铺在 `target_dir`），并在 `target_dir/catalog.md` 汇总。这种组织方式与原始来源文档 frontmatter 中的 `tags`（形如 `suitecloud-platform/suitescript`、`suitescript-2-1-modules/n-action-module`，本身已是权威的多级分类路径）脱节，目录不直观、无法反映文档自身声明的归属。

目标：**改为以 raw 源文档 frontmatter 的 `tags` 为唯一分组主键，生成一棵以 `source_name` 为隐式根、嵌套反映 tag 路径的多级目录索引树**；每个内节点（有子分支的父类）建目录并写一份 `index.md`，所有叶子（无子分支，无论深度几）一律不建独立目录/index，直接作为父 `index.md` 的 `## {leaf}` 章节。一个文件有多个 tag 时在该树中重复镜像出现。

## 2. 关键决策摘要

| 维度 | 决策 |
|---|---|
| 分组主键 | 仅来自每个 raw 文档 frontmatter 的 `tags`（不再读 `toc_path` 作为分组依据） |
| 单条 tag 语义 | tag 即一条完整路径，沿 `/` 拆段；最后一段为「叶子」，之前各段为「父类路径」 |
| 文件归属次数 | 文件有 N 条 tag → 在 N 个位置出现（镜像），跨父类不去重 |
| 树根 | `source_name` 为隐式根，对应 `{target_dir}/index.md`（原 `catalog.md` 改名） |
| 内节点 | 有子分支的父类 → 建目录 + 写 `index.md`；每份正文按 child 列 `## child` 章节 |
| 叶子 | 任何树的叶子节点（无子）→ **不建目录、不写独立 index**，仅以 `## leaf` 章节进入父 index.md 的条目 |
| `source_name` 前缀 | tag 首段 slug 等于 `source_name` 时剥去该段（视为相对根的路径） |
| 该 tag 即整棵子树的根文档 | 具体说 tag 形如 `{source_name}/{branch}` 的文件 → 进入根 `index.md` 的 `## {branch}` 章节，作为该 branch 子树的根文档入口 |
| 入口签名 | 完全不变：`build_source_index(vault_root, source_root, source_name, target_dir=None, page_size=80, max_headings=12, refresh=True)` |
| 查询/索引后处理 | 不改 `wiki_query` / `refresh_indexes` 逻辑 |

## 3. 数据流与算法

### 3.1 入口与校验（保留）

`build_source_index` 的前置校验与解析完全沿用现状：
- `source_name` 非空校验
- `_resolve_source_root`：`source_root` 必须解析到 `raw/sources/` 子目录内
- `_resolve_target_dir`：默认 `wiki/sources/references/{slug(source_name)}`，必须相对且在 `wiki/sources/` 内，最终解析后不能逃出 `vault_root`
- `page_size ∈ [1,250]`、`max_headings ∈ [0,50]` 钳制
- `_load_toc_manifest` 仍读，但其内容只用于「排序辅助 + 索引正文展示」，不参与分组

### 3.2 Entry 收集（基本保留，强调 tags 角色）

复用 `_entry_for_file`，仍从 frontmatter 与 body 提取：
- `title`、`source`(url)、`toc_path`、`headings`、`published`、`type`、`depth`、`hash`、`keywords`、`tags`
- `tags` 不再仅进入 `keywords`，而是本次的核心分组主键
- entry 内的全部字段仅作索引正文展示与排序辅助；分组只看 `tags`

### 3.3 tag 规范化与剥离

对每个 entry 的每条 tag：

1. 沿 `/` 拆段，并对每段做 `slug()` 处理（保留现行 `wiki_paths.slug` 对中文/大小写/特殊字符的行为）。
2. 丢弃任意等于 `..`、为空、含反斜杠或 `slug()` 后为空的段。
3. 若整段序列为空 → 视为「无标签」路径 `("_ungrouped",)`。
4. 若首段（slug 后）等于 `slug(source_name)` → 剥去首段，剩余段作为相对根的路径。剥离后只剩 0 段（tag 恰为 `source_name`）→ 视为「根标记」，文件进入根 `index.md` 的一个虚拟章节 `## {source_name}` 内（罕见，仅为完备性）。
5. 剥离后保留的段序列记为该 (entry, tag) 对应的「tree path」`P`（>=1 段）。

### 3.4 构建索引树

1. 收集所有 `(entry, path_P)` 二元组（同一 entry 多 tag → 多个二元组）。
2. 用所有 `P` 的并集构造一棵以空段序列 `()` 为 root 的前缀树：root = `source_name` 隐式根。
3. 对任何路径 `P`，其「父节点」是 `P[:-1]`（去掉末段）；其「叶子段」是 `P[-1]`。
4. 节点类型：
   - **内节点**（interior）：在树里有至少一个子节点（即存在某条 `P` 使得 `P[:k]==节点路径` 且 `len(P)>k`）。
   - **叶子节点**（leaf）：没有任何子节点。
5. 文件条目的「落点」：对每个 `(entry, P)`：
   - 落入 `parent(P)` 节点对应的 index.md，作为 `## {P[-1]}` 章节下的一份条目。
   - 例：P=`("suitecloud-platform","suitescript")` → 落入根 index 的 `## suitescript` 章节。
   - 例：P=`("suitescript-2-1-modules","n-action-module","action-action")` → 落入 `n-action-module` index 的 `## action-action` 章。
   - 若同一 `(entry, P)` 因多条 tag 在同一父节点同一 `## leaf` 章节（即 leaf 段也相同）下重复出现 → 按 `raw_path` 去重为一条。
6. 没有任何有效 tag 的 entry → 落入 `("_ungrouped",)` 路径，写到 `{target_dir}/_ungrouped/index.md`（按 child-less 单节点处理：本身即父，章节为各 raw 条目，无子树导航）。

### 3.5 输出文件布局

```
{target_dir}/                                  # 默认 wiki/sources/references/{slug(source_name)}
  index.md                                     # 根 index（原 catalog.md，改名），source_name 隐式根
  _ungrouped/index.md                          # 仅有无 tag 文件时存在
  suitecloud-platform/index.md                # 父类层为「suitecloud-platform」下的所有 child
  suitescript/index.md                         # 父类层为「suitescript」
  suitecloud-platform/suitescript-2-x-api-reference/index.md
  suitescript-2-1-modules/index.md
  suitescript-2-1-modules/n-action-module/index.md
  ...
```

flat、同级一层是常情；父类前缀含多段（tag 本身超过 2 段或剥离后仍 >1 段）时自然嵌套。

### 3.6 index.md 的内容结构（所有 index 通用）

每份 `index.md`（根或任一内节点）正文：

```markdown
---
frontmatter:
  type: source_index
  title: <source_name>: <节点 tag 路径 或 "Root">
  generated: true
  source_name: <source_name>
  source_root: <source_rel>
  tag_path: <节点相对根的路径，根节点为 空字符串>
  index_kind: lightweight_source_index
  indexed_count: <本页条目数>
  total_node_count: <本节点为父的所有条目数（含分页全部）>
  summary: <简述>
  tags: ["netsuite", "source-index", "help-docs"]
  sources: [<source_rel>]
---

<导语 + Scope：列出 source_name / source_root / 节点tag路径 / 本页条目数 / chunk序号/总数>

## {child-1}              # 按 casefold 排序的每个直接子节点
<本节点的「根文档条目」：parent(路径)==本节点 && leaf==child-1 的 raw 文件>
<1..k 条 raw 条目：raw / url / toc / metadata / headings / keywords>
（若 child-1 为内节点）→ 追加导航行：
- → [[{child-1}/index|{child-1}/index]] (子树文档数: N)
（若 child-1 为叶子）→ 无导航行

## {child-2}
...
```

新增 frontmatter 字段 `tag_path`：节点相对根的路径，以 `/` 连接，根节点为空字符串。便于 `wiki_query` 在 `filter_tags` 等场景下更精确地过滤。

### 3.7 分页规则

- 当一个节点的 raw 条目数 > `page_size` 时分片：
  - 首页名 `index.md`，后续 `index-02.md`、`index-03.md`…
  - 每页正文按相同「Scope + 全部 `## child` 章节」骨架；条目按 child 顺序切片填充，保证同一 `## child` 的条目不跨页（若单个 child 的条目数 > `page_size`，则该 child 章节被切到多页的相同 `## child` 标题下，并在章节首行标注分片序号）。
- 去掉旧的 `01-{slug}-NN.md` 命名前缀。

### 3.8 根 index.md 的特殊处理

根节点也用统一的 index.md 模板。其 children 即全部深度 1 节点。每个 `## branch`：
- 「根文档条目」即「tag 形如 `{source_name}/{branch}`」的文件（剥离后 P=(branch,)，parent=() 即根），作为该 branch 子树的根节点入口；
- 若 `branch` 是内节点 → 附 `→ [[{branch}/index|{branch}/index]]`（含子树文档数）。
- 顶部不再单列「Tag branches」段落（与内节点结构保持一致），根 index 与其他 index 模板完全一致，简化逻辑。

### 3.9 清理与覆盖保护（沿用 `_clear_existing_generated_pages`）

- 写入前递归扫描 `target/**/*.md`：
  - frontmatter 非 `generated: true` → 返回 `{ok:false, code:"manual_page_exists", path, error}`，立即停止；
  - `generated: true` → 删除并加入 `deleted` 列表。
- 新版 `target` 不再是单层平铺，因此扫描必须用 `rglob("*.md")`（现状已如此），无需修改清理逻辑，能正确清掉旧版 `01-{slug}-01.md` 这类旧页与新版嵌套 `index.md`。

### 3.10 写入流程

1. 解析校验 → 收集 entry 列表 → 若无任何 markdown 源 → 仍返回 `no_markdown_sources`（沿用）。
2. 清理 target 下旧生成页 → 失败即返回。
3. 构造 tag-path 树（§3.4）。
4. 对每个内节点（含 root 节点）：
   - 计算该节点的 raw 条目集合（按 child 分桶）。
   - 按 `page_size` 分页 → 写 `index.md` / `index-NN.md`。
5. 若存在无 tag 文件 → 写 `_ungrouped/index.md`。
6. 收集 `written` 列表（含根 index 与所有内节点 index 与 _ungrouped/index）。
7. `refresh=True` 时调 `refresh_indexes` → 成功后 `refresh_overview`（沿用）。
8. `append_log_entry(operation="source_index", paths=written, sources=_source_manifest_paths(...))`（沿用）。

### 3.11 排序

- Entry 排序键：`(leaf 段 casefold, _toc_sort_key(entry), entry["title"].casefold(), entry["raw_path"].casefold())`，其中 `_toc_sort_key` 沿用现状（基于 `toc_path` 的拼接串）。
- 节点 children 排序：按 child 段 casefold 升序。
- 多个父类目录兄弟排序：按父类段 casefold（自然由前缀树遍历决定）。

## 4. 以真实数据样例验证

`action.Action.md` 的 5 条 tag（剥 `netsuite-help-docs` 前缀后，单条 tag 路径本身深度仍为 2，所以每条 tag 提供一对 (父类, 叶子)，并在树的不同深度形成一条链上的多个父子关系）：

| tag | 剥前缀后 P | 父节点 | 章节标题 | 落点 index.md |
|---|---|---|---|---|
| `suitecloud-platform/suitescript` | `(suitecloud-platform, suitescript)` | `(suitecloud-platform,)` | `## suitescript` | `suitecloud-platform/index.md` |
| `suitescript/suitescript-2-x-api-reference` | `(suitescript, suitescript-2-x-api-reference)` | `(suitescript,)` | `## suitescript-2-x-api-reference` | `suitescript/index.md` |
| `suitescript-2-x-api-reference/suitescript-2-1-modules` | `(suitescript-2-x-api-reference, suitescript-2-1-modules)` | `(suitescript-2-x-api-reference,)` | `## suitescript-2-1-modules` | `suitescript-2-x-api-reference/index.md` |
| `suitescript-2-1-modules/n-action-module` | `(suitescript-2-1-modules, n-action-module)` | `(suitescript-2-1-modules,)` | `## n-action-module` | `suitescript-2-1-modules/index.md` |
| `n-action-module/action-action` | `(n-action-module, action-action)` | `(n-action-module,)` | `## action-action` | `n-action-module/index.md` |

树经合并后形成唯一一条深度链：`() → (suitecloud-platform) → ... `；但每个 tag 自身只贡献「父→叶」一对兄弟关系，文件在该链的不同节点 index 里各自出现一次（共 5 次，与 tag 镜像数一致）。

**关键点**：因为这些 tag 字符串本身就是 NetSuite 帮助文档 `breadcrumb/category` 风格的层级名（`suitecloud-platform → suitescript → suitescript-2-x-api-reference → suitescript-2-1-modules → n-action-module → action-action`），所以从所有文件的 tag 并集自然重构出完整层级；叶子段（如 `suitescript`、`action-action`）只要没有其他 tag 以其为前缀就只是章节而非目录。

`SuiteCloud Supported Records.md` 单 tag `suitecloud-platform/suitecloud-supported-records`：
- P = `(suitecloud-platform, suitecloud-supported-records)`，父 = `(suitecloud-platform,)`
- 因没有任何文件 tag 以 `suitecloud-supported-records/...` 开头，`suitecloud-supported-records` 是叶子 → 不建目录，条目直接进 `suitecloud-platform/index.md` 的 `## suitecloud-supported-records` 章节，与 `## suitescript`（来自 action.Action.md 的另一对）是兄弟章节。

## 5. 兼容性与回归

- **入口签名与默认值不变**：`server.py` 中 `wiki_build_source_index` 工具与 `build_source_index` 函数签名、参数默认值、返回字段名（`ok`、`source_name`、`source_root`、`target_dir`、`indexed_count`、`page_count`、`page_size`、`groups`、`written`、`cleared`、`index_result`、`overview_result`、`log_result`）全部保留。`groups` 字段语义从「旧 toc 分组」改为「tag-path 内节点列表」，每项为 `{"tag_path": "<相对根的路径>", "count": N}`，便于调用方一望即知树形。
- **路径安全**：所有段经 `slug()`、拒绝 `..`/空/反斜杠；最终路径 `is_relative_to(target_dir)` 校验；越界段直接整条 tag 丢弃。
- **覆盖保护**：`_clear_existing_generated_pages` 遇手写页一律拒绝并中止（沿用）。
- **查询/索引**：生成页保持 `type=source_index`、`generated=true`；`wiki_query(filter_type="source_index")`、`refresh_indexes`、`refresh_overview` 行为不变；新页含 `tag_path` 与 `tags` frontmatter，更利于过滤。
- **回滚**：删除 `target_dir` 下生成页即可回滚（与现状一致）。

## 6. 测试计划

调整 `tests/test_wiki_source_index.py`：

1. **`test_build_source_index_writes_queryable_source_pages`**（修改）：
   - 构造两个 raw 页，每页用一个两段 tag，父类不同。
   - 断言 `page_count == 3`（根 index + 2 个父类 index），`"wiki/sources/references/netsuite-help-docs/index.md"` 在 written 中（原 catalog 路径断言改名为 `index.md`）。
   - `wiki_query` 可发现性断言全保留（raw_path、heading、url 均出现于 context）。

2. **`test_build_source_index_refuses_to_overwrite_manual_pages`**（保留不变）：
   - 原断言不动；新嵌套写法不改变清理语义。

3. 新增 `test_build_source_index_mirrors_multi_tag_file`：
   - 单文件 3 条 tag（不同父类）→ 该文件在 3 个父类 index 中各出现一次（搜索每份 index.md 文档体，断言 `raw:` 路径出现 3 次跨不同文件）。

4. 新增 `test_build_source_index_root_document_when_tag_starts_with_source_name`：
   - 文件 tag = `netsuite-help-docs/suitecloud-platform`，`source_name="netsuite-help-docs"`。
   - 断言条目写入 `index.md`（根）的 `## suitecloud-platform` 章节，且无 `suitecloud-platform/index.md` 单独目录（如该子树无其他 child）；若再添加一条 tag `suitecloud-platform/xxx` 的文件 → 此时 `suitecloud-platform` 有子，应建 `suitecloud-platform/index.md`，且 `xxx` 作为其 `## xxx` 章节，原 root 文档仍在根 index 的 `## suitecloud-platform` 章节并附导航指向 `suitecloud-platform/index`。

5. 新增 `test_build_source_index_pure_leaf_no_directory`：
   - 单 tag 形如 `A/B/C` 且无任何文件 tag 以 `A/B/C/` 开头 → 仅在 `A/B/index.md` 的 `## C` 章节出现条目，不存在 `A/B/C/` 目录。

6. 新增 `test_build_source_index_paginates_node_pages`：
   - 让某节点条目数 > `page_size`，断言生成 `index.md` + `index-02.md`。

7. 新增 `test_build_source_index_ungrouped_when_no_tags`：
   - 文件无 tags → `_ungrouped/index.md` 存在且含条目；根 index 与其他 index 不含该条目。

新增的覆盖 `tag` 单段（如 `NetSuite`）、多段（深度 >2）解析的单元用例，以及路径安全用例（含 `..`、空段、反斜杠的 tag 应被安全处理或丢弃不抛异常）。

## 7. 范围外（YAGNI）

- 不增加 `group_by=tags|toc` 可切换参数；旧 `toc` 分组逻辑被本设计替代。
- 不写「虚拟中间节点」index（仅真实 tag 路径上的内节点才有 index）。
- 不修改 `wiki_query` / `refresh_indexes` 的过滤或排序语义。
- 不为索引页做 wikilink enrich（`[[...]]` 仅在 index.md 正文内手写到子节点 index 的导航）。
- 不引入新的外部依赖。

## 8. 实施要点（供 writing-plans 展开）

预计改动集中在 `wiki_source_index.py`：
- 新增 `_group_entries_by_tags(entries, source_name)` → 返回 `{node_path: list[entry]}` 与节点父子关系（用于内节点判定）。
- 新增 `_resolve_tag_path(raw_tag, source_name) -> tuple[str,...] | None`（含 slug/剥离/危险过滤）。
- 改 `_group_entries` → 上述 tags-based 实现；`_write_group_pages` → `_write_node_pages` 写嵌套目录。
- `_write_catalog_page` 改名为 `_write_root_index`，与各节点公用同一 `_write_node_index` 辅助。
- `_pages_by_group`（catalog 反查）删除，改为组路径直接生成链接。
- `_clear_existing_generated_pages` 不需改动（已递归）。
- `_entry_lines` 章节级正文与 `_group_body` 章节化身体保留并适配「每个 index 多 section」语义。
- `server.py`：若 docstring/工具描述有提及 catalog 名称，更新为 root index 格式；其它不动。
- `tests/test_wiki_source_index.py`：按 §6 改写。
# wiki_build_source_index 按 frontmatter tags 生成分级嵌套索引 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `wiki_build_source_index` 的分组主键从 `_toc_manifest` 的 toc_path 改为各 raw 文档 frontmatter 的 `tags`，并产出以 `source_name` 为隐式根、嵌套反映 tag 路径的多级目录索引树；有子分支的内节点建目录 + `_entries.md`，所有叶子仅作为父 `_entries.md` 的 `## leaf` 章节出现。

**Architecture:** 复用现有 entry 收集与清理逻辑；仅替换分组与写页两段。新增 `_resolve_tag_path` / `_entry_tag_paths` / `_build_tag_index_tree` / `_write_node_index` 等纯函数；`_write_catalog_page` 改名 `_write_root_index` 与各内节点 index 共用 `_write_node_index`；`_group_entries` / `_write_group_pages` / `_pages_by_group` 删除。

**Tech Stack:** Python 3.11+、PyYAML、pytest；MCP 文件路径见下方文件结构。

## Global Constraints

- 入口签名不变：`build_source_index(vault_root, source_root, source_name, target_dir=None, page_size=80, max_headings=12, refresh=True)`，`server.py` 中 `wiki_build_source_index` 工具签名与默认值不变。
- 路径安全：所有 tag 段经 `wiki_paths.slug` 处理；危险段（`.`, `..`, 含 `\`、slug 后为空）直接丢弃该 tag;最终写出路径必须 `is_relative_to(target_dir)`。
- 覆盖保护沿用：写入前递归扫描 `target/**/*.md`，遇非 `generated:true` 立即返回 `{ok:false, code:"manual_page_exists", path, error}`。
- 生成页 frontmatter 必须含 `type="source_index"`、`generated=true`、`source_name`、`source_root`、`tag_path`、`index_kind="lightweight_source_index"`、`indexed_count`、`total_node_count`、`summary`、`tags=["netsuite","source-index","help-docs"]`、`sources=[source_rel]`。
- 旧 `_toc_manifest.json` 仍读，仅用于「排序辅助 + 正文展示」的 `toc_path` 填充，不参与分组。
- 旧命名 `01-{slug}-NN.md` 与 `catalog.md` 被新命名 `_entries.md` / `_entries-02.md` 取代（删除 catalog 文件名）。
- 返回字段名与现有完全一致；`groups` 项形状变更为 `{"tag_path": <相对根路径，根为空>, "count": N}`。
- 不修改 `wiki_query` / `refresh_indexes` / `refresh_overview` / `wiki_log` 业务逻辑；不改 `server.py` 工具参数。
- 测试用 `pytest`，遵循 `tests/conftest.py` 环境隔离约定；不要新增运行时依赖。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|---|---|---|
| `src/netsuite_llm_wiki_mcp/wiki_source_index.py` | 核心模块：tag 规范化、树构建、各 index 写出 | Modify（重写部分函数） |
| `tests/test_wiki_source_index.py` | 端到端回归测试 | Modify |
| `src/netsuite_llm_wiki_mcp/server.py` | 工具注册（仅 docstring 提及 catalog 处更新） | Modify（仅 docstring） |

辅助不变模块（只读引用以确认兼容）：
- `src/netsuite_llm_wiki_mcp/wiki_paths.py::slug` — 用做 tag 段安全处理。
- `src/netsuite_llm_wiki_mcp/wiki_io.py::{split_frontmatter, write_wiki_page, WikiWriteError}`
- `src/netsuite_llm_wiki_mcp/wiki_models.py::WikiPage`
- `src/netsuite_llm_wiki_mcp/wiki_log.py::append_log_entry`
- `src/netsuite_llm_wiki_mcp/wiki_index.py::refresh_indexes`、`wiki_overview.py::refresh_overview`

---

## Task 1: 新增 tag 路径解析与树构建函数

**Files:**
- Modify: `src/netsuite_llm_wiki_mcp/wiki_source_index.py`（在 `_group_entries` 之前插入新函数；末尾替换 `_group_entries`）
- Test: `tests/test_wiki_source_index.py`（新增纯函数单元测试段）

**Interfaces:**
- Consumes: `from netsuite_llm_wiki_mcp.wiki_paths import slug`；`entry` 字典形状由现有 `_entry_for_file` 决定。
- Produces:
  - `_resolve_tag_path(raw_tag: Any, source_name: str) -> tuple[str, ...] | None`
  - `_entry_tag_paths(entry: dict[str, Any], source_name: str) -> list[tuple[str, ...]]`
  - `_build_tag_index_tree(entries: list[dict[str, Any]], source_name: str) -> tuple[dict[tuple[str, ...], list[dict[str, Any]]], set[tuple[str, ...]]`
    - 返回 `(section_entries, interior_nodes)`:
      - `section_entries[(parent_path, leaf)] = [entry, ...]`：每条 `(entry, path)` 落入 `parent=path[:-1]` 节点的 `## leaf` 章节，key 同 `(parent, leaf)` 按 `raw_path` 去重。
      - `interior_nodes`：所有「至少存在一条 path 以其为前缀且更长」的节点路径（含根 `()`），这些节点会建目录 + `_entries.md`。

- [ ] **Step 1: 在 `tests/test_wiki_source_index.py` 顶部新增纯函数导入与单元测试**

在现有 `from netsuite_llm_wiki_mcp.wiki_source_index import build_source_index` 行之后追加：

```python
from netsuite_llm_wiki_mcp.wiki_source_index import (
    _resolve_tag_path,
    _entry_tag_paths,
    _build_tag_index_tree,
)
```

在文件末尾追加：

```python
def test_resolve_tag_path_strips_source_name_prefix():
    assert _resolve_tag_path("netsuite-help-docs/suitecloud-platform", "netsuite-help-docs") == ("suitecloud-platform",)
    assert _resolve_tag_path("suitecloud-platform/suitescript", "netsuite-help-docs") == ("suitecloud-platform", "suitescript")
    assert _resolve_tag_path("N/A", "netsuite-help-docs") == ("n", "a")
    assert _resolve_tag_path("", "netsuite-help-docs") is None
    assert _resolve_tag_path(None, "netsuite-help-docs") is None


def test_resolve_tag_path_drops_dangerous_segments():
    assert _resolve_tag_path("..", "x") is None
    assert _resolve_tag_path(".", "x") is None
    assert _resolve_tag_path("a/../b", "x") == ("a", "b")
    assert _resolve_tag_path("a\\b", "x") is None
    assert _resolve_tag_path([1, 2], "x") is None


def test_resolve_tag_path_root_marker_when_tag_equals_source_name():
    assert _resolve_tag_path("netsuite-help-docs", "netsuite-help-docs") == ("netsuite-help-docs",)


def test_entry_tag_paths_mirrors_multiple_tags():
    entry = {
        "raw_path": "raw/x.md",
        "tags": [
            "suitecloud-platform/suitescript",
            "suitescript/suitescript-2-x-api-reference",
        ],
    }
    paths = _entry_tag_paths(entry, "netsuite-help-docs")
    assert paths == [
        ("suitecloud-platform", "suitescript"),
        ("suitescript", "suitescript-2-x-api-reference"),
    ]


def test_entry_tag_paths_ungrouped_when_no_valid_tags():
    entry = {"raw_path": "raw/y.md", "tags": []}
    paths = _entry_tag_paths(entry, "x")
    assert paths == [("_ungrouped", "<ungrouped>")]


def test_build_tag_index_tree_links_chain_through_interior_nodes():
    entries = [
        {
            "raw_path": "raw/a.md",
            "title": "A",
            "toc_path": ["t", "a"],
            "tags": [
                "suitecloud-platform/suitescript",
                "suitescript/suitescript-2-x-api-reference",
                "suitescript-2-x-api-reference/suitescript-2-1-modules",
                "suitescript-2-1-modules/n-action-module",
                "n-action-module/action-action",
            ],
        },
    ]
    section_entries, interior = _build_tag_index_tree(entries, "netsuite-help-docs")
    # 5 个 tag → 5 个 (parent, leaf) 章节，每个章节唯一一个 entry
    assert len(section_entries) == 5
    assert (((), "suitecloud-platform"), entries[0]) in [
        (k, v[0]) for k, v in section_entries.items()
    ]
    # 根、suitecloud-platform、suitescript、suitescript-2-x-api-reference、
    # suitescript-2-1-modules、n-action-module 均为 interior（有子）
    for node in [
        (),
        ("suitecloud-platform",),
        ("suitescript",),
        ("suitescript-2-x-api-reference",),
        ("suitescript-2-1-modules",),
        ("n-action-module",),
    ]:
        assert node in interior
    # action-action 是叶子节点，不出现在 interior 集合中
    assert ("n-action-module", "action-action") not in interior


def test_build_tag_index_tree_dedupes_same_raw_path():
    entries = [
        {
            "raw_path": "raw/a.md",
            "title": "A",
            "toc_path": [],
            "tags": ["suitecloud-platform/suitescript", "suitecloud-platform/suitescript"],
        },
    ]
    section_entries, interior = _build_tag_index_tree(entries, "x")
    assert section_entries[(("suitecloud-platform",), "suitescript")] == [entries[0]]
```

- [ ] **Step 2: 运行新增单测，应全部 FAIL**

Run: `pytest tests/test_wiki_source_index.py -v -k "resolve_tag_path or entry_tag_paths or build_tag_index_tree"`
Expected: ImportError / AttributeError — 函数尚未定义。

- [ ] **Step 3: 在 `wiki_source_index.py` 顶部 imports 末尾增加（如尚未导入 slug）**

定位到文件首部 `from netsuite_llm_wiki_mcp.wiki_paths import slug`。若已存在跳过；若缺失，在 wiki_paths 相关导入附近补：

```python
# 现有 imports
from netsuite_llm_wiki_mcp.wiki_paths import slug
```

- [ ] **Step 4: 在 `_group_entries` 函数定义前面插入三个新函数**

在 `_toc_sort_key` 函数定义之后插入：

```python
_UNGROUPED_MARKER = "_ungrouped"
_UNGROUPED_LEAF = "<ungrouped>"


def _resolve_tag_path(raw_tag: Any, source_name: str) -> tuple[str, ...] | None:
    """Normalize one frontmatter tag string into a tree path tuple.

    Splits on '/', slug-cleans each segment, drops dangerous/empty ones,
    strips a leading segment equal to source_name, and returns either a
    >=1 length tuple, the root-marker (slug(source_name),) when the tag
    equals source_name exactly, or None when no valid segment remains.
    """
    if not isinstance(raw_tag, str):
        return None
    parts: list[str] = []
    for seg in raw_tag.split("/"):
        stripped = seg.strip()
        if not stripped or stripped in {".", ".."} or "\\" in stripped:
            continue
        slug_seg = slug(stripped)
        if not slug_seg:
            continue
        parts.append(slug_seg)
    if not parts:
        return None
    source_slug = slug(source_name).casefold()
    if parts and parts[0].casefold() == source_slug:
        parts = parts[1:]
        if not parts:
            return (source_slug,)
    return tuple(parts)


def _entry_tag_paths(entry: dict[str, Any], source_name: str) -> list[tuple[str, ...]]:
    """Return all tag tree paths for an entry; mirrors across tags.

    No valid tag → returns [(_UNGROUPED_MARKER, _UNGROUPED_LEAF)] so that the
    parent ('_ungrouped',) becomes a standalone index carrying each ungrouped
    raw file as its own `## <title>` section similarly to multi-tag mirroring
    in tagged nodes.
    """
    raw_tags = entry.get("tags") or []
    paths: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for raw_tag in raw_tags:
        path = _resolve_tag_path(raw_tag, source_name)
        if path is None or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    if not paths:
        return [(_UNGROUPED_MARKER, _UNGROUPED_LEAF)]
    return paths


def _build_tag_index_tree(
    entries: list[dict[str, Any]],
    source_name: str,
) -> tuple[dict[tuple[tuple[str, ...], str], list[dict[str, Any]]], set[tuple[str, ...]]]:
    """Build the tag-path index tree.

    Returns:
        section_entries: maps (parent_path, leaf_segment) -> [entry, ...].
            Each (entry, path) pair lands in parent=path[:-1]'s index under
            section `## path[-1]`. Entries are deduped by raw_path per
            (parent_path, leaf_segment) key.
        interior_nodes: set of node paths that have at least one child
            subtree (i.e. appear as a strict prefix of some path), including
            the root (). Those nodes get a directory + _entries.md.
    """
    section_entries: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)
    all_nodes: set[tuple[str, ...]] = set()
    for entry in entries:
        for path in _entry_tag_paths(entry, source_name):
            for depth in range(len(path) + 1):
                all_nodes.add(path[:depth])
            parent = path[:-1]
            leaf = path[-1]
            key = (parent, leaf)
            if not any(e["raw_path"] == entry["raw_path"] for e in section_entries[key]):
                section_entries[key].append(entry)
    interior_nodes = {
        node for node in all_nodes
        if any(
            other != node
            and len(other) > len(node)
            and other[: len(node)] == node
            for other in all_nodes
        )
    }
    return section_entries, interior_nodes
```

注：`defaultdict`、`Any` 已在文件顶部导入，不需新增。

- [ ] **Step 5: 运行新增单测，确认全部 PASS**

Run: `pytest tests/test_wiki_source_index.py -v -k "resolve_tag_path or entry_tag_paths or build_tag_index_tree"`
Expected: PASS（6 个用例全过）。

- [ ] **Step 6: Commit**

```bash
cd C:\Users\26327\AppData\Local\netsuite-llm-wiki-mcp
git add src/netsuite_llm_wiki_mcp/wiki_source_index.py tests/test_wiki_source_index.py
git commit -m "feat(source_index): add tag-path resolver and index tree builder"
```

---

## Task 2: 新增节点 index 写出函数（替换 `_write_group_pages` 与 `_write_catalog_page`）

**Files:**
- Modify: `src/netsuite_llm_wiki_mcp/wiki_source_index.py`
- Test: `tests/test_wiki_source_index.py`（继续在文件末尾追加单测）

**Interfaces:**
- Consumes: Task 1 的 `_build_tag_index_tree` 返回值；`wiki_io.write_wiki_page`、`wiki_models.WikiPage`。
- Produces:
  - `_write_node_index(root, target, target_rel, source_name, source_rel, node_path, section_entries, interior_nodes, page_size) -> list[str]`：写 `node_path` 对应的目录 + `_entries.md`（或多页 `_entries-02.md`），返回写出文件的相对路径列表。`node_path` 是绝对路径元组，根 `()` 写到 `target_rel/_entries.md`，其它 `(a, b)` 写到 `target_rel/a/b/_entries.md`。
  - `_child_sort_key(child: str) -> str`：children 排序辅助。
  - 重命名 `_write_catalog_page` 为 `_write_root_index`，删除旧函数。

- [ ] **Step 1: 在 `tests/test_wiki_source_index.py` 末尾追加新 helper 单测**

```python
def test_write_node_index_creates_nested_directory_and_index_md(tmp_path: Path):
    from netsuite_llm_wiki_mcp.wiki_source_index import _write_node_index, _build_tag_index_tree

    root = tmp_path / "vault"
    create_wiki_root(root)
    target_rel = "wiki/sources/references/netsuite-help-docs"
    target = root / target_rel
    target.mkdir(parents=True)
    source_name = "netsuite-help-docs"
    source_rel = "raw/sources/references/docs"
    entries = [
        {
            "raw_path": "raw/sources/references/docs/a.md",
            "title": "A",
            "source": "https://example.com/a",
            "toc_path": ["x", "a"],
            "type": "article",
            "depth": 2,
            "published": "2026-06-19",
            "headings": ["## alpha"],
            "tags": ["suitecloud-platform/suitescript"],
            "hash": "0" * 64,
            "keywords": ["alpha"],
        },
    ]
    section_entries, interior = _build_tag_index_tree(entries, source_name)
    written = _write_node_index(
        root, target, target_rel, source_name, source_rel,
        node_path=("suitecloud-platform",),
        section_entries=section_entries,
        interior_nodes=interior,
        page_size=80,
    )
    assert written == ["wiki/sources/references/netsuite-help-docs/suitecloud-platform/_entries.md"]
    text = (target / "suitecloud-platform" / "_entries.md").read_text(encoding="utf-8")
    assert "## suitescript" in text
    assert "raw/sources/references/docs/a.md" in text
    assert "https://example.com/a" in text
    assert "## alpha" in text


def test_write_node_index_paginates_with_index_suffix(tmp_path: Path):
    from netsuite_llm_wiki_mcp.wiki_source_index import _write_node_index, _build_tag_index_tree

    root = tmp_path / "vault"
    create_wiki_root(root)
    target_rel = "wiki/sources/references/x"
    target = root / target_rel
    target.mkdir(parents=True)
    source_rel = "raw/sources/references/x"
    entries = [
        {
            "raw_path": f"raw/sources/references/x/f{i}.md",
            "title": f"Title {i}",
            "source": f"https://example.com/f{i}",
            "toc_path": [],
            "type": "",
            "depth": None,
            "published": "",
            "headings": [f"## h{i}"],
            "tags": ["leaf/cat"],
            "hash": f"{i:064d}",
            "keywords": [],
        }
        for i in range(3)
    ]
    section_entries, interior = _build_tag_index_tree(entries, "x")
    written = _write_node_index(
        root, target, target_rel, "x", source_rel,
        node_path=("leaf",),
        section_entries=section_entries,
        interior_nodes=interior,
        page_size=2,
    )
    assert set(written) == {
        "wiki/sources/references/x/leaf/_entries.md",
        "wiki/sources/references/x/leaf/_entries-02.md",
    }


def test_write_node_index_includes_navigation_link_to_interior_child(tmp_path: Path):
    from netsuite_llm_wiki_mcp.wiki_source_index import _write_node_index, _build_tag_index_tree

    root = tmp_path / "vault"
    create_wiki_root(root)
    target_rel = "wiki/sources/references/x"
    target = root / target_rel
    target.mkdir(parents=True)
    source_rel = "raw/sources/references/x"
    entries = [
        {
            "raw_path": "raw/sources/references/x/a.md",
            "title": "A",
            "source": "https://example.com/a",
            "toc_path": [],
            "type": "",
            "depth": None,
            "published": "",
            "headings": ["## ha"],
            "tags": ["parent/child/leaf"],
            "hash": "0" * 64,
            "keywords": [],
        },
    ]
    section_entries, interior = _build_tag_index_tree(entries, "x")
    written = _write_node_index(
        root, target, target_rel, "x", source_rel,
        node_path=("parent",),
        section_entries=section_entries,
        interior_nodes=interior,
        page_size=80,
    )
    assert written == ["wiki/sources/references/x/parent/_entries.md"]
    text = (target / "parent" / "_entries.md").read_text(encoding="utf-8")
    assert "## child" in text
    assert "[[wiki/sources/references/x/parent/child/index|child/index]]" in text
```

- [ ] **Step 2: 运行新单测，应 FAIL（函数未写）**

Run: `pytest tests/test_wiki_source_index.py -v -k "write_node_index_creates or write_node_index_paginates"`
Expected: ImportError / AttributeError。

- [ ] **Step 3: 在 `wiki_source_index.py` 中新增 `_write_node_index` 与 `_child_sort_key`**

在 `_write_group_pages` 与 `_write_catalog_page` 之间先插入新函数（两个旧函数稍后被 Task 3 删除）：

```python
def _child_sort_key(child: str) -> str:
    return child.casefold()


def _node_relative_path(node_path: tuple[str, ...]) -> str:
    return "/".join(node_path)


def _node_display_path(node_path: tuple[str, ...], source_name: str) -> str:
    return _node_relative_path(node_path) or source_name


def _write_node_index(
    root: Path,
    target: Path,
    target_rel: str,
    source_name: str,
    source_rel: str,
    node_path: tuple[str, ...],
    section_entries: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]],
    interior_nodes: set[tuple[str, ...]],
    page_size: int,
) -> list[str]:
    """Write the _entries.md (and pagination _entries-NN.md) for one tree node.

    `node_path` is an absolute tree path tuple (root node is ()).  Children
    are the unique direct child segments of `node_path`.  For each child we
    emit a `## {child}` section containing:
      - any raw entries whose (parent==node_path, leaf==child) landed here
      - a `-> [[child/.../index|child/.../index]]` navigation row when
        `(*node_path, child)` is itself an interior node.
    """
    children = {
        leaf for parent, leaf in section_entries.keys() if parent == node_path
    } | {
        path[len(node_path)] for path in interior_nodes
        if len(path) > len(node_path) and path[: len(node_path)] == node_path
    }
    children = sorted(children, key=_child_sort_key)

    node_rel_dir = "/".join((*Path(target_rel).parts, *node_path)) if node_path else target_rel
    section_blocks: list[tuple[str, list[dict[str, Any]], bool, str]] = []
    for child in children:
        direct_key = (node_path, child)
        child_entry_list = list(section_entries.get(direct_key, []))
        child_entry_list.sort(
            key=lambda entry: (
                _toc_sort_key(entry),
                str(entry["title"]).casefold(),
                str(entry["raw_path"]).casefold(),
            )
        )
        child_node_path = (*node_path, child)
        is_child_interior = child_node_path in interior_nodes
        child_index_rel = f"{node_rel_dir}/{child}/index"
        section_blocks.append((child, child_entry_list, is_child_interior, child_index_rel))

    if node_path == (_UNGROUPED_MARKER,):
        section_blocks = [
            (entry["title"], [entry], False, "")
            for entry in section_entries.get((node_path, _UNGROUPED_LEAF), [])
        ]
        section_blocks.sort(key=lambda block: block[0].casefold())

    grouped_entries = [entry for _, entries, _, _ in section_blocks for entry in entries]
    chunk_count = max(1, (len(grouped_entries) + page_size - 1) // page_size)
    written: list[str] = []
    node_dir = target.joinpath(*node_path) if node_path else target
    if node_dir.resolve() != target.resolve() and not node_dir.resolve().is_relative_to(target.resolve()):
        raise WikiWriteError(f"node dir escapes target: {node_dir}")

    entries_cursor = 0
    per_chunk = max(1, page_size) if page_size else len(grouped_entries)
    for chunk_index in range(1, chunk_count + 1):
        filename = "_entries.md" if chunk_index == 1 else f"index-{chunk_index:02d}.md"
        rel_path = f"{node_rel_dir}/{filename}"
        full_path = root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        # 取当前 chunk：先按 child 顺序填到 page_size
        chunk_entries: list[dict[str, Any]] = []
        while entries_cursor < len(grouped_entries) and len(chunk_entries) < per_chunk:
            chunk_entries.append(grouped_entries[entries_cursor])
            entries_cursor += 1
        body = _node_index_body(
            source_name, source_rel, node_path,
            section_blocks, chunk_entries, chunk_index, chunk_count,
        )
        title_tag = _node_display_path(node_path, source_name)
        title = f"{source_name}: {title_tag}"
        if chunk_count > 1:
            title += f" ({chunk_index}/{chunk_count})"
        page = WikiPage(
            relative_path=Path(rel_path),
            title=title,
            frontmatter=_frontmatter(
                title=title,
                source_name=source_name,
                source_rel=source_rel,
                summary=f"Lightweight source index for {title_tag}.",
                indexed_count=len(chunk_entries),
                total_node_count=len(grouped_entries),
                tag_path=_node_relative_path(node_path),
            ),
            body=body,
        )
        write_wiki_page(root, page)
        written.append(rel_path)
    return written


def _node_index_body(
    source_name: str,
    source_rel: str,
    node_path: tuple[str, ...],
    section_blocks: list[tuple[str, list[dict[str, Any]], bool, str]],
    chunk_entries: list[dict[str, Any]],
    chunk_index: int,
    chunk_count: int,
) -> str:
    lines = [
        "This page is a lightweight source index for raw documentation. It is intended for query discovery and targeted follow-up ingestion.",
        "",
        "## Scope",
        "",
        f"- Source name: `{source_name}`",
        f"- Source root: `{source_rel}`",
        f"- Node tag path: `{_node_relative_path(node_path) or '(root)'}`",
        f"- Documents on this page: {len(chunk_entries)}",
        f"- Page chunk: {chunk_index}/{chunk_count}",
        "",
    ]
    cursor = 0
    for child, child_entries, is_child_interior, child_index_rel in section_blocks:
        child_lines: list[str] = [f"## {child}", ""]
        chunk_child: list[dict[str, Any]] = []
        while cursor < len(chunk_entries) and chunk_entries[cursor] in child_entries:
            chunk_child.append(chunk_entries[cursor])
            cursor += 1
        for index, entry in enumerate(chunk_child, 1):
            child_lines.extend(_entry_lines(index, entry))
        if is_child_interior:
            child_lines.append(f"- → [[{child_index_rel}|{child}/index]]")
            child_lines.append("")
        if len(child_lines) > 2:
            lines.extend(child_lines)
    return "\n".join(lines).rstrip()
```

注: `_frontmatter` 现有签名不含 `tag_path`，Task 3 将在 `_frontmatter` 中加入 `tag_path` 参数；本 task 步骤暂让该调用显式传 `tag_path`，先令测试报错（预期 fails），下一步统一调整。

- [ ] **Step 4: 调整 `_frontmatter` 签名加入 `tag_path`**

在 `_frontmatter` 定义处改：

```python
def _frontmatter(
    title: str,
    source_name: str,
    source_rel: str,
    summary: str,
    indexed_count: int,
    total_group_count: int,
    tag_path: str = "",
) -> dict[str, Any]:
    return {
        "type": "source_index",
        "title": title,
        "generated": True,
        "source_name": source_name,
        "source_root": source_rel,
        "tag_path": tag_path,
        "index_kind": "lightweight_source_index",
        "indexed_count": indexed_count,
        "total_group_count": total_group_count,
        "summary": summary,
        "tags": ["netsuite", "source-index", "help-docs"],
        "sources": [source_rel],
    }
```

- [ ] **Step 5: 运行新单测确认 PASS**

Run: `pytest tests/test_wiki_source_index.py -v -k "write_node_index_creates or write_node_index_paginates"`
Expected: PASS（两个用例）。

- [ ] **Step 6: Commit**

```bash
git add src/netsuite_llm_wiki_mcp/wiki_source_index.py tests/test_wiki_source_index.py
git commit -m "feat(source_index): add _write_node_index for nested tag index pages"
```

---

## Task 3: 重写 `build_source_index` 主流程，删除旧分组与写页函数

**Files:**
- Modify: `src/netsuite_llm_wiki_mcp/wiki_source_index.py`（删除 `_group_entries`、`_write_group_pages`、`_write_catalog_page`、`_pages_by_group`；改写 `build_source_index`；调整 `_clear_existing_generated_pages` 不变；删除 `_group_body`）
- Test: `tests/test_wiki_source_index.py`（修改现有两测；新增多 tag 镜像、root 文档、无 tag、纯叶子用例）

**Interfaces:**
- Consumes: Task 1 + Task 2 输出。
- Produces: `build_source_index` 的新返回值，`groups` 项为 `{"tag_path": <相对根路径, 根为空>, "count": N}`。

- [ ] **Step 1: 重写现有两测与新增测试**

在 `tests/test_wiki_source_index.py` 中：

a) 替换 `test_build_source_index_writes_queryable_source_pages` 函数体：

```python
def test_build_source_index_writes_queryable_source_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source_root = root / "raw/sources/references/docs"
    first_url = "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/record.html"
    second_url = "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/search.html"
    _raw_page(
        root,
        "raw/sources/references/docs/SuiteScript/N_record Module.md",
        "N/record Module",
        first_url,
        "## record.create(options)\n\nCreate records.\n\n## record.submitFields(options)\n",
        tags=["suitecloud-platform/suitescript-2-x-api-reference/record"],
    )
    _raw_page(
        root,
        "raw/sources/references/docs/SuiteScript/N_search Module.md",
        "N/search Module",
        second_url,
        "## search.create(options)\n\nCreate searches.\n",
        tags=["suitecloud-platform/suitescript-2-x-api-reference/search"],
    )
    (source_root / "_toc_manifest.json").write_text(
        json.dumps({
            "tree": {
                first_url: {
                    "title": "N/record Module",
                    "type": "article",
                    "depth": 3,
                    "toc_path": ["SuiteCloud Platform", "SuiteScript", "SuiteScript 2.x API", "N/record Module"],
                    "published": "2026-06-19",
                },
                second_url: {
                    "title": "N/search Module",
                    "type": "article",
                    "depth": 3,
                    "toc_path": ["SuiteCloud Platform", "SuiteScript", "SuiteScript 2.x API", "N/search Module"],
                    "published": "2026-06-19",
                },
            }
        }),
        encoding="utf-8",
    )

    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="netsuite-help-docs",
        page_size=1,
    )

    assert result["ok"] is True
    assert result["indexed_count"] == 2
    # 根 index + suitecloud-platform index + suitescript-2-x-api-reference index = 3 pages
    assert result["page_count"] == 3
    assert "wiki/sources/references/netsuite-help-docs/_entries.md" in result["written"]

    query = wiki_query(root, "record.submitFields", top_k=3, filter_type="source_index")
    paths = [item["path"] for item in query["results"]]
    assert any(path.startswith("wiki/sources/references/netsuite-help-docs/") for path in paths)
    content_list = "\n".join(item["content"] for item in query["context"])
    content = content_list or "\n".join(str(item.get("frontmatter", "")) for item in query["results"])
    assert "raw/sources/references/docs/SuiteScript/N_record Module.md" in content
    assert "record.submitFields(options)" in content
    assert first_url in content
```

b) `_raw_page` helper 增加 `tags=None` 参数（默认值对应原硬编码 `["NetSuite"]`），将其改为可覆盖：

```python
def _raw_page(root: Path, relative: str, title: str, url: str, body: str, tags: list[str] | None = None) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if tags is None:
        tags = ["NetSuite"]
    tag_lines = "\n".join(f'  - "{tag}"' for tag in tags)
    path.write_text(
        "\n".join([
            "---",
            f'title: "{title}"',
            f'source: "{url}"',
            'published: "2026-06-19"',
            "tags:",
            tag_lines,
            "---",
            "",
            body,
            "",
        ]),
        encoding="utf-8",
    )
```

c) `test_build_source_index_refuses_to_overwrite_manual_pages` 无需改动（仍使用默认 `tags=["NetSuite"]`，断言不变）。

d) 在文件末尾追加：

```python
def test_build_source_index_mirrors_one_file_across_multiple_tag_branches(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source_root = root / "raw/sources/references/docs"
    _raw_page(
        root,
        "raw/sources/references/docs/mirror.md",
        "Mirror Doc",
        "https://example.com/mirror.html",
        "## Mirror\n",
        tags=[
            "suitecloud-platform/suitescript",
            "suitescript/suitescript-2-x-api-reference",
            "n-action-module/action-action",
        ],
    )
    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="netsuite-help-docs",
    )
    assert result["ok"] is True
    # 落点 index：根 + suitecloud-platform/ + suitescript/ + n-action-module/
    assert any(p.endswith("suitecloud-platform/_entries.md") for p in result["written"])
    assert any(p.endswith("suitescript/_entries.md") for p in result["written"])
    assert any(p.endswith("n-action-module/_entries.md") for p in result["written"])
    for path_rel in result["written"]:
        if not path_rel.endswith("_entries.md"):
            continue
        text = (root / path_rel).read_text(encoding="utf-8")
        if "mirror.md" in text:
            # 三份 index 应各出现一次该 raw 条目
            assert text.count("raw/sources/references/docs/mirror.md") == 1


def test_build_source_index_root_doc_when_tag_starts_with_source_name(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source_root = root / "raw/sources/references/docs"
    _raw_page(
        root,
        "raw/sources/references/docs/root.md",
        "SuiteCloud Root",
        "https://example.com/root.html",
        "## SuiteCloud\n",
        tags=["netsuite-help-docs/suitecloud-platform"],
    )
    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="netsuite-help-docs",
    )
    assert result["ok"] is True
    root_index_text = (root / "wiki/sources/references/netsuite-help-docs/_entries.md").read_text(encoding="utf-8")
    assert "## suitecloud-platform" in root_index_text
    assert "https://example.com/root.html" in root_index_text
    # suitecloud-platform 是叶子（无其它文件 tag 以它为父前缀），不建独立目录
    assert not (root / "wiki/sources/references/netsuite-help-docs/suitecloud-platform").exists()


def test_build_source_index_pure_leaf_no_directory(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _raw_page(
        root,
        "raw/sources/references/docs/leaf.md",
        "Deep Leaf Doc",
        "https://example.com/leaf.html",
        "## Deep\n",
        tags=["a/b/c"],
    )
    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="netsuite-help-docs",
    )
    assert result["ok"] is True
    expected = "wiki/sources/references/netsuite-help-docs/a/b/_entries.md"
    assert expected in result["written"]
    assert not (root / "wiki/sources/references/netsuite-help-docs/a/b/c").exists()


def test_build_source_index_ungrouped_when_no_tags(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _raw_page(
        root,
        "raw/sources/references/docs/untagged.md",
        "Untagged Doc",
        "https://example.com/untagged.html",
        "## Untagged\n",
        tags=None,
    )
    # _raw_page 默认 tags=["NetSuite"]，所以为了构造"无 tag"需手动覆盖 frontmatter
    raw_path = root / "raw/sources/references/docs/untagged.md"
    raw_path.write_text(
        "\n".join([
            "---",
            'title: "Untagged Doc"',
            'source: "https://example.com/untagged.html"',
            'published: "2026-06-19"',
            "---",
            "",
            "## Untagged",
            "",
        ]),
        encoding="utf-8",
    )
    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="netsuite-help-docs",
    )
    assert result["ok"] is True
    assert "wiki/sources/references/netsuite-help-docs/_ungrouped/_entries.md" in result["written"]
    text = (root / "wiki/sources/references/netsuite-help-docs/_ungrouped/_entries.md").read_text(encoding="utf-8")
    assert "raw/sources/references/docs/untagged.md" in text
```

- [ ] **Step 2: 运行测试，多数应当 FAIL（主流程尚未改）**

Run: `pytest tests/test_wiki_source_index.py -v`
Expected: 已改两测 + 新增 4 个测全部 FAIL（assertion error 或 page_count 不对、文件不存在）。

- [ ] **Step 3: 改写 `build_source_index` 主流程**

将现有 `build_source_index` 函数体中"清理 → 分组写页 → catalog"一段替换为新流程，并删除 `_group_entries`、`_write_group_pages`、`_write_catalog_page`、`_pages_by_group`、`_group_body`。具体替换为（保留 build_source_index 的开头解析/校验部分不变）：

```python
def build_source_index(
    vault_root: str | Path,
    source_root: str | Path,
    source_name: str,
    target_dir: str | Path | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_headings: int = DEFAULT_MAX_HEADINGS,
    refresh: bool = True,
) -> dict[str, Any]:
    """Build queryable source_index pages for raw markdown without LLM analysis."""
    root = Path(vault_root).expanduser().resolve()
    if not source_name.strip():
        return {"ok": False, "code": "missing_source_name", "error": "source_name is required"}

    resolved_source = _resolve_source_root(root, source_root)
    if not resolved_source.get("ok"):
        return resolved_source
    source_dir: Path = resolved_source["absolute_path"]
    source_rel: str = resolved_source["relative_path"]

    resolved_target = _resolve_target_dir(root, target_dir, source_name)
    if not resolved_target.get("ok"):
        return resolved_target
    target: Path = resolved_target["absolute_path"]
    target_rel: str = resolved_target["relative_path"]

    page_size = _clamp(page_size, MIN_PAGE_SIZE, MAX_PAGE_SIZE)
    max_headings = _clamp(max_headings, 0, 50)
    toc = _load_toc_manifest(source_dir)
    entries = [_entry_for_file(root, source_dir, path, toc, max_headings) for path in sorted(source_dir.rglob("*.md"))]
    entries = [entry for entry in entries if entry is not None]
    if not entries:
        return {"ok": False, "code": "no_markdown_sources", "error": f"no markdown files found under {source_rel}"}

    clear_result = _clear_existing_generated_pages(root, target)
    if not clear_result.get("ok"):
        return clear_result

    section_entries, interior_nodes = _build_tag_index_tree(entries, source_name)

    # 确定需要写 index 的节点集合：所有有章节条目落入的父路径并上所有 interior 节点。
    node_paths: set[tuple[str, ...]] = {parent for parent, _ in section_entries.keys()}
    node_paths |= interior_nodes
    # 根 () 始终写一份根 index
    node_paths.add(())

    written: list[str] = []
    group_summary: list[dict[str, Any]] = []
    for node_path in sorted(node_paths, key=lambda p: (len(p), [seg.casefold() for seg in p])):
        page_written = _write_node_index(
            root, target, target_rel, source_name, source_rel,
            node_path=node_path,
            section_entries=section_entries,
            interior_nodes=interior_nodes,
            page_size=page_size,
        )
        written.extend(page_written)
        total_at_node = sum(
            len(section_entries.get((node_path, leaf), []))
            for parent, leaf in section_entries.keys()
            if parent == node_path
        )
        group_summary.append({
            "tag_path": "/".join(node_path),
            "count": total_at_node,
        })

    index_result: dict[str, Any] | None = None
    overview_result: dict[str, Any] | None = None
    if refresh:
        index_result = refresh_indexes(root)
        if index_result.get("ok"):
            overview_result = refresh_overview(root)
        else:
            return index_result

    log_result = append_log_entry(
        root,
        WikiLogEntry(
            operation="source_index",
            title=f"{source_name} lightweight source index",
            project="",
            status="ok",
            paths=written,
            sources=_source_manifest_paths(source_dir, source_rel),
        ),
    )

    return {
        "ok": True,
        "source_name": source_name,
        "source_root": source_rel,
        "target_dir": target_rel,
        "indexed_count": len(entries),
        "page_count": len(written),
        "page_size": page_size,
        "groups": group_summary,
        "written": written,
        "cleared": clear_result.get("deleted", []),
        "index_result": index_result or {},
        "overview_result": overview_result or {},
        "log_result": log_result,
    }
```

并删除以下五个旧函数（保留 `_entry_lines`、`_clear_existing_generated_pages`、其它辅助函数不动）：

- `_group_entries`
- `_write_group_pages`
- `_write_catalog_page`
- `_pages_by_group`
- `_group_body`

- [ ] **Step 4: 运行全部 source_index 测试确认 PASS**

Run: `pytest tests/test_wiki_source_index.py -v`
Expected: PASS（全部 8 个用例）。

- [ ] **Step 5: Commit**

```bash
git add src/netsuite_llm_wiki_mcp/wiki_source_index.py tests/test_wiki_source_index.py
git commit -m "feat(source_index): rewrite build pipeline to nested tag index tree"
```

---

## Task 4: 同步 `server.py` docstring 与回归整套测试

**Files:**
- Modify: `src/netsuite_llm_wiki_mcp/server.py`（仅工具 docstring 提及 catalog 名处）
- Test: 全套自动化回归 `pytest`

**Interfaces:** 不变；确认 `server.py` 与 `wiki_source_index.py` 接口对称。

- [ ] **Step 1: 定位并更新 server.py 描述**

搜索 `wiki_build_source_index` 工具内 docstring，如果有提及 `catalog.md`、`catalog`，改为：

```python
"""Build lightweight source_index pages for a raw source tree without LLM analysis.

Source documents are grouped by their frontmatter ``tags`` (treated as
``parent/leaf`` tree paths); each interior tag-path node receives a nested
``_entries.md`` (paginated as ``_entries-02.md`` ...), and pure leaves appear only
as ``## {leaf}`` sections inside the parent index. The source_name acts as
the implicit root and gets ``{target_dir}/_entries.md``.
"""
```

- [ ] **Step 2: 运行整套测试**

Run: `pytest tests/ -v`
Expected: PASS（全部已有测试无回归；新加测试全过；`test_readme_global_mcp_docs.py` 若引用工具说明请同步通过——若失败按其提示再次同步 README）。

- [ ] **Step 3: Commit**

```bash
git add src/netsuite_llm_wiki_mcp/server.py
git commit -m "docs(server): update wiki_build_source_index description for nested tag index"
```

---

## 把转换前后样本过一遍（spec coverage 自检）

对应 spec 的章节覆盖：

- §3.1 入口与校验 → Task 3 build_source_index 开头保留。
- §3.2 Entry 收集 → 不动 `_entry_for_file`；Task 3 复用。
- §3.3 tag 规范化与剥离 → Task 1 `_resolve_tag_path`。
- §3.4 构建索引树 → Task 1 `_build_tag_index_tree`，包含 source_name prefix 剥离、`_ungrouped` 路径、`## leaf` 章节归属、按 raw_path 去重、节点 interior/leaf 判定。
- §3.5 输出布局 → Task 2/3 `_write_node_index`。
- §3.6 _entries.md 内容结构 → Task 2 `_node_index_body`、`_frontmatter` 含 `tag_path`。
- §3.7 分页规则 → Task 2 `_write_node_index` 多页 `_entries-NN.md` + Task 2 测 `test_write_node_index_paginates_with_index_suffix`。
- §3.8 根 index → Task 2/3 走 `_write_node_index(node_path=())`，统一模板；不再单列 catalog。
- §3.9 清理与覆盖保护 → 不动 `_clear_existing_generated_pages`；测试 `test_build_source_index_refuses_to_overwrite_manual_pages` 保留通过。
- §3.10 写入流程 → Task 3 主流程。
- §3.11 排序 → Task 1/2 用 `_child_sort_key` + `_toc_sort_key`。
- §4 真实数据样例 → Task 1 测 `test_build_tag_index_tree_links_chain_through_interior_nodes` 复刻 action.Action 的 5 tag 链。
- §5 兼容性与回归 → Task 3 修改两测 + Task 4 全套 `pytest`。
- §6 测试计划 → 7 个用例 + 单元测试全在本计划覆盖。
- §7 范围外（YAGNI）→ 无任何对应实现；未引入 `group_by` 参数、未改 `wiki_query`。
- §8 实施要点 → 与本计划文件结构对照一致。

无漏项、无 TBD/TODO、签名一致。
from __future__ import annotations

import json
from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_query import wiki_query
from netsuite_llm_wiki_mcp.wiki_source_index import build_source_index
from netsuite_llm_wiki_mcp.wiki_source_index import (
    _resolve_tag_path,
    _entry_tag_paths,
    _build_tag_index_tree,
)


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
        page_size=2,
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


def test_build_source_index_refuses_to_overwrite_manual_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source_root = root / "raw/sources/references/docs"
    _raw_page(
        root,
        "raw/sources/references/docs/page.md",
        "Page",
        "https://example.com/page.html",
        "## Heading\n",
    )
    target = root / "wiki/sources/references/docs/manual.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ngenerated: false\n---\n\n# Manual\n", encoding="utf-8")

    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="docs",
        target_dir="wiki/sources/references/docs",
    )

    assert result["ok"] is False
    assert result["code"] == "manual_page_exists"
    assert target.exists()


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
    assert ((("suitecloud-platform",), "suitescript"), entries[0]) in [
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


def test_write_node_index_creates_nested_directory_and_entries_md(tmp_path: Path):
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


def test_write_node_index_paginates_with_entries_suffix(tmp_path: Path):
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
    assert "[[wiki/sources/references/x/parent/child/_entries|child/_entries]]" in text


def test_write_node_index_paginates_interior_child_navigation(tmp_path: Path):
    from netsuite_llm_wiki_mcp.wiki_source_index import _write_node_index

    root = tmp_path / "vault"
    create_wiki_root(root)
    target_rel = "wiki/sources/references/x"
    target = root / target_rel
    target.mkdir(parents=True)
    interior_nodes = {()}
    interior_nodes.update((f"child-{index}",) for index in range(5))

    written = _write_node_index(
        root,
        target,
        target_rel,
        "x",
        "raw/sources/references/x",
        node_path=(),
        section_entries={},
        interior_nodes=interior_nodes,
        page_size=2,
    )

    assert written == [
        "wiki/sources/references/x/_entries.md",
        "wiki/sources/references/x/_entries-02.md",
        "wiki/sources/references/x/_entries-03.md",
    ]
    pages = [(root / path).read_text(encoding="utf-8") for path in written]
    combined = "\n".join(pages)
    assert [page.count("- → [[") for page in pages] == [2, 2, 1]
    for index in range(5):
        assert combined.count(f"|child-{index}/_entries]]") == 1


def test_write_node_index_paginates_mixed_entries_and_navigation(tmp_path: Path):
    from netsuite_llm_wiki_mcp.wiki_source_index import _write_node_index

    root = tmp_path / "vault"
    create_wiki_root(root)
    target_rel = "wiki/sources/references/x"
    target = root / target_rel
    target.mkdir(parents=True)
    entries = [
        {
            "raw_path": f"raw/sources/references/x/f{index}.md",
            "title": f"Title {index}",
            "source": f"https://example.com/f{index}",
            "toc_path": [],
            "type": "",
            "depth": None,
            "published": "",
            "headings": [],
            "tags": ["child/leaf"],
            "hash": f"{index:064d}",
            "keywords": [],
        }
        for index in range(3)
    ]

    written = _write_node_index(
        root,
        target,
        target_rel,
        "x",
        "raw/sources/references/x",
        node_path=(),
        section_entries={((), "child"): entries},
        interior_nodes={(), ("child",)},
        page_size=2,
    )

    assert written == [
        "wiki/sources/references/x/_entries.md",
        "wiki/sources/references/x/_entries-02.md",
    ]
    combined = "\n".join(
        (root / path).read_text(encoding="utf-8") for path in written
    )
    assert combined.count("|child/_entries]]") == 1
    for entry in entries:
        assert combined.count(entry["raw_path"]) == 1


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


def test_build_source_index_same_file_mirrored_into_sibling_children_with_small_page_size(tmp_path: Path):
    """Fix for final-review Important finding: a single file mirrored into TWO
    sibling children of the SAME parent node must NOT lose one child's section
    when page_size splits chunks across section boundaries. The old
    `chunk_entries[cursor] in child_entries` allocation silently swallowed the
    second sibling's section because the same entry dict is `==` to its own copy.
    """
    root = tmp_path / "vault"
    create_wiki_root(root)
    _raw_page(
        root,
        "raw/sources/references/docs/mirror-sibling.md",
        "Mirror Sibling Doc",
        "https://example.com/mirror-sibling.html",
        "## Mirror Sibling\n",
        tags=["parent/child-a", "parent/child-b"],
    )
    # page_size=1 forces the section boundary across chunks
    result = build_source_index(
        root,
        source_root="raw/sources/references/docs",
        source_name="netsuite-help-docs",
        page_size=1,
    )
    assert result["ok"] is True
    # parent node should get an _entries.md (and possibly pagination pages)
    parent_paths = [p for p in result["written"] if "parent/_entries" in p]
    assert parent_paths, "expected at least one parent/_entries page"
    combined = "\n".join(
        (root / p).read_text(encoding="utf-8") for p in parent_paths
    )
    assert "## child-a" in combined
    assert "## child-b" in combined
    # mirror entry must appear once per child section across the node pages
    assert combined.count("raw/sources/references/docs/mirror-sibling.md") == 2

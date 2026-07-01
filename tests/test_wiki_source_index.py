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


def _raw_page(root: Path, relative: str, title: str, url: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            "---",
            f'title: "{title}"',
            f'source: "{url}"',
            'published: "2026-06-19"',
            "tags:",
            '  - "NetSuite"',
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
    )
    _raw_page(
        root,
        "raw/sources/references/docs/SuiteScript/N_search Module.md",
        "N/search Module",
        second_url,
        "## search.create(options)\n\nCreate searches.\n",
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
    assert result["page_count"] == 3
    assert "wiki/sources/references/netsuite-help-docs/catalog.md" in result["written"]

    query = wiki_query(root, "record.submitFields", top_k=3, filter_type="source_index")
    paths = [item["path"] for item in query["results"]]
    assert any(path.startswith("wiki/sources/references/netsuite-help-docs/") for path in paths)
    content = "\n".join(item["content"] for item in query["context"])
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

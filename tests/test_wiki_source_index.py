from __future__ import annotations

import json
from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_query import wiki_query
from netsuite_llm_wiki_mcp.wiki_source_index import build_source_index


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

from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_rag_mcp.wiki_enrich import wiki_enrich


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "wiki").mkdir(parents=True)
    index = root / "wiki" / "index.md"
    index.write_text(
        "---\ntype: index\ngenerated: true\n---\n\n# Index\n\n"
        "## Concepts\n- [[concepts/suiteql|SuiteQL]]\n- [[concepts/restlet|RESTlet]]\n",
        encoding="utf-8",
    )
    page = root / "wiki" / "projects" / "myproj" / "code" / "entry-point.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: code\ntitle: Entry Point\ngenerated: true\n---\n\n# Entry Point\n\n"
        "This script uses SuiteQL to query records. It also calls the RESTlet endpoint.\n",
        encoding="utf-8",
    )
    return root


def test_prepare_returns_prompt(wiki_root: Path):
    result = wiki_enrich(str(wiki_root), "wiki/projects/myproj/code/entry-point.md", stage="prepare")
    assert result["ok"] is True
    assert result["stage"] == "prepare"
    assert "SuiteQL" in result["prompt"]
    assert "RESTlet" in result["prompt"]


def test_apply_inserts_links(wiki_root: Path):
    links = [
        {"term": "SuiteQL", "target": "concepts/suiteql"},
        {"term": "RESTlet", "target": "concepts/restlet"},
    ]
    result = wiki_enrich(str(wiki_root), "wiki/projects/myproj/code/entry-point.md", stage="apply", links=links)
    assert result["ok"] is True
    assert result["links_applied"] == 2

    content = (wiki_root / "wiki" / "projects" / "myproj" / "code" / "entry-point.md").read_text(encoding="utf-8")
    assert "[[concepts/suiteql|SuiteQL]]" in content
    assert "[[concepts/restlet|RESTlet]]" in content


def test_apply_skips_already_linked(wiki_root: Path):
    page = wiki_root / "wiki" / "projects" / "myproj" / "code" / "entry-point.md"
    page.write_text(
        "---\ntype: code\ntitle: Entry Point\ngenerated: true\n---\n\n# Entry Point\n\n"
        "This uses [[concepts/suiteql|SuiteQL]] already.\n",
        encoding="utf-8",
    )
    links = [{"term": "SuiteQL", "target": "concepts/suiteql"}]
    result = wiki_enrich(str(wiki_root), "wiki/projects/myproj/code/entry-point.md", stage="apply", links=links)
    assert result["ok"] is True
    assert result["links_applied"] == 0


def test_apply_json_string(wiki_root: Path):
    links_json = '{"links": [{"term": "SuiteQL", "target": "concepts/suiteql"}]}'
    result = wiki_enrich(str(wiki_root), "wiki/projects/myproj/code/entry-point.md", stage="apply", links=links_json)
    assert result["ok"] is True
    assert result["links_applied"] == 1


def test_page_not_found(wiki_root: Path):
    result = wiki_enrich(str(wiki_root), "wiki/nonexistent.md", stage="prepare")
    assert result["ok"] is False
    assert result["code"] == "page_not_found"

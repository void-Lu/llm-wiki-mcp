from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.wiki_index import refresh_indexes
from netsuite_rag_mcp.wiki_io import write_wiki_page
from netsuite_rag_mcp.wiki_models import WikiPage
from netsuite_rag_mcp.wiki_paths import create_wiki_root


def _page(path: str, title: str, summary: str = "") -> WikiPage:
    return WikiPage(
        relative_path=Path(path),
        frontmatter={"title": title, "summary": summary, "generated": True, "sources": []},
        title=title,
        body=summary or title,
    )


def test_refresh_indexes_groups_top_level_wiki_categories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(root, _page("wiki/projects/alpha/code/script.md", "Script", "code summary"))
    write_wiki_page(root, _page("wiki/concepts/suitescript.md", "SuiteScript", "concept summary"))
    write_wiki_page(root, _page("wiki/sources/source-a.md", "Source A", "source summary"))
    write_wiki_page(root, _page("wiki/queries/query-a.md", "Query A", "query summary"))
    write_wiki_page(root, _page("wiki/synthesis/synth-a.md", "Synthesis A", "synthesis summary"))
    write_wiki_page(root, _page("wiki/comparisons/compare-a.md", "Compare A", "comparison summary"))

    result = refresh_indexes(root)

    assert result["ok"] is True
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    for heading in ["## Projects", "## Concepts", "## Sources", "## Queries", "## Synthesis", "## Comparisons"]:
        assert heading in index
    assert "[[projects/alpha/index.md|alpha]]" in index
    assert "[[concepts/suitescript.md|SuiteScript]] — concept summary" in index
    assert "[[sources/source-a.md|Source A]] — source summary" in index


def test_refresh_indexes_creates_project_index_grouped_by_subdirectories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(root, _page("wiki/projects/alpha/code/script.md", "Script", "code summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/decisions/decision.md", "Decision", "decision summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/troubleshooting/issue.md", "Issue", "issue summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/requirements/spec.md", "Spec", "spec summary"))

    refresh_indexes(root)

    project_index = (root / "wiki/projects/alpha/index.md").read_text(encoding="utf-8")
    for heading in ["## Code", "## Decisions", "## Troubleshooting", "## Requirements"]:
        assert heading in project_index
    assert "[[code/script.md|Script]] — code summary" in project_index
    assert "[[decisions/decision.md|Decision]] — decision summary" in project_index
    assert "[[troubleshooting/issue.md|Issue]] — issue summary" in project_index
    assert "[[requirements/spec.md|Spec]] — spec summary" in project_index


def test_refresh_indexes_refuses_to_overwrite_manual_top_index(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/index.md").write_text("---\ngenerated: false\n---\n\n# Manual Index\n", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is False
    assert result["code"] == "manual_page_exists"
    assert "Manual Index" in (root / "wiki/index.md").read_text(encoding="utf-8")


def test_refresh_indexes_does_not_crash_on_malformed_frontmatter_page(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/concepts/bad.md"
    target.write_text("---\ntitle: [broken\n---\n\n# Bad\n\nsummary", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is True
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    assert "[[concepts/bad.md|Bad]]" in index

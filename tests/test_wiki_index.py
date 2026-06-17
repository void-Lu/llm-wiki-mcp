from __future__ import annotations

from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


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
    write_wiki_page(root, _page("wiki/projects/alpha/specs/spec.md", "Spec", "spec summary"))
    write_wiki_page(root, _page("wiki/concepts/suitescript/module.md", "SuiteScript", "concept summary"))
    write_wiki_page(root, _page("wiki/chatlog/2026/06/13/session.md", "Session", "chat summary"))
    write_wiki_page(root, _page("wiki/sources/concepts/suitescript/source-a.md", "Source A", "source summary"))
    write_wiki_page(root, _page("wiki/queries/2026/06/16/query-a/research.md", "Query A", "query summary"))
    write_wiki_page(root, _page("wiki/entities/customer/customer.md", "Customer", "entity summary"))

    result = refresh_indexes(root)

    assert result["ok"] is True
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    for heading in ["## Projects", "## Concepts", "## Chatlog", "## Sources", "## Queries", "## Entities", "## Archives"]:
        assert heading in index
    assert "[[projects/alpha/index.md|alpha]]" in index
    assert "[[concepts/index.md|Concepts]]" in index
    assert "[[chatlog/index.md|Chatlog]]" in index
    assert "[[sources/index.md|Sources]]" in index
    assert "[[queries/index.md|Queries]]" in index
    assert "[[entities/index.md|Entities]]" in index
    assert "[[archives/log.md|Archives Log]]" in index
    assert "concept summary" not in index

    concepts_index = (root / "wiki/concepts/index.md").read_text(encoding="utf-8")
    assert "[[suitescript/index.md|suitescript]]" in concepts_index
    concept_domain_index = (root / "wiki/concepts/suitescript/index.md").read_text(encoding="utf-8")
    assert "[[module.md|SuiteScript]] — concept summary" in concept_domain_index
    chatlog_index = (root / "wiki/chatlog/index.md").read_text(encoding="utf-8")
    assert "[[2026/06/13/session.md|Session]] — chat summary" in chatlog_index
    assert not (root / "wiki/chatlog/2026/06/13/index.md").exists()
    entities_index = (root / "wiki/entities/index.md").read_text(encoding="utf-8")
    assert "[[customer/customer.md|Customer]] — entity summary" in entities_index
    assert not (root / "wiki/entities/customer/index.md").exists()


def test_refresh_indexes_creates_project_index_grouped_by_subdirectories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(root, _page("wiki/projects/alpha/specs/spec.md", "Spec", "spec summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/plans/plan.md", "Plan", "plan summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/architecture/arch.md", "Architecture", "architecture summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/pipelines/pipeline.md", "Pipeline", "pipeline summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/troubleshooting/issue.md", "Issue", "issue summary"))
    write_wiki_page(root, _page("wiki/projects/alpha/researches/investigation.md", "Investigation", "research summary"))

    refresh_indexes(root)

    project_index = (root / "wiki/projects/alpha/index.md").read_text(encoding="utf-8")
    for heading in ["## Specs", "## Plans", "## Architecture", "## Pipelines", "## Troubleshooting", "## Researches"]:
        assert heading in project_index
    assert "## Sources" not in project_index
    assert "[[specs/spec.md|Spec]] — spec summary" in project_index
    assert "[[plans/plan.md|Plan]] — plan summary" in project_index
    assert "[[architecture/arch.md|Architecture]] — architecture summary" in project_index
    assert "[[pipelines/pipeline.md|Pipeline]] — pipeline summary" in project_index
    assert "[[troubleshooting/issue.md|Issue]] — issue summary" in project_index
    assert "[[researches/investigation.md|Investigation]] — research summary" in project_index


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
    index = (root / "wiki/concepts/index.md").read_text(encoding="utf-8")
    assert "[[bad.md|Bad]]" in index

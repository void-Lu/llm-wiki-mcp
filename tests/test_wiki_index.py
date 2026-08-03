from __future__ import annotations

from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_query import wiki_query


def _page(path: str, title: str, summary: str = "") -> WikiPage:
    return WikiPage(
        relative_path=Path(path),
        frontmatter={"title": title, "summary": summary, "generated": True, "sources": []},
        title=title,
        body=summary or title,
    )


def test_refresh_indexes_groups_only_active_wiki_categories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(root, _page("wiki/projects/alpha/specs/spec.md", "Spec", "spec summary"))
    write_wiki_page(root, _page("wiki/concepts/suitescript/module.md", "SuiteScript", "concept summary"))
    write_wiki_page(root, _page("wiki/entities/customer/customer.md", "Customer", "entity summary"))

    result = refresh_indexes(root)

    assert result["ok"] is True
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    for heading in ["## Projects", "## Concepts", "## Entities"]:
        assert heading in index
    assert "## Sources" not in index
    assert "sources/index.md" not in index
    assert "[[projects/alpha/index.md|alpha]]" in index
    assert "[[concepts/index.md|Concepts]]" in index
    assert "[[entities/index.md|Entities]]" in index

    concepts_index = (root / "wiki/concepts/index.md").read_text(encoding="utf-8")
    assert "[[suitescript/index.md|suitescript]]" in concepts_index
    entities_index = (root / "wiki/entities/index.md").read_text(encoding="utf-8")
    assert "[[customer/customer.md|Customer]]" in entities_index


def test_refresh_indexes_creates_project_index_grouped_by_subdirectories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    for subdir, filename, title in (
        ("specs", "spec.md", "Spec"),
        ("plans", "plan.md", "Plan"),
        ("architecture", "arch.md", "Architecture"),
        ("pipelines", "pipeline.md", "Pipeline"),
        ("troubleshooting", "issue.md", "Issue"),
        ("researches", "investigation.md", "Investigation"),
    ):
        write_wiki_page(root, _page(f"wiki/projects/alpha/{subdir}/{filename}", title, f"{subdir} summary"))

    refresh_indexes(root)

    project_index = (root / "wiki/projects/alpha/index.md").read_text(encoding="utf-8")
    for heading in ["## Specs", "## Plans", "## Architecture", "## Pipelines", "## Troubleshooting", "## Researches"]:
        assert heading in project_index
    assert "## Sources" not in project_index


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
    assert "[[bad.md|Bad]]" in (root / "wiki/concepts/index.md").read_text(encoding="utf-8")


def test_refresh_indexes_never_recreates_retired_source_namespace(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    legacy = root / "wiki/sources/old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ntype: source_capsule\ngenerated: true\n---\n\n# old\n\nlegacy noise", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is True
    assert legacy.exists()
    assert not (root / "wiki/sources/index.md").exists()
    assert wiki_query(root, "legacy noise", top_k=5)["results"] == []

from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_lint import wiki_lint
from netsuite_llm_wiki_mcp.wiki_limits import HARD_PAGE_BYTES, utf8_size
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


def test_refresh_indexes_groups_top_level_wiki_categories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(root, _page("wiki/projects/alpha/specs/spec.md", "Spec", "spec summary"))
    write_wiki_page(root, _page("wiki/concepts/suitescript/module.md", "SuiteScript", "concept summary"))
    write_wiki_page(root, _page("wiki/sources/concepts/suitescript/source-a.md", "Source A", "source summary"))
    write_wiki_page(root, _page("wiki/entities/customer/customer.md", "Customer", "entity summary"))

    result = refresh_indexes(root)

    assert result["ok"] is True
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    for heading in ["## Projects", "## Concepts", "## Sources", "## Entities"]:
        assert heading in index
    assert "[[projects/alpha/index.md|alpha]]" in index
    assert "[[concepts/index.md|Concepts]]" in index
    assert "[[sources/index.md|Sources]]" in index
    assert "[[entities/index.md|Entities]]" in index
    assert "Chatlog" not in index
    assert "Queries" not in index
    assert "concept summary" not in index

    concepts_index = (root / "wiki/concepts/index.md").read_text(encoding="utf-8")
    assert "[[suitescript/index.md|suitescript]]" in concepts_index
    concept_domain_index = (root / "wiki/concepts/suitescript/index.md").read_text(encoding="utf-8")
    assert "[[module.md|SuiteScript]] — concept summary" in concept_domain_index
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


def test_refresh_sources_uses_bounded_hierarchical_navigation_without_hiding_leaves(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    bulk = root / "wiki/sources/bulk"
    bulk.mkdir(parents=True)
    for number in range(1_654):
        write_wiki_page(
            root,
            _page(
                f"wiki/sources/bulk/source-{number:04d}.md",
                f"Source {number:04d}",
                f"unique leaf token {number:04d}",
            ),
        )

    result = refresh_indexes(root)

    assert result["ok"] is True
    root_index = (root / "wiki/sources/index.md").read_text(encoding="utf-8")
    assert "[[bulk/index.md|bulk]]" in root_index
    assert root_index.count("[[bulk/index.md|bulk]]") == 1
    assert "source-0000.md" not in root_index
    navigation_pages = sorted(bulk.glob("index*.md"))
    assert len(navigation_pages) > 1
    assert all(utf8_size(path.read_text(encoding="utf-8")) <= HARD_PAGE_BYTES for path in navigation_pages)
    assert "[[index-02.md|Next]]" in (bulk / "index.md").read_text(encoding="utf-8")
    first_render = {path.name: path.read_text(encoding="utf-8") for path in navigation_pages}
    assert refresh_indexes(root)["ok"] is True
    assert {path.name: path.read_text(encoding="utf-8") for path in sorted(bulk.glob("index*.md"))} == first_render

    lint_issues = wiki_lint(root)["issues"]
    assert not any(
        issue["code"] == "oversized_page" and issue["path"].startswith("wiki/sources/")
        for issue in lint_issues
    )

    query = wiki_query(root, "unique leaf token 1653", top_k=3)
    assert "wiki/sources/bulk/source-1653.md" in [item["path"] for item in query["results"]]


def test_refresh_sources_never_overwrites_manual_index(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/sources/index.md"
    target.write_text("---\ngenerated: false\n---\n\n# Manual Sources\n", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is False
    assert result["code"] == "manual_page_exists"
    assert target.read_text(encoding="utf-8").endswith("# Manual Sources\n")


def test_refresh_sources_stages_navigation_before_replacing_existing_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(root, _page("wiki/sources/bulk/first.md", "First", "first"))
    assert refresh_indexes(root)["ok"] is True
    before = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "wiki/sources").rglob("index*.md")
    }
    raw = root / "raw/sources/unchanged.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw content must not change", encoding="utf-8")
    raw_before = raw.read_bytes()
    write_wiki_page(root, _page("wiki/sources/bulk/second.md", "Second", "second"))

    original_write_text = Path.write_text

    def fail_staged_writes(path: Path, text: str, *args: str | None, **kwargs: str | None) -> int:
        if path.suffix == ".tmp" and path.parent.is_relative_to(root / "wiki/sources"):
            raise OSError("injected navigation write failure")
        return original_write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_staged_writes)
    with pytest.raises(OSError, match="injected navigation write failure"):
        refresh_indexes(root)

    after = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "wiki/sources").rglob("index*.md")
    }
    assert after == before
    assert raw.read_bytes() == raw_before


def test_refresh_sources_rolls_back_if_a_navigation_page_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    create_wiki_root(root)
    bulk = root / "wiki/sources/bulk"
    for number in range(200):
        write_wiki_page(root, _page(f"wiki/sources/bulk/source-{number:03d}.md", f"Source {number:03d}"))
    assert refresh_indexes(root)["ok"] is True
    before = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "wiki/sources").rglob("index*.md")
    }

    write_wiki_page(root, _page("wiki/sources/bulk/source-200.md", "Source 200"))
    original_replace = Path.replace

    def fail_second_page_commit(path: Path, target: Path) -> Path:
        if path.suffix == ".tmp" and target.name == "index-02.md":
            raise OSError("injected navigation replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_page_commit)
    with pytest.raises(OSError, match="injected navigation replace failure"):
        refresh_indexes(root)

    after = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "wiki/sources").rglob("index*.md")
    }
    assert after == before
    assert not (bulk / "index-02.md").exists()

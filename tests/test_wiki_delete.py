from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_delete import wiki_delete_source


@pytest.fixture
def delete_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    # Raw source
    raw = root / "raw" / "sources" / "codegraph" / "proj" / "my-source"
    raw.mkdir(parents=True)
    (raw / "snapshot.json").write_text("{}", encoding="utf-8")

    # Wiki pages derived from this source
    wiki = root / "wiki"
    code = wiki / "projects" / "proj" / "code"
    code.mkdir(parents=True)
    (code / "derived-page.md").write_text(
        "---\ntype: code\ntitle: Derived Page\ngenerated: true\nproject: proj\nsources:\n- my-source/snapshot.json\n---\n\n# Derived Page\n\nContent.\n",
        encoding="utf-8",
    )
    # Manual page (should not be deleted)
    (code / "manual-page.md").write_text(
        "---\ntype: code\ntitle: Manual Page\nsources:\n- my-source/other.ts\n---\n\n# Manual Page\n\nManual.\n",
        encoding="utf-8",
    )
    # Source summary
    sources = wiki / "sources"
    sources.mkdir(parents=True)
    (sources / "my-source.md").write_text(
        "---\ntype: source\ntitle: My Source\ngenerated: true\n---\n\n# My Source\n\nSummary.\n",
        encoding="utf-8",
    )
    # A page that links to derived-page
    concepts = wiki / "concepts" / "domain"
    concepts.mkdir(parents=True)
    (concepts / "related.md").write_text(
        "---\ntype: concept\ntitle: Related\n---\n\n# Related\n\nSee [[derived-page]] for details.\n",
        encoding="utf-8",
    )
    # Index and log
    (wiki / "index.md").write_text("---\ntype: index\ngenerated: true\n---\n\n# Index\n", encoding="utf-8")
    (wiki / "log.md").write_text("", encoding="utf-8")
    # Ingest cache
    cache = root / ".llm-wiki" / "ingest-cache" / "proj"
    cache.mkdir(parents=True)
    (cache / "my-source.json").write_text("{}", encoding="utf-8")

    return root


def test_dry_run(delete_root: Path):
    result = wiki_delete_source(str(delete_root), "proj", "my-source", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert len(result["derived_pages"]) >= 1
    # Nothing actually deleted
    assert (delete_root / "raw" / "sources" / "codegraph" / "proj" / "my-source").exists()


def test_delete_cascades(delete_root: Path):
    result = wiki_delete_source(str(delete_root), "proj", "my-source")
    assert result["ok"] is True
    assert result["raw_deleted"] is True
    assert len(result["pages_deleted"]) >= 1

    # Raw dir gone
    assert not (delete_root / "raw" / "sources" / "codegraph" / "proj" / "my-source").exists()
    # Derived page gone
    assert not (delete_root / "wiki" / "projects" / "proj" / "code" / "derived-page.md").exists()
    # Manual page preserved
    assert (delete_root / "wiki" / "projects" / "proj" / "code" / "manual-page.md").exists()
    # Source summary gone
    assert not (delete_root / "wiki" / "sources" / "my-source.md").exists()
    # Cache cleaned
    assert not (delete_root / ".llm-wiki" / "ingest-cache" / "proj" / "my-source.json").exists()
    # Wikilink removed from related page
    related = (delete_root / "wiki" / "concepts" / "domain" / "related.md").read_text(encoding="utf-8")
    assert "[[derived-page]]" not in related
    assert "derived-page" in related  # text preserved, just not as link


def test_delete_source_preserves_generated_pages_with_other_sources(delete_root: Path):
    shared_page = delete_root / "wiki" / "projects" / "proj" / "code" / "shared-page.md"
    shared_page.write_text(
        "---\n"
        "type: code\n"
        "title: Shared Page\n"
        "generated: true\n"
        "project: proj\n"
        "sources:\n"
        "- raw/sources/codegraph/proj/my-source/context.json\n"
        "- raw/sources/codegraph/proj/other-source/context.json\n"
        "---\n\n"
        "# Shared Page\n\nContent from multiple sources.\n",
        encoding="utf-8",
    )

    result = wiki_delete_source(str(delete_root), "proj", "my-source")

    assert result["ok"] is True
    assert shared_page.exists()
    text = shared_page.read_text(encoding="utf-8")
    assert "my-source" not in text
    assert "other-source" in text
    assert shared_page.relative_to(delete_root).as_posix() not in result["pages_deleted"]

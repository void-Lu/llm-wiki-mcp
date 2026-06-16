from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_delete import wiki_delete_source


@pytest.fixture
def delete_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    # Raw source
    raw = root / "raw" / "projects" / "proj" / "codegraph" / "my-source"
    raw.mkdir(parents=True)
    (raw / "snapshot.json").write_text("{}", encoding="utf-8")

    # Wiki pages derived from this source
    wiki = root / "wiki"
    project_sources = wiki / "projects" / "proj" / "sources"
    project_sources.mkdir(parents=True)
    (project_sources / "derived-page.md").write_text(
        "---\ntype: source_index\ntitle: Derived Page\ngenerated: true\nproject: proj\nsources:\n- raw/projects/proj/codegraph/my-source/snapshot.json\n---\n\n# Derived Page\n\nContent.\n",
        encoding="utf-8",
    )
    # Manual page (should not be deleted)
    (project_sources / "manual-page.md").write_text(
        "---\ntype: source_index\ntitle: Manual Page\nsources:\n- my-source/other.ts\n---\n\n# Manual Page\n\nManual.\n",
        encoding="utf-8",
    )
    # Source summary
    (project_sources / "my-source.md").write_text(
        "---\ntype: source_index\ntitle: My Source\ngenerated: true\nproject: proj\n---\n\n# My Source\n\nSummary.\n",
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
    assert (delete_root / "raw" / "projects" / "proj" / "codegraph" / "my-source").exists()


def test_delete_cascades(delete_root: Path):
    result = wiki_delete_source(str(delete_root), "proj", "my-source")
    assert result["ok"] is True
    assert result["raw_deleted"] is True
    assert len(result["pages_deleted"]) >= 1

    # Raw dir gone
    assert not (delete_root / "raw" / "projects" / "proj" / "codegraph" / "my-source").exists()
    # Derived page gone
    assert not (delete_root / "wiki" / "projects" / "proj" / "sources" / "derived-page.md").exists()
    # Manual page preserved
    assert (delete_root / "wiki" / "projects" / "proj" / "sources" / "manual-page.md").exists()
    # Source summary gone
    assert not (delete_root / "wiki" / "projects" / "proj" / "sources" / "my-source.md").exists()
    # Cache cleaned
    assert not (delete_root / ".llm-wiki" / "ingest-cache" / "proj" / "my-source.json").exists()
    # Wikilink removed from related page
    related = (delete_root / "wiki" / "concepts" / "domain" / "related.md").read_text(encoding="utf-8")
    assert "[[derived-page]]" not in related
    assert "derived-page" in related  # text preserved, just not as link


def test_delete_source_preserves_generated_pages_with_other_sources(delete_root: Path):
    shared_page = delete_root / "wiki" / "projects" / "proj" / "sources" / "shared-page.md"
    shared_page.write_text(
        "---\n"
        "type: source_index\n"
        "title: Shared Page\n"
        "generated: true\n"
        "project: proj\n"
        "sources:\n"
        "- raw/projects/proj/codegraph/my-source/context.json\n"
        "- raw/projects/proj/codegraph/other-source/context.json\n"
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


def test_delete_source_removes_date_grouped_chat_raw_dir(tmp_path: Path):
    root = tmp_path / "vault"
    raw = root / "raw" / "sources" / "chat" / "2026" / "06" / "16" / "session-2026-06-16"
    raw.mkdir(parents=True)
    (raw / "session.md").write_text("chat", encoding="utf-8")
    wiki = root / "wiki"
    (wiki / "chatlog" / "2026" / "06" / "16").mkdir(parents=True)
    (wiki / "chatlog" / "2026" / "06" / "16" / "session.md").write_text(
        "---\n"
        "type: chatlog\n"
        "title: Session\n"
        "generated: true\n"
        "project: general\n"
        "sources:\n"
        "- raw/sources/chat/2026/06/16/session-2026-06-16/session.md\n"
        "---\n\n"
        "# Session\n\nBody.\n",
        encoding="utf-8",
    )
    (wiki / "sources" / "chatlog" / "2026" / "06" / "16").mkdir(parents=True)
    (wiki / "sources" / "chatlog" / "2026" / "06" / "16" / "session-2026-06-16.md").write_text(
        "---\n"
        "type: source_index\n"
        "title: Session Source\n"
        "generated: true\n"
        "project: general\n"
        "source_name: session-2026-06-16\n"
        "sources:\n"
        "- raw/sources/chat/2026/06/16/session-2026-06-16/session.md\n"
        "---\n\n"
        "# Session Source\n\n- [[session]]\n",
        encoding="utf-8",
    )
    (wiki / "index.md").write_text("---\ntype: index\ngenerated: true\n---\n\n# Index\n", encoding="utf-8")
    (wiki / "log.md").write_text("", encoding="utf-8")

    result = wiki_delete_source(root, "general", "session-2026-06-16")

    assert result["ok"] is True
    assert result["raw_deleted"] is True
    assert not raw.exists()

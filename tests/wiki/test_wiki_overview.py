from __future__ import annotations

from pathlib import Path

from wiki.wiki_io import write_wiki_page
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry, WikiPage
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import create_wiki_root


def test_refresh_overview_writes_deterministic_counts_and_recent_log(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/projects/alpha/architecture/script.md"),
            frontmatter={"title": "Script", "generated": True, "sources": ["raw/sources/a.md"]},
            title="Script",
            body="code",
        ),
    )
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/projects/alpha/specs/spec.md"),
            frontmatter={"title": "Spec", "generated": False},
            title="Spec",
            body="manual",
        ),
        overwrite_generated_only=False,
    )
    append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="Alpha",
            paths=["wiki/projects/alpha/architecture/script.md"],
            sources=["raw/sources/a.md"],
            project="alpha",
            status="ok",
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    result = refresh_overview(root)

    assert result["ok"] is True
    overview = (root / "wiki/overview.md").read_text(encoding="utf-8")
    assert "- Projects: 1" in overview
    assert "- Generated pages: 1" in overview
    assert "- Manual pages: 1" in overview
    assert "## [2026-05-26T10:20:30Z] ingest | Alpha" in overview


def test_refresh_overview_refuses_to_overwrite_manual_overview(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/overview.md").write_text("---\ngenerated: false\n---\n\n# Manual Overview\n", encoding="utf-8")

    result = refresh_overview(root)

    assert result["ok"] is False
    assert result["code"] == "manual_page_exists"
    assert "Manual Overview" in (root / "wiki/overview.md").read_text(encoding="utf-8")


def test_refresh_overview_does_not_crash_on_malformed_frontmatter_page(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/concepts/bad.md"
    target.write_text("---\ntitle: [broken\n---\n\n# Bad\n\nbody", encoding="utf-8")

    result = refresh_overview(root)

    assert result["ok"] is True
    overview = (root / "wiki/overview.md").read_text(encoding="utf-8")
    assert "- Manual pages: 1" in overview

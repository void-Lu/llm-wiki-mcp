from __future__ import annotations

from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry, WikiPage
from netsuite_llm_wiki_mcp.wiki_overview import refresh_overview
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


def test_refresh_overview_writes_deterministic_counts_and_recent_log(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/projects/alpha/code/script.md"),
            frontmatter={"title": "Script", "generated": True, "sources": ["raw/sources/a.md"]},
            title="Script",
            body="code",
        ),
    )
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/projects/alpha/decisions/decision.md"),
            frontmatter={"title": "Decision", "generated": False},
            title="Decision",
            body="manual",
        ),
        overwrite_generated_only=False,
    )
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/sources/source-a.md"),
            frontmatter={"title": "Source A", "generated": True, "sources": ["raw/sources/a.md"]},
            title="Source A",
            body="source",
        ),
    )
    append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="Alpha",
            paths=["wiki/projects/alpha/code/script.md"],
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
    assert "- Source pages: 1" in overview
    assert "- Generated pages: 2" in overview
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

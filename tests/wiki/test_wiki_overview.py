from __future__ import annotations

from pathlib import Path

import pytest

import wiki.wiki_overview as wiki_overview
from tests.helpers import write_test_page
from wiki.atomic_file import AtomicFileError, fault_context
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import create_wiki_root


def test_refresh_overview_writes_deterministic_counts_and_recent_log(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/projects/alpha/architecture/script.md",
        {"title": "Script", "generated": True, "sources": ["raw/sources/a.md"]},
        "code",
    )
    write_test_page(
        root,
        "wiki/projects/alpha/specs/spec.md",
        {"title": "Spec", "generated": False},
        "manual",
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


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_refresh_overview_atomic_fault_keeps_existing_overview(stage: str, tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/overview.md"
    target.write_text("---\ntype: overview\ngenerated: true\n---\n\nold overview\n", encoding="utf-8")

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            wiki_overview.refresh_overview(root)

    assert target.read_text(encoding="utf-8") == "---\ntype: overview\ngenerated: true\n---\n\nold overview\n"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_refresh_overview_post_replace_fault_keeps_complete_new_overview(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/overview.md"
    target.write_text("---\ntype: overview\ngenerated: true\n---\n\nold overview\n", encoding="utf-8")

    def fault(stage: str) -> None:
        if stage == "post_replace":
            raise RuntimeError("injected")

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            wiki_overview.refresh_overview(root)

    rendered = target.read_text(encoding="utf-8")
    assert rendered != "---\ntype: overview\ngenerated: true\n---\n\nold overview\n"
    assert "# Overview" in rendered
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []

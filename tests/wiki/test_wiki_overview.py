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


def test_incremental_overview_create_update_delete_matches_full_and_reads_only_changed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    target_path = "wiki/projects/alpha/specs/target.md"
    manual_path = "wiki/projects/alpha/specs/manual.md"
    _write_pages = [
        (target_path, {"title": "Target", "generated": True}),
        (manual_path, {"title": "Manual", "generated": False}),
    ]
    for index in range(15):
        _write_pages.append(
            (
                f"wiki/projects/beta/specs/unrelated-{index}.md",
                {"title": f"Unrelated {index}", "generated": index % 2 == 0},
            )
        )
    for path, frontmatter in _write_pages:
        write_test_page(root, path, frontmatter, "body")
    refresh_overview(root)

    target = root / target_path
    target.write_text(target.read_text(encoding="utf-8").replace("# Target", "# Target Updated"), encoding="utf-8")
    read_paths: list[str] = []
    write_paths: list[str] = []
    original_read_frontmatter = wiki_overview._read_frontmatter
    original_atomic_write = wiki_overview.atomic_write_text

    def counted_read(path: Path) -> dict[str, object]:
        comparable = Path(str(path).removeprefix("\\\\?\\"))
        read_paths.append(comparable.relative_to(root).as_posix())
        return original_read_frontmatter(path)

    def counted_write(target: Path, text: str, **kwargs: object) -> object:
        comparable = Path(str(target).removeprefix("\\\\?\\"))
        write_paths.append(comparable.relative_to(root).as_posix())
        return original_atomic_write(target, text, **kwargs)

    monkeypatch.setattr(wiki_overview, "_read_frontmatter", counted_read)
    monkeypatch.setattr(wiki_overview, "atomic_write_text", counted_write)
    updated = refresh_overview(root, changed_path=target_path)
    assert updated["ok"] is True
    assert read_paths == [target_path]
    assert write_paths == ["wiki/overview.md"]
    incremental_update = (root / "wiki/overview.md").read_bytes()
    refresh_overview(root)
    assert (root / "wiki/overview.md").read_bytes() == incremental_update

    new_path = "wiki/projects/alpha/specs/new.md"
    write_test_page(root, new_path, {"title": "New", "generated": True}, "new")
    created = refresh_overview(root, changed_path=new_path, changed_page_state="created")
    assert created["ok"] is True
    incremental_create = (root / "wiki/overview.md").read_bytes()
    refresh_overview(root)
    assert (root / "wiki/overview.md").read_bytes() == incremental_create

    (root / manual_path).unlink()
    deleted = refresh_overview(root, changed_path=manual_path, changed_page_state="deleted", previous_generated=False)
    assert deleted["ok"] is True
    incremental_delete = (root / "wiki/overview.md").read_bytes()
    refresh_overview(root)
    assert (root / "wiki/overview.md").read_bytes() == incremental_delete


def test_incremental_overview_fails_on_missing_structure_instead_of_scanning_full_vault(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    page_path = "wiki/concepts/invoice.md"
    write_test_page(root, page_path, {"title": "Invoice", "generated": True}, "body")
    refresh_overview(root)
    (root / "wiki/overview.md").unlink()

    result = refresh_overview(root, changed_path=page_path)

    assert result == {
        "ok": False,
        "code": "incremental_overview_structure_missing",
        "path": "wiki/overview.md",
        "error": "overview structure is missing; run 'uv run llm-wiki-mcp repair page-operation apply --vault <vault> --operation-id <operation-id>'",
    }


def test_incremental_overview_bootstraps_a_bare_vault_without_a_full_scan(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = root / "wiki/concepts/invoice.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: Invoice\ngenerated: true\n---\n\n# Invoice\n", encoding="utf-8")

    result = refresh_overview(root, changed_path="wiki/concepts/invoice.md", changed_page_state="created")

    assert result["ok"] is True
    overview = (root / "wiki/overview.md").read_text(encoding="utf-8")
    assert "- Projects: 0" in overview
    assert "- Generated pages: 1" in overview

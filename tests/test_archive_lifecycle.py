from __future__ import annotations

from pathlib import Path

from netsuite_llm_wiki_mcp.archive_migration import apply_legacy_migration, plan_legacy_migration
from netsuite_llm_wiki_mcp.archive_service import ArchiveService
from netsuite_llm_wiki_mcp.knowledge_dependencies import KnowledgeDependencies


def _page(root: Path, relative: str, lifecycle: str = "deprecated") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\ngenerated: true\nlifecycle: {lifecycle}\n---\n\n# Archived\n", encoding="utf-8")
    return target


def test_archive_restore_is_immutable_and_plan_gated(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = _page(root, "wiki/concepts/example.md")
    service = ArchiveService(root)
    assert service.apply("")["code"] == "archive_plan_required"
    planned = service.plan_archive("wiki/concepts/example.md", reason="deprecated")
    committed = service.apply(planned["plan_id"])
    assert committed["ok"] and not page.exists()
    bundle = next((root / "archives" / "bundles").glob("*/*/*"))
    manifest_before = (bundle / "manifest.yaml").read_bytes()
    restored = service.plan_restore(committed["archive_id"])
    assert service.apply(restored["plan_id"])["ok"] and page.exists()
    assert (bundle / "manifest.yaml").read_bytes() == manifest_before


def test_raw_active_dependency_blocks_unless_cascade(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    raw = root / "raw/sources/file/proj/source.md"; raw.parent.mkdir(parents=True); raw.write_text("source", encoding="utf-8")
    page = _page(root, "wiki/concepts/example.md")
    deps = KnowledgeDependencies(root)
    deps.update_page("wiki/concepts/example.md", "hash", {"raw/sources/file/proj/source.md": "hash"}, generated=True, lifecycle="deprecated")
    service = ArchiveService(root)
    blocked = service.plan_archive("raw/sources/file/proj/source.md")
    assert blocked["ok"] is False and blocked["blockers"][0]["code"] == "archive_dependency_blocked"
    cascading = service.plan_archive("raw/sources/file/proj/source.md", cascade=True)
    assert cascading["ok"] is True and {item["original_path"] for item in cascading["items"]} == {"raw/sources/file/proj/source.md", "wiki/concepts/example.md"}


def test_recovery_restores_active_file_after_detaching_fault(tmp_path: Path) -> None:
    root = tmp_path / "vault"; page = _page(root, "wiki/concepts/example.md")
    service = ArchiveService(root, fault_at="detaching")
    planned = service.plan_archive("wiki/concepts/example.md", reason="deprecated")
    failed = service.apply(planned["plan_id"])
    assert failed["ok"] is False and page.exists()
    assert service.recover()["ok"]


def test_restore_failure_rolls_back_files_created_by_that_operation(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = _page(root, "wiki/concepts/example.md")
    service = ArchiveService(root)
    archive_plan = service.plan_archive("wiki/concepts/example.md", reason="deprecated")
    archive = service.apply(archive_plan["plan_id"])
    assert archive["ok"] and not page.exists()

    failing = ArchiveService(root, fault_at="pending")
    restore_plan = failing.plan_restore(archive["archive_id"])
    result = failing.apply(restore_plan["plan_id"])
    assert result["ok"] is False
    assert not page.exists()
    assert failing.recover()["recovered"] == []


def test_legacy_migration_blocks_non_markdown_queries_and_is_repeatable(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    query = root / "wiki" / "queries" / "unexpected.json"
    query.parent.mkdir(parents=True)
    query.write_text("{}", encoding="utf-8")
    assert plan_legacy_migration(root)["ok"] is False

    query.unlink()
    (root / "wiki" / "queries" / ".gitkeep").write_text("", encoding="utf-8")
    assert plan_legacy_migration(root)["ok"] is True

    chat = root / "wiki" / "chatlog" / "session.md"
    chat.parent.mkdir(parents=True)
    chat.write_text("Authorization: token_verysecretvalue\n", encoding="utf-8")
    first = apply_legacy_migration(root)
    assert first["ok"] is True
    migrated = root / "raw" / "sources" / "chat" / "legacy" / "session.md"
    assert "source_kind: \"legacy_chatlog\"" in migrated.read_text(encoding="utf-8")
    assert apply_legacy_migration(root)["already_migrated"] is True


def test_legacy_migration_serializes_date_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    chat = root / "wiki" / "chatlog" / "session.md"
    chat.parent.mkdir(parents=True)
    chat.write_text(
        "---\ntype: chatlog\ntitle: Session\ndate: 2026-07-28\n---\n\nbody\n",
        encoding="utf-8",
    )

    plan = plan_legacy_migration(root)
    assert plan["ok"] is True

    migrated = apply_legacy_migration(root)
    assert migrated["ok"] is True
    migrated_text = (root / "raw" / "sources" / "chat" / "legacy" / "session.md").read_text(encoding="utf-8")
    assert "date: \"2026-07-28\"" in migrated_text
    assert "source_kind: \"legacy_chatlog\"" in migrated_text

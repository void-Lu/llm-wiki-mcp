from __future__ import annotations

from pathlib import Path

import netsuite_llm_wiki_mcp.archive_service as archive_service_module
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


def test_archive_rejects_new_reason_outside_manifest_contract(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = _page(root, "wiki/concepts/example.md")

    planned = ArchiveService(root).plan_archive("wiki/concepts/example.md", reason="mcp_crud_validation_cleanup")

    assert planned["ok"] is False
    assert planned["code"] == "invalid_archive_reason"
    assert page.exists()


def test_restore_accepts_legacy_custom_reason_without_mutating_bundle(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = _page(root, "wiki/concepts/example.md")
    service = ArchiveService(root)
    archive_plan = service.plan_archive("wiki/concepts/example.md", reason="deprecated")
    archive = service.apply(archive_plan["plan_id"])
    bundle = next((root / "archives" / "bundles").glob("*/*/*"))
    manifest = bundle / "manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("reason: deprecated", "reason: legacy_cleanup"), encoding="utf-8")
    manifest_before = manifest.read_bytes()

    assert service.rebuild_archive_index()["ok"] is True
    restore_plan = service.plan_restore(archive["archive_id"])

    assert restore_plan["ok"] is True
    assert service.apply(restore_plan["plan_id"])["ok"] is True
    assert page.exists()
    assert manifest.read_bytes() == manifest_before


def test_raw_active_dependency_blocks_unless_cascade(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    raw = root / "raw/sources/file/proj/source.md"; raw.parent.mkdir(parents=True); raw.write_text("source", encoding="utf-8")
    _page(root, "wiki/concepts/example.md")
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


def test_final_bundle_rename_failure_restores_active_file(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"; page = _page(root, "wiki/concepts/example.md")
    service = ArchiveService(root)
    planned = service.plan_archive("wiki/concepts/example.md", reason="deprecated")
    original_replace = archive_service_module.os.replace

    def fail_final_rename(source, destination):
        if "bundles" in Path(destination).parts:
            raise OSError("injected final bundle rename failure")
        return original_replace(source, destination)

    monkeypatch.setattr(archive_service_module.os, "replace", fail_final_rename)
    failed = service.apply(planned["plan_id"])

    assert failed["ok"] is False
    assert page.exists()
    assert not list((root / "archives" / ".pending").glob("*.recovery"))


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
    assert first["archive_id"]
    migrated = next((root / "archives" / "bundles").glob(f"*/*/{first['archive_id']}/wiki/chatlog/session.md"))
    assert migrated.read_text(encoding="utf-8") == "Authorization: token_verysecretvalue\n"
    assert not chat.exists()
    assert not (root / "raw" / "sources" / "chat" / "legacy" / "session.md").exists()
    assert apply_legacy_migration(root)["already_migrated"] is True


def test_legacy_migration_preserves_date_frontmatter(tmp_path: Path) -> None:
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
    archived = next((root / "archives" / "bundles").glob(f"*/*/{migrated['archive_id']}/wiki/chatlog/session.md"))
    migrated_text = archived.read_text(encoding="utf-8")
    assert "date: 2026-07-28" in migrated_text


def test_legacy_migration_archives_previously_migrated_chat_sources(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    legacy = root / "raw" / "sources" / "chat" / "legacy" / "2026" / "session.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("previously migrated legacy chat", encoding="utf-8")

    migrated = apply_legacy_migration(root)

    assert migrated["ok"] is True
    assert not legacy.exists()
    archived = next(
        (root / "archives" / "bundles").glob(
            f"*/*/{migrated['archive_id']}/raw/sources/chat/legacy/2026/session.md"
        )
    )
    assert archived.read_text(encoding="utf-8") == "previously migrated legacy chat"

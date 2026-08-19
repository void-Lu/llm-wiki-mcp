from __future__ import annotations

import json
from pathlib import Path

import pytest

import wiki.log_volume as log_volume
import wiki.wiki_log as wiki_log

from wiki.atomic_file import AtomicFileError
from wiki.page_operation_store import PageOperationStore
from wiki.wiki_log import WikiLogStore, append_log_entry, read_recent_log_entries
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import create_wiki_root


def test_append_log_entry_uses_parseable_heading_and_fields(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="CodeGraph alpha",
            paths=["wiki/projects/alpha/architecture/script.md"],
            sources=["raw/sources/projects/alpha/requirements/status.json"],
            project="alpha",
            status="ok",
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    text = (root / "wiki/log.md").read_text(encoding="utf-8")
    assert "## [2026-05-26T10:20:30Z] ingest | CodeGraph alpha" in text
    assert "- project: alpha" in text
    assert "- status: ok" in text
    assert "- paths:" in text
    assert "  - wiki/projects/alpha/architecture/script.md" in text
    assert "- sources:" in text
    assert "  - raw/sources/projects/alpha/requirements/status.json" in text


def test_append_log_entry_redacts_persisted_strings(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    append_log_entry(
        root,
        WikiLogEntry(
            operation="query",
            title="Ask a@example.com",
            paths=["wiki/queries/token-api_abc1234567890.md"],
            sources=["raw/sources/phone-13800138000.md"],
            project="alpha",
            status="ok",
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    text = (root / "wiki/log.md").read_text(encoding="utf-8")
    assert "a@example.com" not in text
    assert "api_abc1234567890" not in text
    assert "13800138000" not in text
    assert "[REDACTED_EMAIL]" in text
    assert "[UNSAFE_LOCATOR]" in text


def test_read_recent_log_entries_returns_latest_headings(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(3):
        append_log_entry(
            root,
            WikiLogEntry(
                operation="query",
                title=f"Question {index}",
                paths=[],
                sources=[],
                status="ok",
                timestamp=f"2026-05-26T10:20:3{index}Z",
            ),
        )

    assert read_recent_log_entries(root, limit=2) == [
        "## [2026-05-26T10:20:32Z] query | Question 2",
        "## [2026-05-26T10:20:31Z] query | Question 1",
    ]


def test_operation_index_rebuilds_once_and_deduplicates_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    entry = WikiLogEntry(operation="update", title="Indexed", operation_id="operation-001", timestamp="2026-05-26T10:20:30Z")

    first = append_log_entry(root, entry)
    assert first["operation_index_rebuilt"] is True
    manifest = root / ".llm-wiki/log-operation-index.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["operation_ids"] == ["operation-001"]

    duplicate = append_log_entry(root, entry, log_store=WikiLogStore(root))
    assert duplicate["deduplicated"] is True
    assert "operation_index_rebuilt" not in duplicate
    assert (root / "wiki/log.md").read_text(encoding="utf-8").count("- operation_id: operation-001") == 1


def test_operation_index_normal_append_does_not_scan_archive_volumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    append_log_entry(root, WikiLogEntry(operation="update", title="First", operation_id="operation-002"))
    def fail_scan(_root: Path) -> set[str]:
        raise AssertionError("normal append must load the manifest instead of scanning archives")

    monkeypatch.setattr(wiki_log, "_scan_operation_ids", fail_scan)
    result = append_log_entry(root, WikiLogEntry(operation="update", title="Second", operation_id="operation-003"))

    assert result["ok"] is True
    assert "operation_index_rebuilt" not in result


def test_corrupt_operation_index_rebuilds_from_active_and_archived_logs(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    entry = WikiLogEntry(operation="update", title="Corruptible", operation_id="operation-004")
    append_log_entry(root, entry)
    (root / ".llm-wiki/log-operation-index.json").write_text("{not json", encoding="utf-8")
    result = append_log_entry(root, entry)

    assert result["deduplicated"] is True
    assert result["operation_index_rebuilt"] is True


def test_operation_index_rebuild_finds_ids_in_rotated_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    monkeypatch.setattr(log_volume, "MAX_LOG_ENTRIES", 2)
    monkeypatch.setattr(log_volume, "TARGET_LOG_ENTRIES", 1)
    first = WikiLogEntry(operation="update", title="Archived", operation_id="operation-archive-001")
    append_log_entry(root, first)
    append_log_entry(root, WikiLogEntry(operation="update", title="Active", operation_id="operation-archive-002"))
    append_log_entry(root, WikiLogEntry(operation="update", title="Newest", operation_id="operation-archive-003"))
    assert "Archived" not in (root / "wiki/log.md").read_text(encoding="utf-8")

    (root / ".llm-wiki/log-operation-index.json").unlink()
    result = append_log_entry(root, first)

    assert result["deduplicated"] is True
    assert result["operation_index_rebuilt"] is True


def test_operation_index_manifest_failure_rebuilds_after_log_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    log_store = WikiLogStore(root)
    log_store.write_operation_index(set())
    original_atomic_write_text = wiki_log.atomic_write_text

    def fail_manifest(target: str | Path, text: str):
        if Path(target).name == "log-operation-index.json":
            raise AtomicFileError("injected_manifest_failure")
        return original_atomic_write_text(target, text)

    monkeypatch.setattr(wiki_log, "atomic_write_text", fail_manifest)
    entry = WikiLogEntry(operation="update", title="Manifest fault", operation_id="operation-005")
    with pytest.raises(AtomicFileError) as error:
        append_log_entry(root, entry, log_store=log_store)
    assert error.value.code == "injected_manifest_failure"
    assert (root / "wiki/log.md").read_text(encoding="utf-8").count("- operation_id: operation-005") == 1

    monkeypatch.setattr(wiki_log, "atomic_write_text", original_atomic_write_text)
    retry = append_log_entry(root, entry, log_store=log_store)
    assert retry["deduplicated"] is True
    assert retry["operation_index_rebuilt"] is True
    assert (root / "wiki/log.md").read_text(encoding="utf-8").count("- operation_id: operation-005") == 1


def test_operation_journal_audit_stage_is_primary_over_manifest(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    store = PageOperationStore(root)
    operation = store.create_operation(
        request_key="journal-primary",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash="base",
        intended_hash="intended",
        operation_id="operation-journal-primary",
    )
    store.record_stage(operation.operation_id, "audit_log", "succeeded", result={"ok": True})
    manifest = root / ".llm-wiki/log-operation-index.json"
    manifest.write_text("{not json", encoding="utf-8")

    result = append_log_entry(
        root,
        WikiLogEntry(operation="update", title="Journal primary", operation_id=operation.operation_id),
        operation_store=store,
    )

    assert result["deduplicated"] is True
    assert manifest.read_text(encoding="utf-8") == "{not json"


def test_operation_journal_repair_uses_manifest_as_history_fallback(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    entry = WikiLogEntry(operation="update", title="Repair fallback", operation_id="operation-repair-fallback")
    append_log_entry(root, entry)
    store = PageOperationStore(root)
    operation = store.create_operation(
        request_key="journal-repair",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash="base",
        intended_hash="intended",
        operation_id=entry.operation_id,
    )
    store.record_stage(operation.operation_id, "audit_log", "failed", code="audit_log_failed")

    result = append_log_entry(root, entry, operation_store=store)

    assert result["deduplicated"] is True
    assert (root / "wiki/log.md").read_text(encoding="utf-8").count("- operation_id: operation-repair-fallback") == 1


def test_append_log_entry_rotation_and_dedup_crosses_volume_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    monkeypatch.setattr(log_volume, "MAX_LOG_ENTRIES", 2)
    monkeypatch.setattr(log_volume, "TARGET_LOG_ENTRIES", 1)
    first = WikiLogEntry(operation="update", title="Archived", operation_id="operation-seam-001")

    append_log_entry(root, first)
    append_log_entry(root, WikiLogEntry(operation="update", title="Active", operation_id="operation-seam-002"))
    append_log_entry(root, WikiLogEntry(operation="update", title="Newest", operation_id="operation-seam-003"))

    assert "Archived" not in (root / "wiki/log.md").read_text(encoding="utf-8")
    result = append_log_entry(root, first, log_store=WikiLogStore(root))

    assert result["deduplicated"] is True
    assert (root / "wiki/log.md").read_text(encoding="utf-8").count("- operation_id: operation-seam-001") == 0
    archived = "\n".join(path.read_text(encoding="utf-8") for path in (root / "archives/log").rglob("log-*.md"))
    assert archived.count("- operation_id: operation-seam-001") == 1

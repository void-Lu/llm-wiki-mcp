from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import wiki.wiki_log as wiki_log

from wiki.atomic_file import AtomicFileError, fault_context
from wiki.wiki_limits import HARD_PAGE_BYTES, TARGET_PAGE_BYTES, partition_rendered_units, utf8_size
from wiki.wiki_limits import split_text_by_utf8
from wiki.wiki_io import split_frontmatter
from wiki.page_operation_store import PageOperationStore
from wiki.wiki_log import WikiLogStore, append_log_entry, read_recent_log_entries
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import create_wiki_root


def _assert_obsidian_links_resolve(root: Path, text: str) -> None:
    for target in re.findall(r"\[\[([^|\]#]+)(?:#[^|\]]+)?(?:\|[^\]]+)?\]\]", text):
        path = root / target
        if path.suffix != ".md":
            path = path.with_suffix(".md")
        assert path.is_file(), f"unresolved Obsidian link: {target}"


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


def test_append_log_entry_archives_oldest_half_when_exceeding_limit(tmp_path: Path):
    """When log exceeds 200 entries, the oldest 100 are archived, leaving 100."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(201):
        append_log_entry(
            root,
            WikiLogEntry(
                operation="query",
                title=f"Question {index:03d}",
                paths=[],
                sources=[],
                status="ok",
                timestamp=f"2026-05-26T10:{index // 60:02d}:{index % 60:02d}Z",
            ),
        )

    headings = read_recent_log_entries(root, limit=250)
    text = (root / "wiki/log.md").read_text(encoding="utf-8")
    archived = sorted((root / "archives/log/2026/05").glob("log-*.md"))

    # After 201 entries: 101 oldest archived (0-100), 100 kept in log (101-200)
    assert len(headings) == 100
    assert "Question 100" not in text
    assert "Question 101" in text
    assert archived
    assert "Question 000" in archived[0].read_text(encoding="utf-8")
    assert "Question 100" in archived[0].read_text(encoding="utf-8")
    archive_index = root / "archives/log/index.md"
    assert archive_index.exists()
    assert utf8_size(archive_index.read_text(encoding="utf-8")) <= HARD_PAGE_BYTES
    assert not (root / "wiki/archives").exists()


def test_partition_rendered_units_counts_utf8_bytes_without_splitting_chinese_units():
    header = "# 日志"
    units = ["- " + "中文" * 20, "- " + "中文" * 20, "- " + "中文" * 20]

    pages, oversized = partition_rendered_units(units, header, target_bytes=150)

    assert oversized == []
    assert [unit for page in pages for unit in page] == units
    assert all(utf8_size(header + "\n\n" + "\n\n".join(page) + "\n") <= 150 for page in pages)


def test_split_text_by_utf8_preserves_unicode_text_and_rendered_page_limit():
    source = "开头\n" + "路径" * 500 + "\n结尾"
    header = "# 归档分卷"
    footer = "[[archives/log/index.md|总索引]]"

    chunks = split_text_by_utf8(source, header, footer, target_bytes=300)

    assert len(chunks) > 1
    assert "".join(chunks) == source
    assert all(utf8_size(f"{header}\n\n{chunk.rstrip()}\n\n{footer}\n") <= 300 for chunk in chunks)


def test_append_log_entry_archives_large_single_entry_and_keeps_summary_link(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    oversized_path = "wiki/" + "路径" * 30_000

    result = append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="超大记录",
            paths=[oversized_path],
            sources=[],
            project="alpha",
            status="ok",
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    assert result["ok"] is True
    archived = result["archived"]
    assert isinstance(archived, list)
    assert archived
    first_archived = archived[0]
    assert isinstance(first_archived, str)
    active = (root / "wiki/log.md").read_text(encoding="utf-8")
    detail = root / first_archived
    assert "details archived" in active
    assert f"[[{first_archived}|Full record]]" in active
    assert "wiki/archives" not in active
    assert detail.exists()
    assert utf8_size(active) <= TARGET_PAGE_BYTES
    assert utf8_size(detail.read_text(encoding="utf-8")) <= HARD_PAGE_BYTES


def test_append_log_entry_rotates_few_long_records_on_utf8_bytes(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    long_path = "wiki/" + "路径" * 25_000
    for title, timestamp in (("First", "2026-05-26T10:20:30Z"), ("Second", "2026-05-26T10:20:31Z")):
        append_log_entry(
            root,
            WikiLogEntry(
                operation="ingest",
                title=title,
                paths=[long_path],
                sources=[],
                timestamp=timestamp,
            ),
        )

    active = (root / "wiki/log.md").read_text(encoding="utf-8")
    archives = list((root / "archives/log").rglob("log-*.md"))
    assert "First" not in active
    assert "Second" in active
    assert len(archives) == 1
    assert "First" in archives[0].read_text(encoding="utf-8")
    assert utf8_size(active) <= TARGET_PAGE_BYTES


def test_append_log_entry_splits_oversized_record_into_navigable_volumes(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="超大归档",
            paths=["wiki/" + "路径" * 70_000],
            sources=[],
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    assert isinstance(result, dict)
    assert result["ok"] is True
    archived_value = result["archived"]
    assert isinstance(archived_value, list)
    archived_paths = [path for path in archived_value if isinstance(path, str)]
    assert len(archived_paths) == len(archived_value)
    archived = [root / path for path in archived_paths]
    assert len(archived) >= 2
    assert all(path.is_file() for path in archived)
    assert all(utf8_size(path.read_text(encoding="utf-8")) <= min(TARGET_PAGE_BYTES, HARD_PAGE_BYTES) for path in archived)

    active = (root / "wiki/log.md").read_text(encoding="utf-8")
    assert f"[[{archived_paths[0]}|Full record]]" in active
    archive_index = root / "archives/log/index.md"
    index_text = archive_index.read_text(encoding="utf-8")
    for number, path in enumerate(archived):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = split_frontmatter(text)
        assert frontmatter["generated"] is True
        assert f"[[{relative}|" in index_text
        assert "[[archives/log/index.md|Archive index]]" in text
        if number:
            previous = archived[number - 1].relative_to(root).as_posix()
            assert f"[[{previous}|Previous volume]]" in text
        if number + 1 < len(archived):
            following = archived[number + 1].relative_to(root).as_posix()
            assert f"[[{following}|Next volume]]" in text
        _assert_obsidian_links_resolve(root, text)
    _assert_obsidian_links_resolve(root, index_text)


def test_append_log_entry_rejects_when_archive_wrapper_cannot_fit(tmp_path: Path, monkeypatch):
    root = tmp_path / "vault"
    create_wiki_root(root)
    monkeypatch.setattr(wiki_log, "TARGET_PAGE_BYTES", 64)

    result = append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="无法归档",
            paths=["wiki/small.md"],
            sources=[],
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    assert result["ok"] is False
    assert result["code"] == "log_entry_too_large"
    assert read_recent_log_entries(root) == []


def test_archive_index_includes_existing_top_level_archives(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    existing = root / "archives/log/2026/04/log-007.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# Existing archive\n", encoding="utf-8")

    append_log_entry(
        root,
        WikiLogEntry(operation="query", title="Current", timestamp="2026-05-26T10:20:30Z"),
    )

    index = (root / "archives/log/index.md").read_text(encoding="utf-8")
    assert "[[archives/log/2026/04/log-007.md|log-007]]" in index
    _assert_obsidian_links_resolve(root, index)


def test_archive_index_never_overwrites_a_manual_paged_index(tmp_path: Path, monkeypatch):
    root = tmp_path / "vault"
    create_wiki_root(root)
    archive_dir = root / "archives/log"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-05-log-001.md").write_text("# First\n", encoding="utf-8")
    (archive_dir / "2026-05-log-002.md").write_text("# Second\n", encoding="utf-8")
    manual_page = archive_dir / "index-02.md"
    manual_page.write_text("---\ngenerated: false\n---\n\n# Manual archive page\n", encoding="utf-8")
    monkeypatch.setattr(wiki_log, "TARGET_PAGE_BYTES", 200)

    wiki_log._write_archive_index(root)

    assert manual_page.read_text(encoding="utf-8").endswith("# Manual archive page\n")
    assert not (archive_dir / "index.md").exists()


def test_log_multi_file_write_is_per_file_atomic_not_a_cross_file_transaction(tmp_path: Path) -> None:
    first = tmp_path / "archives/log/2026/05/log-001.md"
    second = tmp_path / "archives/log/2026/05/log-002.md"
    first.parent.mkdir(parents=True)
    first.write_text("old first", encoding="utf-8")
    second.write_text("old second", encoding="utf-8")
    temp_write_count = 0

    def fault(stage: str) -> None:
        nonlocal temp_write_count
        if stage == "temp_write":
            temp_write_count += 1
            if temp_write_count == 2:
                raise RuntimeError("injected")

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            wiki_log._atomic_write_many({first: "new first", second: "new second"})

    assert first.read_text(encoding="utf-8") == "new first"
    assert second.read_text(encoding="utf-8") == "old second"
    assert list(first.parent.glob(".*.tmp")) == []


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
    monkeypatch.setattr(wiki_log, "MAX_LOG_ENTRIES", 2)
    monkeypatch.setattr(wiki_log, "TARGET_LOG_ENTRIES", 1)
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
    wiki_log._write_operation_index(root, set(), log_store=log_store)
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

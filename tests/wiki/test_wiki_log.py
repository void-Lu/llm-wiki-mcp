from __future__ import annotations

from pathlib import Path

import wiki.wiki_log as wiki_log

from wiki.wiki_limits import HARD_PAGE_BYTES, TARGET_PAGE_BYTES, partition_rendered_units, utf8_size
from wiki.wiki_log import append_log_entry, read_recent_log_entries
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
            sources=["raw/sources/projects/alpha/codegraph/status.json"],
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
    assert "  - raw/sources/projects/alpha/codegraph/status.json" in text


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
    assert "[REDACTED_SECRET]" in text
    assert "[REDACTED_PHONE]" in text


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
    archived = sorted((root / "wiki/archives/log/2026/05").glob("log-*.md"))

    # After 201 entries: 101 oldest archived (0-100), 100 kept in log (101-200)
    assert len(headings) == 100
    assert "Question 100" not in text
    assert "Question 101" in text
    assert archived
    assert "Question 000" in archived[0].read_text(encoding="utf-8")
    assert "Question 100" in archived[0].read_text(encoding="utf-8")
    archive_index = root / "wiki/archives/log/index.md"
    assert archive_index.exists()
    assert utf8_size(archive_index.read_text(encoding="utf-8")) <= HARD_PAGE_BYTES


def test_partition_rendered_units_counts_utf8_bytes_without_splitting_chinese_units():
    header = "# 日志"
    units = ["- " + "中文" * 20, "- " + "中文" * 20, "- " + "中文" * 20]

    pages, oversized = partition_rendered_units(units, header, target_bytes=150)

    assert oversized == []
    assert [unit for page in pages for unit in page] == units
    assert all(utf8_size(header + "\n\n" + "\n\n".join(page) + "\n") <= 150 for page in pages)


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
    assert result["archived"]
    active = (root / "wiki/log.md").read_text(encoding="utf-8")
    detail = root / result["archived"][0]
    assert "details archived" in active
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
    archives = list((root / "wiki/archives/log").rglob("log-*.md"))
    assert "First" not in active
    assert "Second" in active
    assert len(archives) == 1
    assert "First" in archives[0].read_text(encoding="utf-8")
    assert utf8_size(active) <= TARGET_PAGE_BYTES


def test_append_log_entry_rejects_record_that_cannot_fit_in_an_archive_page(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="无法归档",
            paths=["wiki/" + "路径" * 65_000],
            sources=[],
            timestamp="2026-05-26T10:20:30Z",
        ),
    )

    assert result["ok"] is False
    assert result["code"] == "log_entry_too_large"
    assert read_recent_log_entries(root) == []


def test_archive_index_never_overwrites_a_manual_paged_index(tmp_path: Path, monkeypatch):
    root = tmp_path / "vault"
    create_wiki_root(root)
    archive_dir = root / "wiki/archives/log"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-05-log-001.md").write_text("# First\n", encoding="utf-8")
    (archive_dir / "2026-05-log-002.md").write_text("# Second\n", encoding="utf-8")
    manual_page = archive_dir / "index-02.md"
    manual_page.write_text("---\ngenerated: false\n---\n\n# Manual archive page\n", encoding="utf-8")
    monkeypatch.setattr(wiki_log, "TARGET_PAGE_BYTES", 200)

    wiki_log._write_archive_index(root)

    assert manual_page.read_text(encoding="utf-8").endswith("# Manual archive page\n")
    assert not (archive_dir / "index.md").exists()

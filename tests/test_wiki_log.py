from __future__ import annotations

from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_log import append_log_entry, parse_log_entries, read_recent_log_entries
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


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


def test_parse_log_entries_returns_structured_entries(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    append_log_entry(
        root,
        WikiLogEntry(
            operation="ingest",
            title="CodeGraph alpha",
            paths=["wiki/projects/alpha/architecture/script.md", "wiki/sources/projects/alpha/architecture/codegraph.md"],
            sources=["raw/sources/projects/alpha/codegraph/context.json"],
            project="alpha",
            status="ok",
            timestamp="2026-05-26T10:20:30Z",
        ),
    )
    append_log_entry(
        root,
        WikiLogEntry(
            operation="llm_ingest",
            title="alpha/docs",
            paths=["wiki/sources/alpha-docs.md"],
            sources=["raw/sources/file/alpha/docs/notes.md"],
            project="alpha",
            status="ok",
            timestamp="2026-05-26T11:00:00Z",
        ),
    )

    entries = parse_log_entries(root, limit=5)

    assert len(entries) == 2
    assert entries[0]["timestamp"] == "2026-05-26T11:00:00Z"
    assert entries[0]["operation"] == "llm_ingest"
    assert entries[0]["title"] == "alpha/docs"
    assert entries[0]["project"] == "alpha"
    assert entries[0]["status"] == "ok"
    assert entries[0]["paths"] == ["wiki/sources/alpha-docs.md"]
    assert entries[0]["sources"] == ["raw/sources/file/alpha/docs/notes.md"]
    assert entries[1]["timestamp"] == "2026-05-26T10:20:30Z"
    assert entries[1]["operation"] == "ingest"


def test_append_log_entry_archives_old_entries_after_two_hundred(tmp_path: Path):
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

    entries = parse_log_entries(root, limit=250)
    text = (root / "wiki/log.md").read_text(encoding="utf-8")
    archived = sorted((root / "wiki/archives/2026/05/26/log").glob("wiki-log-*.md"))

    assert len(entries) == 200
    assert "Question 000" not in text
    assert "Question 001" in text
    assert archived
    assert "Question 000" in archived[0].read_text(encoding="utf-8")

from __future__ import annotations

import re
from pathlib import Path

import pytest

import wiki.log_volume as log_volume

from wiki.atomic_file import AtomicFileError, fault_context
from wiki.wiki_io import split_frontmatter
from wiki.wiki_limits import HARD_PAGE_BYTES, TARGET_PAGE_BYTES, partition_rendered_units, split_text_by_utf8, utf8_size
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import create_wiki_root


def _assert_obsidian_links_resolve(root: Path, text: str) -> None:
    for target in re.findall(r"\[\[([^|\]#]+)(?:#[^|\]]+)?(?:\|[^\]]+)?\]\]", text):
        path = root / target
        if path.suffix != ".md":
            path = path.with_suffix(".md")
        assert path.is_file(), f"unresolved Obsidian link: {target}"


def test_split_and_join_blocks_preserve_complete_entries() -> None:
    text = "# Log\n\n## [2026-05-26T10:20:30Z] query | One\n- status: ok\n\n## [2026-05-26T10:20:31Z] query | Two\n- status: ok\n"

    preamble, blocks = log_volume.split_blocks(text)

    assert preamble == "# Log"
    assert blocks == [
        "## [2026-05-26T10:20:30Z] query | One\n- status: ok",
        "## [2026-05-26T10:20:31Z] query | Two\n- status: ok",
    ]
    assert log_volume.join_blocks(preamble, blocks) == text


def test_read_blocks_uses_default_preamble_for_missing_log(tmp_path: Path) -> None:
    preamble, blocks = log_volume.read_blocks(tmp_path / "wiki/log.md", "# Log")

    assert preamble == "# Log"
    assert blocks == []


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

    headings = [block.splitlines()[0] for block in log_volume.read_blocks(root / "wiki/log.md", "# Log")[1]]
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
    monkeypatch.setattr(log_volume, "TARGET_PAGE_BYTES", 64)

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
    assert log_volume.read_blocks(root / "wiki/log.md", "# Log")[1] == []


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
    monkeypatch.setattr(log_volume, "TARGET_PAGE_BYTES", 200)

    log_volume._write_archive_index(root)

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
            log_volume._atomic_write_many({first: "new first", second: "new second"})

    assert first.read_text(encoding="utf-8") == "new first"
    assert second.read_text(encoding="utf-8") == "old second"
    assert list(first.parent.glob(".*.tmp")) == []

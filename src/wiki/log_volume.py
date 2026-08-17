"""正式知识页日志的有界字节卷宗布局。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from wiki.atomic_file import atomic_write_text
from wiki.wiki_io import split_frontmatter
from wiki.wiki_limits import (
    HARD_PAGE_BYTES,
    MAX_LOG_ENTRIES,
    TARGET_LOG_ENTRIES,
    TARGET_PAGE_BYTES,
    partition_rendered_units,
    render_units,
    split_text_by_utf8,
    utf8_size,
)
from wiki.wiki_paths import ARCHIVES_LOG_DIR, ARCHIVES_LOG_PATH
from wiki.wikilinks import format_wikilink


_LOG_HEADING_RE = re.compile(r"^## \[([^\]]+)\] (\S+) \| (.+)$")
_INDEX_PAGE_RE = re.compile(r"^index(?:-\d{2,})?\.md$")
_ARCHIVE_HEADER = "---\ntype: log_archive\ngenerated: true\narchived: true\n---\n\n# Log archive"
_ARCHIVE_NAVIGATION_INDEX = "archives/log/index.md"


@dataclass(frozen=True)
class PublishOutcome:
    """一次日志发布在卷宗布局上的结果。"""

    keep_ok: bool
    archived_rel_paths: list[str]


def publish(
    vault_root: str | Path,
    timestamp: str,
    block: str,
    *,
    render_summary: Callable[[str], str],
) -> PublishOutcome:
    """把一个完整日志块发布到活动卷或归档卷。"""

    root = Path(vault_root).expanduser().resolve()
    log_path = root / "wiki" / "log.md"
    preamble, blocks = read_blocks(log_path, "# Log")
    archived_paths: list[Path] = []

    if utf8_size(join_blocks(preamble, [block])) > _archive_target_bytes():
        try:
            detail_paths = _write_archive_document(root, timestamp, [block], prefix="log")
        except ValueError:
            return PublishOutcome(keep_ok=False, archived_rel_paths=[])
        archived_paths.extend(detail_paths)
        block = render_summary(detail_paths[0].relative_to(root).as_posix())

    keep, overflow = _rotate_blocks(preamble, [*blocks, block])
    if overflow:
        archived_paths.extend(_write_archived_log_blocks(root, "wiki-log", overflow))
    _atomic_write(log_path, join_blocks(preamble, keep))

    for archive_path in archived_paths:
        _append_archive_log(root, archive_path)
    if archived_paths or not (root / ARCHIVES_LOG_DIR / "index.md").exists():
        _write_archive_index(root)

    return PublishOutcome(
        keep_ok=True,
        archived_rel_paths=[path.relative_to(root).as_posix() for path in archived_paths],
    )


def split_blocks(text: str) -> tuple[str, list[str]]:
    """按完整日志条目拆分前导文本和日志块。"""

    lines = text.splitlines()
    preamble_lines: list[str] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("## ["):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is None:
            preamble_lines.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append(current)
    preamble = "\n".join(preamble_lines).rstrip()
    return preamble, ["\n".join(block).rstrip() for block in blocks]


def join_blocks(preamble: str, blocks: list[str]) -> str:
    """按日志文件约定重新渲染前导文本和完整日志块。"""

    return render_units(preamble, blocks)


def read_blocks(log_path: str | Path, default_preamble: str) -> tuple[str, list[str]]:
    """读取日志块；缺失文件时返回默认前导文本和空块列表。"""

    path = Path(log_path)
    if not path.exists():
        return default_preamble, []
    preamble, blocks = split_blocks(path.read_text(encoding="utf-8"))
    return preamble or default_preamble, blocks


def _rotate_blocks(preamble: str, blocks: list[str]) -> tuple[list[str], list[str]]:
    """只移动最旧的完整块，直到活动日志满足两个容量上限。"""

    if len(blocks) <= MAX_LOG_ENTRIES and utf8_size(join_blocks(preamble, blocks)) <= _archive_target_bytes():
        return blocks, []

    if len(blocks) > MAX_LOG_ENTRIES:
        keep = blocks[-TARGET_LOG_ENTRIES:]
        overflow = blocks[:-TARGET_LOG_ENTRIES]
    else:
        keep = list(blocks)
        overflow = []
    while keep and utf8_size(join_blocks(preamble, keep)) > _archive_target_bytes():
        overflow.append(keep.pop(0))
    return keep, overflow


def _write_archived_log_blocks(root: Path, source_log_name: str, blocks: list[str]) -> list[Path]:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for block in blocks:
        grouped[_archive_month_parts(block)].append(block)
    written: list[Path] = []
    for (year, month), group in sorted(grouped.items()):
        written.extend(
            _write_archive_document(
                root,
                f"{year}-{month}-01T00:00:00Z",
                group,
                prefix=_archive_prefix(source_log_name),
            )
        )
    return written


def _archive_prefix(source_log_name: str) -> str:
    return "log" if source_log_name == "wiki-log" else source_log_name


def _write_archive_document(
    root: Path,
    timestamp: str,
    units: list[str],
    *,
    prefix: str,
    header: str = _ARCHIVE_HEADER,
) -> list[Path]:
    year, month = _archive_month_parts_from_timestamp(timestamp)
    archive_dir = root / ARCHIVES_LOG_DIR / year / month
    target_bytes = _archive_target_bytes()
    placeholder = _archive_navigation_placeholder(prefix, year, month)
    page_units = _partition_archive_units(units, header, placeholder, target_bytes)
    sequence = _next_archive_sequence(archive_dir, prefix)
    paths = [archive_dir / f"{prefix}-{sequence + index:03d}.md" for index in range(len(page_units))]
    total = len(paths)
    contents: dict[Path, str] = {}
    for index, (path, page) in enumerate(zip(paths, page_units, strict=True), start=1):
        footer = _archive_navigation_footer(prefix, year, month, sequence, index, total)
        rendered = render_units(header, page, footer)
        if utf8_size(rendered) > target_bytes or utf8_size(rendered) > HARD_PAGE_BYTES:
            raise ValueError("archived Markdown page cannot fit into a bounded page")
        contents[path] = rendered
    _atomic_write_many(contents)
    return paths


def _write_archive_text(root: Path, timestamp: str, text: str, prefix: str) -> Path:
    """写入旧的预渲染归档文本，并保留原有私有 API。"""

    year, month = _archive_month_parts_from_timestamp(timestamp)
    placeholder = _archive_navigation_placeholder(prefix, year, month)
    if utf8_size(render_units("", [text], placeholder)) > _archive_target_bytes():
        raise ValueError("archive text was split into multiple pages")
    paths = _write_archive_document(root, timestamp, [text], prefix=prefix, header="")
    return paths[0]


def _partition_archive_units(units: list[str], header: str, footer: str, target_bytes: int) -> list[list[str]]:
    pages: list[list[str]] = []
    current: list[str] = []
    for unit in units:
        if utf8_size(render_units(header, [unit], footer)) > target_bytes:
            if current:
                pages.append(current)
                current = []
            pages.extend([[chunk] for chunk in split_text_by_utf8(unit, header, footer, target_bytes)])
            continue
        candidate = [*current, unit]
        if current and utf8_size(render_units(header, candidate, footer)) > target_bytes:
            pages.append(current)
            current = []
        current.append(unit)
    if current:
        pages.append(current)
    return pages


def _archive_target_bytes() -> int:
    return min(TARGET_PAGE_BYTES, HARD_PAGE_BYTES)


def _archive_navigation_placeholder(prefix: str, year: str, month: str) -> str:
    return " · ".join([
        f"← {format_wikilink(f'archives/log/{year}/{month}/{prefix}-999999.md', 'Previous volume')}",
        format_wikilink(_ARCHIVE_NAVIGATION_INDEX, "Archive index"),
        f"{format_wikilink(f'archives/log/{year}/{month}/{prefix}-999999.md', 'Next volume')} →",
    ])


def _archive_navigation_footer(
    prefix: str,
    year: str,
    month: str,
    first_sequence: int,
    number: int,
    total: int,
) -> str:
    links = [format_wikilink(_ARCHIVE_NAVIGATION_INDEX, "Archive index")]
    current_sequence = first_sequence + number - 1
    if number > 1:
        links.insert(
            0,
            f"← {format_wikilink(f'archives/log/{year}/{month}/{prefix}-{current_sequence - 1:03d}.md', 'Previous volume')}",
        )
    if number < total:
        links.append(
            f"{format_wikilink(f'archives/log/{year}/{month}/{prefix}-{current_sequence + 1:03d}.md', 'Next volume')} →"
        )
    return " · ".join(links)


def _next_archive_sequence(archive_dir: Path, prefix: str) -> int:
    if not archive_dir.exists():
        return 1
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)\.md$")
    values = [
        int(match.group(1))
        for path in archive_dir.glob(f"{prefix}-*.md")
        if (match := pattern.match(path.name))
    ]
    return max(values, default=0) + 1


def _archive_month_parts(block: str) -> tuple[str, str]:
    first_line = block.splitlines()[0] if block.splitlines() else ""
    match = _LOG_HEADING_RE.match(first_line)
    return _archive_month_parts_from_timestamp(match.group(1) if match else "")


def _archive_month_parts_from_timestamp(timestamp: str) -> tuple[str, str]:
    match = re.match(r"(\d{4})-(\d{2})", timestamp)
    if match:
        return match.group(1), match.group(2)
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}", f"{now.month:02d}"


def _append_archive_log(root: Path, archive_path: Path) -> None:
    archive_log = root / ARCHIVES_LOG_PATH
    timestamp = _now()
    rel = archive_path.relative_to(root).as_posix()
    block = "\n".join([
        f"## [{timestamp}] archive_log | Archived wiki log entries",
        "- project: ",
        "- status: ok",
        "- paths:",
        f"  - {rel}",
        "- sources:",
        "  - wiki/log.md",
    ])
    preamble, blocks = read_blocks(archive_log, "# Archives Log")
    keep, overflow = _rotate_blocks(preamble, [*blocks, block])
    if overflow:
        _write_archived_log_blocks(root, "archives-log", overflow)
    _atomic_write(archive_log, join_blocks(preamble, keep))


def _write_archive_index(root: Path) -> None:
    directory = root / ARCHIVES_LOG_DIR
    target = directory / "index.md"
    if target.exists() and not _is_generated_page(target):
        return
    if any(
        path != target and _INDEX_PAGE_RE.match(path.name) and not _is_generated_page(path)
        for path in directory.glob("index-*.md")
    ):
        return
    entries = []
    if directory.exists():
        for path in sorted(directory.rglob("*.md")):
            if _INDEX_PAGE_RE.match(path.name):
                continue
            rel = path.relative_to(root).as_posix()
            entries.append(f"- {format_wikilink(rel, path.stem)}")
    base_header = "---\ntype: index\ngenerated: true\nnavigation: true\n---\n\n# Log archives"
    pages, oversized = partition_rendered_units(entries or ["- 无"], base_header, _index_footer_placeholder(), _archive_target_bytes())
    if oversized:
        raise ValueError("archive index entry cannot fit into a bounded page")
    pages = pages or [["- 无"]]
    contents: dict[Path, str] = {}
    for number, units in enumerate(pages, 1):
        filename = "index.md" if number == 1 else f"index-{number:02d}.md"
        header = base_header if len(pages) == 1 else f"{base_header}\n\nPage {number}/{len(pages)}"
        rendered = render_units(header, units, _index_footer(number, len(pages)))
        if utf8_size(rendered) > _archive_target_bytes() or utf8_size(rendered) > HARD_PAGE_BYTES:
            raise ValueError("archive index cannot fit into a bounded page")
        contents[directory / filename] = rendered
    if any(path.exists() and not _is_generated_page(path) for path in contents):
        return
    _atomic_write_many(contents)
    for path in directory.glob("index-*.md"):
        if path not in contents and _is_generated_page(path):
            path.unlink()


def _index_footer_placeholder() -> str:
    index_pages = f"{_ARCHIVE_NAVIGATION_INDEX[:-3]}-9999.md"
    return " · ".join([
        f"← {format_wikilink(index_pages, 'Previous')}",
        format_wikilink(_ARCHIVE_NAVIGATION_INDEX, "Archive index"),
        f"{format_wikilink(index_pages, 'Next')}",
    ])


def _index_footer(number: int, total: int) -> str:
    links = []
    if number > 1:
        previous = _ARCHIVE_NAVIGATION_INDEX if number == 2 else f"archives/log/index-{number - 1:02d}.md"
        links.append(f"← {format_wikilink(previous, 'Previous')}")
    if number < total:
        next_page = f"archives/log/index-{number + 1:02d}.md"
        links.append(f"{format_wikilink(next_page, 'Next')} →")
    return " · ".join(links)


def _is_generated_page(path: Path) -> bool:
    if not path.exists():
        return False
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter.get("generated") is True


def _atomic_write_many(contents: dict[Path, str]) -> None:
    """逐文件原子替换；这不是跨文件事务。"""

    for target, text in contents.items():
        atomic_write_text(target, text)


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_many({path: text})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["PublishOutcome", "join_blocks", "publish", "read_blocks", "split_blocks"]

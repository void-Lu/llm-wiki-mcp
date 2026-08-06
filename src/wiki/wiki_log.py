from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from common.redaction import redact_sensitive_text
from wiki.wiki_io import split_frontmatter
from wiki.wiki_limits import (
    HARD_PAGE_BYTES,
    MAX_LOG_ENTRIES,
    TARGET_LOG_ENTRIES,
    TARGET_PAGE_BYTES,
    partition_rendered_units,
    render_units,
    utf8_size,
)
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import ARCHIVES_LOG_DIR, ARCHIVES_LOG_PATH

_LOG_HEADING_RE = re.compile(r"^## \[([^\]]+)\] (\S+) \| (.+)$")
_INDEX_PAGE_RE = re.compile(r"^index(?:-\d{2,})?\.md$")


def append_log_entry(vault_root: str | Path, entry: WikiLogEntry) -> dict[str, object]:
    """Append one complete log record while keeping every generated log bounded."""

    root = Path(vault_root)
    log_path = root / "wiki" / "log.md"
    timestamp = entry.timestamp or _now()
    block = _render_log_entry(entry, timestamp)
    preamble, blocks = _read_log_blocks(log_path, "# Log")
    archived_paths: list[Path] = []

    if utf8_size(_join_log_blocks(preamble, [block])) > TARGET_PAGE_BYTES:
        detail = _render_archive_detail(block)
        if utf8_size(detail) > HARD_PAGE_BYTES:
            return {
                "ok": False,
                "code": "log_entry_too_large",
                "error": "log entry cannot fit into a bounded archive page",
                "path": "wiki/log.md",
            }
        detail_path = _write_archive_text(root, timestamp, detail, prefix="log")
        archived_paths.append(detail_path)
        block = _render_archive_summary(entry, timestamp, detail_path.relative_to(root).as_posix())

    next_blocks = [*blocks, block]
    keep, overflow = _rotate_blocks(preamble, next_blocks)
    if overflow:
        archived_paths.extend(_write_archived_log_blocks(root, "wiki-log", overflow))
    _atomic_write(log_path, _join_log_blocks(preamble, keep))

    for archive_path in archived_paths:
        _append_archive_log(root, archive_path)
    _write_archive_index(root)
    return {
        "ok": True,
        "path": "wiki/log.md",
        "archived": [path.relative_to(root).as_posix() for path in archived_paths],
    }


def read_recent_log_entries(vault_root: str | Path, limit: int = 5) -> list[str]:
    log_path = Path(vault_root) / "wiki" / "log.md"
    if not log_path.exists():
        return []
    headings = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.startswith("## [")]
    return list(reversed(headings[-limit:]))


def _render_log_entry(entry: WikiLogEntry, timestamp: str) -> str:
    operation = redact_sensitive_text(entry.operation)
    title = redact_sensitive_text(entry.title)
    project = redact_sensitive_text(entry.project)
    status = redact_sensitive_text(entry.status)
    return "\n".join([
        f"## [{timestamp}] {operation} | {title}",
        f"- project: {project}",
        f"- status: {status}",
        "- paths:",
        *_indented_items(entry.paths),
        "- sources:",
        *_indented_items(entry.sources),
    ])


def _render_archive_summary(entry: WikiLogEntry, timestamp: str, archive_rel: str) -> str:
    operation = _bounded_text(redact_sensitive_text(entry.operation), 256)
    title = _bounded_text(redact_sensitive_text(entry.title), 1_024)
    project = _bounded_text(redact_sensitive_text(entry.project), 1_024)
    status = _bounded_text(redact_sensitive_text(entry.status), 1_024)
    return "\n".join([
        f"## [{timestamp}] {operation} | {title}",
        f"- project: {project}",
        f"- status: {status} (details archived)",
        f"- detail: [[{archive_rel}|Full record]]",
        "- paths:",
        f"  - {len(entry.paths)} paths archived",
        "- sources:",
        f"  - {len(entry.sources)} sources archived",
    ])


def _render_archive_detail(block: str) -> str:
    return render_units(
        "---\ntype: log_archive\ngenerated: true\narchived: true\n---\n\n# Log archive",
        [block],
    )


def _indented_items(items: list[str]) -> list[str]:
    if not items:
        return ["  - none"]
    return [f"  - {redact_sensitive_text(item)}" for item in items]


def _read_log_blocks(log_path: Path, default_preamble: str) -> tuple[str, list[str]]:
    if not log_path.exists():
        return default_preamble, []
    preamble, blocks = _split_log_blocks(log_path.read_text(encoding="utf-8"))
    return preamble or default_preamble, blocks


def _rotate_blocks(preamble: str, blocks: list[str]) -> tuple[list[str], list[str]]:
    """Move only oldest whole blocks until both active limits are satisfied."""

    if len(blocks) <= MAX_LOG_ENTRIES and utf8_size(_join_log_blocks(preamble, blocks)) <= TARGET_PAGE_BYTES:
        return blocks, []

    if len(blocks) > MAX_LOG_ENTRIES:
        keep = blocks[-TARGET_LOG_ENTRIES:]
        overflow = blocks[:-TARGET_LOG_ENTRIES]
    else:
        keep = list(blocks)
        overflow = []
    while keep and utf8_size(_join_log_blocks(preamble, keep)) > TARGET_PAGE_BYTES:
        overflow.append(keep.pop(0))
    return keep, overflow


def _split_log_blocks(text: str) -> tuple[str, list[str]]:
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


def _join_log_blocks(preamble: str, blocks: list[str]) -> str:
    return render_units(preamble, blocks)


def _write_archived_log_blocks(root: Path, source_log_name: str, blocks: list[str]) -> list[Path]:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for block in blocks:
        grouped[_archive_month_parts(block)].append(block)
    written: list[Path] = []
    for (year, month), group in sorted(grouped.items()):
        header = "---\ntype: log_archive\ngenerated: true\narchived: true\n---\n\n# Log archive"
        pages, oversized = partition_rendered_units(group, header, target_bytes=TARGET_PAGE_BYTES)
        for page_blocks in pages:
            written.append(_write_archive_text(root, f"{year}-{month}-01T00:00:00Z", render_units(header, page_blocks), prefix=_archive_prefix(source_log_name)))
        for block in oversized:
            detail = render_units(header, [block])
            if utf8_size(detail) > HARD_PAGE_BYTES:
                raise ValueError("archived log entry cannot fit into a bounded page")
            written.append(_write_archive_text(root, f"{year}-{month}-01T00:00:00Z", detail, prefix=_archive_prefix(source_log_name)))
    return written


def _archive_prefix(source_log_name: str) -> str:
    return "log" if source_log_name == "wiki-log" else source_log_name


def _write_archive_text(root: Path, timestamp: str, text: str, prefix: str) -> Path:
    year, month = _archive_month_parts_from_timestamp(timestamp)
    archive_dir = root / ARCHIVES_LOG_DIR / year / month
    sequence = _next_archive_sequence(archive_dir, prefix)
    archive_path = archive_dir / f"{prefix}-{sequence:03d}.md"
    _atomic_write(archive_path, text)
    return archive_path


def _next_archive_sequence(archive_dir: Path, prefix: str) -> int:
    if not archive_dir.exists():
        return 1
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)\.md$")
    values = [int(match.group(1)) for path in archive_dir.glob(f"{prefix}-*.md") if (match := pattern.match(path.name))]
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
    preamble, blocks = _read_log_blocks(archive_log, "# Archives Log")
    keep, overflow = _rotate_blocks(preamble, [*blocks, block])
    if overflow:
        _write_archived_log_blocks(root, "archives-log", overflow)
    _atomic_write(archive_log, _join_log_blocks(preamble, keep))


def _write_archive_index(root: Path) -> None:
    directory = root / ARCHIVES_LOG_DIR
    target = directory / "index.md"
    if target.exists() and not _is_generated_page(target):
        return
    entries = []
    if directory.exists():
        for path in sorted(directory.rglob("*.md")):
            if _INDEX_PAGE_RE.match(path.name):
                continue
            rel = path.relative_to(directory).as_posix()
            entries.append(f"- [[{rel}|{path.stem}]]")
    base_header = "---\ntype: index\ngenerated: true\nnavigation: true\n---\n\n# Log archives"
    pages, oversized = partition_rendered_units(entries or ["- 无"], base_header, _index_footer_placeholder(), TARGET_PAGE_BYTES)
    if oversized:
        raise ValueError("archive index entry cannot fit into a bounded page")
    pages = pages or [["- 无"]]
    contents: dict[Path, str] = {}
    for number, units in enumerate(pages, 1):
        filename = "index.md" if number == 1 else f"index-{number:02d}.md"
        header = base_header if len(pages) == 1 else f"{base_header}\n\nPage {number}/{len(pages)}"
        contents[directory / filename] = render_units(header, units, _index_footer(number, len(pages)))
    if any(path.exists() and not _is_generated_page(path) for path in contents):
        return
    _atomic_write_many(contents)
    for path in directory.glob("index-*.md"):
        if path not in contents and _is_generated_page(path):
            path.unlink()


def _index_footer_placeholder() -> str:
    return "← [[index-9999.md|Previous]] · [[index-9999.md|Next]]"


def _index_footer(number: int, total: int) -> str:
    links = []
    if number > 1:
        previous = "index.md" if number == 2 else f"index-{number - 1:02d}.md"
        links.append(f"← [[{previous}|Previous]]")
    if number < total:
        links.append(f"[[index-{number + 1:02d}.md|Next]] →")
    return " · ".join(links)


def _is_generated_page(path: Path) -> bool:
    if not path.exists():
        return False
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter.get("generated") is True


def _atomic_write_many(contents: dict[Path, str]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for target, text in contents.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(text, encoding="utf-8")
            staged.append((target, temporary))
        for target, temporary in staged:
            temporary.replace(target)
    finally:
        for _, temporary in staged:
            if temporary.exists():
                temporary.unlink()


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_many({path: text})


def _bounded_text(text: str, max_bytes: int) -> str:
    if utf8_size(text) <= max_bytes:
        return text
    result: list[str] = []
    used = 0
    for character in text:
        size = utf8_size(character)
        if used + size + utf8_size("…") > max_bytes:
            break
        result.append(character)
        used += size
    return "".join(result) + "…"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

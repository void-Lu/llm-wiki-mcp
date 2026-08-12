from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from common.redaction import redact_sensitive_text
from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki.atomic_file import AtomicFileError, FaultBarrier, atomic_write_text
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
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import ARCHIVES_LOG_DIR, ARCHIVES_LOG_PATH
from wiki.wikilinks import format_wikilink

_LOG_HEADING_RE = re.compile(r"^## \[([^\]]+)\] (\S+) \| (.+)$")
_INDEX_PAGE_RE = re.compile(r"^index(?:-\d{2,})?\.md$")
_ARCHIVE_HEADER = "---\ntype: log_archive\ngenerated: true\narchived: true\n---\n\n# Log archive"
_ARCHIVE_NAVIGATION_INDEX = "archives/log/index.md"
_OPERATION_INDEX_SCHEMA_VERSION = 1
_OPERATION_INDEX_PATH = Path(".llm-wiki/log-operation-index.json")
_OPERATION_ID_RE = re.compile(r"^- operation_id:\s*(\S+)\s*$")
_OPERATION_INDEX_CACHE: dict[Path, set[str]] = {}
_OPERATION_INDEX_FORCE_REBUILD: set[Path] = set()
_OPERATION_INDEX_LOCK = RLock()


def append_log_entry(
    vault_root: str | Path,
    entry: WikiLogEntry,
    *,
    fault: FaultBarrier | None = None,
) -> dict[str, object]:
    """Append one complete log record while keeping every generated log bounded."""

    root = Path(vault_root)
    log_path = root / "wiki" / "log.md"
    operation_index: set[str] | None = None
    operation_index_rebuilt = False
    if entry.operation_id:
        operation_index, operation_index_rebuilt = _load_operation_index(root)
        if entry.operation_id in operation_index:
            result: dict[str, object] = {"ok": True, "path": "wiki/log.md", "deduplicated": True}
            if operation_index_rebuilt:
                result["operation_index_rebuilt"] = True
            return result
    timestamp = entry.timestamp or _now()
    block = _render_log_entry(entry, timestamp)
    preamble, blocks = _read_log_blocks(log_path, "# Log")
    archived_paths: list[Path] = []

    try:
        if utf8_size(_join_log_blocks(preamble, [block])) > _archive_target_bytes():
            try:
                detail_paths = _write_archive_document(root, timestamp, [block], prefix="log", fault=fault)
            except AtomicFileError:
                raise
            except ValueError:
                return {
                    "ok": False,
                    "code": "log_entry_too_large",
                    "error": "log entry cannot fit into a bounded archive page",
                    "path": "wiki/log.md",
                }
            archived_paths.extend(detail_paths)
            block = _render_archive_summary(entry, timestamp, detail_paths[0].relative_to(root).as_posix())

        next_blocks = [*blocks, block]
        keep, overflow = _rotate_blocks(preamble, next_blocks)
        if overflow:
            archived_paths.extend(_write_archived_log_blocks(root, "wiki-log", overflow, fault=fault))
        _atomic_write(log_path, _join_log_blocks(preamble, keep), fault=fault)

        for archive_path in archived_paths:
            _append_archive_log(root, archive_path, fault=fault)
        if archived_paths or not (root / ARCHIVES_LOG_DIR / "index.md").exists():
            _write_archive_index(root, fault=fault)
        if entry.operation_id:
            assert operation_index is not None
            operation_index = {*operation_index, entry.operation_id}
            _write_operation_index(root, operation_index, fault=fault)
    except Exception:
        if entry.operation_id:
            _force_operation_index_rebuild(root)
        raise
    return {
        "ok": True,
        "path": "wiki/log.md",
        "archived": [path.relative_to(root).as_posix() for path in archived_paths],
        **({"operation_index_rebuilt": True} if operation_index_rebuilt else {}),
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
    lines = [
        f"## [{timestamp}] {operation} | {title}",
        f"- project: {project}",
        f"- status: {status}",
    ]
    if entry.operation_id:
        lines.append(f"- operation_id: {entry.operation_id}")
    lines.extend([
        "- paths:",
        *_indented_items(entry.paths, field="path"),
        "- sources:",
        *_indented_items(entry.sources, field="source"),
    ])
    return "\n".join(lines)


def _render_archive_summary(entry: WikiLogEntry, timestamp: str, archive_rel: str) -> str:
    operation = _bounded_text(redact_sensitive_text(entry.operation), 256)
    title = _bounded_text(redact_sensitive_text(entry.title), 1_024)
    project = _bounded_text(redact_sensitive_text(entry.project), 1_024)
    status = _bounded_text(redact_sensitive_text(entry.status), 1_024)
    lines = [
        f"## [{timestamp}] {operation} | {title}",
        f"- project: {project}",
        f"- status: {status} (details archived)",
        f"- detail: {format_wikilink(archive_rel, 'Full record')}",
    ]
    if entry.operation_id:
        lines.append(f"- operation_id: {entry.operation_id}")
    lines.extend([
        "- paths:",
        f"  - {len(entry.paths)} paths archived",
        "- sources:",
        f"  - {len(entry.sources)} sources archived",
    ])
    return "\n".join(lines)


def _operation_logged(root: Path, operation_id: str) -> bool:
    """Check the vault-local operation index without scanning archive volumes."""

    operation_ids, _rebuilt = _load_operation_index(root)
    return operation_id in operation_ids


def _load_operation_index(root: Path) -> tuple[set[str], bool]:
    root = root.expanduser().resolve()
    with _OPERATION_INDEX_LOCK:
        if root in _OPERATION_INDEX_CACHE and root not in _OPERATION_INDEX_FORCE_REBUILD:
            return _OPERATION_INDEX_CACHE[root], False
        force_rebuild = root in _OPERATION_INDEX_FORCE_REBUILD
        _OPERATION_INDEX_FORCE_REBUILD.discard(root)
        manifest = root / _OPERATION_INDEX_PATH
        operation_ids = None if force_rebuild else _read_operation_index(manifest)
        rebuilt = operation_ids is None
        if rebuilt:
            operation_ids = _scan_operation_ids(root)
            _write_operation_index(root, operation_ids)
        _OPERATION_INDEX_CACHE[root] = operation_ids
        return operation_ids, rebuilt


def _read_operation_index(path: Path) -> set[str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != _OPERATION_INDEX_SCHEMA_VERSION:
        return None
    values = payload.get("operation_ids")
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        return None
    return set(values)


def _scan_operation_ids(root: Path) -> set[str]:
    candidates = [root / "wiki" / "log.md"]
    archive_dir = root / ARCHIVES_LOG_DIR
    if archive_dir.exists():
        candidates.extend(archive_dir.rglob("*.md"))
    operation_ids: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        operation_ids.update(match.group(1) for line in lines if (match := _OPERATION_ID_RE.match(line)))
    return operation_ids


def _write_operation_index(
    root: Path,
    operation_ids: set[str],
    *,
    fault: FaultBarrier | None = None,
) -> None:
    path = root / _OPERATION_INDEX_PATH
    payload = {
        "schema_version": _OPERATION_INDEX_SCHEMA_VERSION,
        "operation_ids": sorted(operation_ids),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", fault=fault)
    with _OPERATION_INDEX_LOCK:
        _OPERATION_INDEX_CACHE[root.expanduser().resolve()] = operation_ids
        _OPERATION_INDEX_FORCE_REBUILD.discard(root.expanduser().resolve())


def _force_operation_index_rebuild(root: Path) -> None:
    with _OPERATION_INDEX_LOCK:
        root = root.expanduser().resolve()
        _OPERATION_INDEX_CACHE.pop(root, None)
        _OPERATION_INDEX_FORCE_REBUILD.add(root)


def _render_archive_detail(block: str) -> str:
    return render_units(
        _ARCHIVE_HEADER,
        [block],
    )


def _indented_items(items: list[str], *, field: str) -> list[str]:
    if not items:
        return ["  - none"]
    if field in {"path", "source"}:
        return [f"  - {_safe_log_locator(item)}" for item in items]
    return [f"  - {redact_sensitive_text(item)}" for item in items]


def _safe_log_locator(value: str) -> str:
    try:
        return normalize_vault_relative(value)
    except (LocatorError, TypeError):
        return "[UNSAFE_LOCATOR]"


def _read_log_blocks(log_path: Path, default_preamble: str) -> tuple[str, list[str]]:
    if not log_path.exists():
        return default_preamble, []
    preamble, blocks = _split_log_blocks(log_path.read_text(encoding="utf-8"))
    return preamble or default_preamble, blocks


def _rotate_blocks(preamble: str, blocks: list[str]) -> tuple[list[str], list[str]]:
    """Move only oldest whole blocks until both active limits are satisfied."""

    if len(blocks) <= MAX_LOG_ENTRIES and utf8_size(_join_log_blocks(preamble, blocks)) <= _archive_target_bytes():
        return blocks, []

    if len(blocks) > MAX_LOG_ENTRIES:
        keep = blocks[-TARGET_LOG_ENTRIES:]
        overflow = blocks[:-TARGET_LOG_ENTRIES]
    else:
        keep = list(blocks)
        overflow = []
    while keep and utf8_size(_join_log_blocks(preamble, keep)) > _archive_target_bytes():
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


def _write_archived_log_blocks(
    root: Path,
    source_log_name: str,
    blocks: list[str],
    *,
    fault: FaultBarrier | None = None,
) -> list[Path]:
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
                fault=fault,
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
    fault: FaultBarrier | None = None,
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
    _atomic_write_many(contents, fault=fault)
    return paths


def _write_archive_text(root: Path, timestamp: str, text: str, prefix: str) -> Path:
    """Write a legacy pre-rendered archive text, preserving its old API."""

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


def _append_archive_log(root: Path, archive_path: Path, *, fault: FaultBarrier | None = None) -> None:
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
        _write_archived_log_blocks(root, "archives-log", overflow, fault=fault)
    _atomic_write(archive_log, _join_log_blocks(preamble, keep), fault=fault)


def _write_archive_index(root: Path, *, fault: FaultBarrier | None = None) -> None:
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
    _atomic_write_many(contents, fault=fault)
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


def _atomic_write_many(
    contents: dict[Path, str],
    *,
    fault: FaultBarrier | None = None,
) -> None:
    """Replace each log file atomically; this is not a cross-file transaction."""

    for target, text in contents.items():
        atomic_write_text(target, text, fault=fault)


def _atomic_write(path: Path, text: str, *, fault: FaultBarrier | None = None) -> None:
    _atomic_write_many({path: text}, fault=fault)


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

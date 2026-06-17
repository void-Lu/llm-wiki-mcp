from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.redaction import redact_sensitive_text
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry

_LOG_HEADING_RE = re.compile(r"^## \[([^\]]+)\] (\S+) \| (.+)$")


def append_log_entry(vault_root: str | Path, entry: WikiLogEntry) -> dict[str, object]:
    root = Path(vault_root)
    log_path = root / "wiki" / "log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text("# Log\n\n", encoding="utf-8")

    timestamp = entry.timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    operation = redact_sensitive_text(entry.operation)
    title = redact_sensitive_text(entry.title)
    project = redact_sensitive_text(entry.project)
    status = redact_sensitive_text(entry.status)
    lines = [
        f"## [{timestamp}] {operation} | {title}",
        f"- project: {project}",
        f"- status: {status}",
        "- paths:",
        *_indented_items(entry.paths),
        "- sources:",
        *_indented_items(entry.sources),
        "",
    ]
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    _enforce_log_limit(root, log_path, "wiki-log", record_archive=True)
    return {"ok": True, "path": "wiki/log.md"}


def read_recent_log_entries(vault_root: str | Path, limit: int = 5) -> list[str]:
    log_path = Path(vault_root) / "wiki" / "log.md"
    if not log_path.exists():
        return []
    headings = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.startswith("## [")]
    return list(reversed(headings[-limit:]))


def parse_log_entries(vault_root: str | Path, limit: int = 10) -> list[dict[str, Any]]:
    log_path = Path(vault_root) / "wiki" / "log.md"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_field: str = ""
    for line in lines:
        match = _LOG_HEADING_RE.match(line)
        if match:
            if current is not None:
                entries.append(current)
            current = {
                "timestamp": match.group(1),
                "operation": match.group(2),
                "title": match.group(3),
                "project": "",
                "status": "",
                "paths": [],
                "sources": [],
            }
            current_field = ""
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("- project:"):
            current["project"] = stripped[len("- project:"):].strip()
            current_field = ""
        elif stripped.startswith("- status:"):
            current["status"] = stripped[len("- status:"):].strip()
            current_field = ""
        elif stripped == "- paths:":
            current_field = "paths"
        elif stripped == "- sources:":
            current_field = "sources"
        elif stripped.startswith("- ") and current_field:
            value = stripped[2:].strip()
            if value != "none":
                current[current_field].append(value)
    if current is not None:
        entries.append(current)
    return list(reversed(entries[-limit:]))


def _indented_items(items: list[str]) -> list[str]:
    if not items:
        return ["  - none"]
    return [f"  - {redact_sensitive_text(item)}" for item in items]


def _enforce_log_limit(root: Path, log_path: Path, source_log_name: str, record_archive: bool) -> None:
    """Archive oldest entries when the log exceeds *max_entries*.

    When the log exceeds 200 entries the oldest 100 are moved to an
    archive file so each archive batch is a meaningful chunk rather than
    a single-entry fragment.
    """
    max_entries = 200
    keep_after_archive = 100
    text = log_path.read_text(encoding="utf-8")
    preamble, blocks = _split_log_blocks(text)
    if len(blocks) <= max_entries:
        return
    overflow = blocks[: len(blocks) - keep_after_archive]
    keep = blocks[len(blocks) - keep_after_archive :]
    log_path.write_text(_join_log_blocks(preamble, keep), encoding="utf-8")
    archive_path = _write_archived_log_blocks(root, source_log_name, overflow)
    if record_archive:
        _append_archive_log(root, archive_path)


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
    parts = [preamble] if preamble else []
    parts.extend(blocks)
    return "\n\n".join(part for part in parts if part).rstrip() + "\n\n"


def _write_archived_log_blocks(root: Path, source_log_name: str, blocks: list[str]) -> Path:
    year, month, day = _archive_date_parts(blocks)
    archive_dir = root / "wiki" / "archives" / year / month / day / "log"
    archive_dir.mkdir(parents=True, exist_ok=True)
    sequence = len(list(archive_dir.glob(f"{source_log_name}-*.md"))) + 1
    archive_path = archive_dir / f"{source_log_name}-{sequence:03d}.md"
    archive_path.write_text("---\narchived: true\ntags:\n- archived\n---\n\n" + "\n\n".join(blocks).rstrip() + "\n", encoding="utf-8")
    return archive_path


def _archive_date_parts(blocks: list[str]) -> tuple[str, str, str]:
    for block in blocks:
        first_line = block.splitlines()[0] if block.splitlines() else ""
        match = _LOG_HEADING_RE.match(first_line)
        if match:
            date_part = match.group(1)[:10]
            if re.match(r"\d{4}-\d{2}-\d{2}", date_part):
                year, month, day = date_part.split("-")
                return year, month, day
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}"


def _append_archive_log(root: Path, archive_path: Path) -> None:
    archive_log = root / "wiki" / "archives" / "log.md"
    archive_log.parent.mkdir(parents=True, exist_ok=True)
    if not archive_log.exists():
        archive_log.write_text("# Archives Log\n\n", encoding="utf-8")
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rel = archive_path.relative_to(root).as_posix()
    lines = [
        f"## [{timestamp}] archive_log | Archived wiki log entries",
        "- project: ",
        "- status: ok",
        "- paths:",
        f"  - {rel}",
        "- sources:",
        "  - wiki/log.md",
        "",
    ]
    with archive_log.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    _enforce_log_limit(root, archive_log, "archives-log", record_archive=False)

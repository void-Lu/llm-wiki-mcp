from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.redaction import redact_sensitive_text
from netsuite_rag_mcp.wiki_models import WikiLogEntry

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

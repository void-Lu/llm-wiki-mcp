from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from netsuite_rag_mcp.redaction import redact_sensitive_text
from netsuite_rag_mcp.wiki_models import WikiLogEntry


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


def _indented_items(items: list[str]) -> list[str]:
    if not items:
        return ["  - none"]
    return [f"  - {redact_sensitive_text(item)}" for item in items]

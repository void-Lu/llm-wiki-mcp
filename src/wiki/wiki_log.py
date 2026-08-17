from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Protocol

from common.redaction import redact_sensitive_text
from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki import log_volume
from wiki.atomic_file import atomic_write_text
from wiki.wiki_limits import utf8_size
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_paths import ARCHIVES_LOG_DIR, LOG_OPERATION_INDEX
from wiki.wikilinks import format_wikilink

_OPERATION_INDEX_SCHEMA_VERSION = 1
_OPERATION_ID_RE = re.compile(r"^- operation_id:\s*(\S+)\s*$")


class _OperationJournal(Protocol):
    def is_stage_succeeded(self, operation_id: str, stage: str) -> bool: ...


class WikiLogStore:
    """Hold one vault's log index state and optional operation journal seam."""

    def __init__(
        self,
        vault_root: str | Path,
        *,
        operation_store: _OperationJournal | None = None,
    ) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.operation_store = operation_store
        self._operation_index_cache: set[str] | None = None
        self._operation_index_force_rebuild = False
        self._operation_index_lock = RLock()

    def is_operation_logged(self, operation_id: str) -> tuple[bool, bool]:
        """Check journal audit state first, then the historical manifest."""

        if self.operation_store is not None and self.operation_store.is_stage_succeeded(operation_id, "audit_log"):
            return True, False
        operation_ids, rebuilt = self.load_operation_index()
        return operation_id in operation_ids, rebuilt

    def load_operation_index(self) -> tuple[set[str], bool]:
        with self._operation_index_lock:
            if self._operation_index_cache is not None and not self._operation_index_force_rebuild:
                return self._operation_index_cache, False
            force_rebuild = self._operation_index_force_rebuild
            self._operation_index_force_rebuild = False
            operation_ids = None if force_rebuild else _read_operation_index(self.root / LOG_OPERATION_INDEX)
            rebuilt = operation_ids is None
            if rebuilt:
                self._operation_index_force_rebuild = True
                operation_ids = _scan_operation_ids(self.root)
                self.write_operation_index(operation_ids)
            self._operation_index_cache = operation_ids
            return operation_ids, rebuilt

    def write_operation_index(self, operation_ids: set[str]) -> None:
        payload = {
            "schema_version": _OPERATION_INDEX_SCHEMA_VERSION,
            "operation_ids": sorted(operation_ids),
        }
        atomic_write_text(self.root / LOG_OPERATION_INDEX, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        with self._operation_index_lock:
            self._operation_index_cache = set(operation_ids)
            self._operation_index_force_rebuild = False

    def force_operation_index_rebuild(self) -> None:
        with self._operation_index_lock:
            self._operation_index_cache = None
            self._operation_index_force_rebuild = True


def append_log_entry(
    vault_root: str | Path,
    entry: WikiLogEntry,
    *,
    operation_store: _OperationJournal | None = None,
    log_store: WikiLogStore | None = None,
) -> dict[str, object]:
    """Append one complete log record while keeping every generated log bounded."""

    root = Path(vault_root).expanduser().resolve()
    state = log_store or WikiLogStore(root, operation_store=operation_store)
    if state.root != root:
        raise ValueError("log store root does not match vault root")
    if operation_store is not None and state.operation_store is None:
        state.operation_store = operation_store
    operation_index_rebuilt = False
    if entry.operation_id:
        logged, operation_index_rebuilt = state.is_operation_logged(entry.operation_id)
        if logged:
            result: dict[str, object] = {"ok": True, "path": "wiki/log.md", "deduplicated": True}
            if operation_index_rebuilt:
                result["operation_index_rebuilt"] = True
            return result
    timestamp = entry.timestamp or _now()
    block = _render_log_entry(entry, timestamp)

    try:
        outcome = log_volume.publish(
            root,
            timestamp,
            block,
            render_summary=lambda archive_rel: _render_archive_summary(entry, timestamp, archive_rel),
        )
        if not outcome.keep_ok:
            return {
                "ok": False,
                "code": "log_entry_too_large",
                "error": "log entry cannot fit into a bounded archive page",
                "path": "wiki/log.md",
            }
        if entry.operation_id:
            operation_ids, _ = state.load_operation_index()
            state.write_operation_index({*operation_ids, entry.operation_id})
    except Exception:
        if entry.operation_id:
            state.force_operation_index_rebuild()
        raise
    return {
        "ok": True,
        "path": "wiki/log.md",
        "archived": outcome.archived_rel_paths,
        **({"operation_index_rebuilt": True} if operation_index_rebuilt else {}),
    }


def read_recent_log_entries(vault_root: str | Path, limit: int = 5) -> list[str]:
    log_path = Path(vault_root) / "wiki" / "log.md"
    _preamble, blocks = log_volume.read_blocks(log_path, "# Log")
    headings = [block.splitlines()[0] for block in blocks if block.splitlines()]
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


def _operation_logged(
    root: Path,
    operation_id: str,
    *,
    operation_store: _OperationJournal | None = None,
    log_store: WikiLogStore | None = None,
) -> bool:
    """Check the vault-local operation index without scanning archive volumes."""

    state = log_store or WikiLogStore(root, operation_store=operation_store)
    logged, _rebuilt = state.is_operation_logged(operation_id)
    return logged


def _load_operation_index(
    root: Path,
    *,
    log_store: WikiLogStore | None = None,
) -> tuple[set[str], bool]:
    state = log_store or WikiLogStore(root)
    return state.load_operation_index()


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
    log_store: WikiLogStore | None = None,
) -> None:
    state = log_store or WikiLogStore(root)
    state.write_operation_index(operation_ids)


def _force_operation_index_rebuild(root: Path, *, log_store: WikiLogStore | None = None) -> None:
    state = log_store or WikiLogStore(root)
    state.force_operation_index_rebuild()


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

from __future__ import annotations

import json
import shutil
from importlib import metadata
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_paths import DEFAULT_FILES, TOP_LEVEL_DIRS

DEFAULT_MAX_FILES = 2_000
HARD_MAX_FILES = 10_000
DEFAULT_MAX_BYTES = 120_000
HARD_MAX_BYTES = 2 * 1024 * 1024

_TEXT_EXTENSIONS = {
    ".cfg",
    ".csv",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".py",
    ".sql",
    ".ts",
    ".tsx",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def wiki_status(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    missing = _missing_required_paths(root)
    return {
        "ok": True,
        "vault_root": str(root),
        "initialized": root.exists() and not missing,
        "missing_required_paths": missing,
        "queue": _queue_status(root),
        "codegraph": _codegraph_status(),
        "version": _package_version(),
    }


def wiki_list_files(
    vault_root: str | Path,
    root_name: str = "wiki",
    recursive: bool = True,
    max_files: int | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    roots = _public_roots(root, root_name)
    if roots is None:
        return {"ok": False, "code": "invalid_root", "error": "root_name must be 'wiki', 'sources', or 'all'"}

    limit = _clamp_limit(max_files, DEFAULT_MAX_FILES, HARD_MAX_FILES)
    files: list[dict[str, Any]] = []
    truncated = False
    for public_root in roots:
        if not public_root.exists():
            continue
        paths = sorted(public_root.rglob("*") if recursive else public_root.iterdir())
        for path in paths:
            if len(files) >= limit:
                truncated = True
                break
            files.append(_file_item(root, path))
        if truncated:
            break
    return {"ok": True, "root": root_name, "files": files, "truncated": truncated, "max_files": limit}


def wiki_read_file(
    vault_root: str | Path,
    path: str,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    resolved = _resolve_public_text_path(root, path)
    if not resolved.get("ok"):
        return resolved

    target: Path = resolved["absolute_path"]
    if not target.is_file():
        return {"ok": False, "code": "file_not_found", "error": f"file not found: {path}"}

    limit = _clamp_limit(max_bytes, DEFAULT_MAX_BYTES, HARD_MAX_BYTES)
    data = target.read_bytes()
    try:
        full_content = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "code": "decode_error", "error": f"file is not valid UTF-8 text: {path}"}
    content, used_bytes = _truncate_utf8(full_content, limit)
    omitted = max(0, len(data) - used_bytes)
    return {
        "ok": True,
        "path": target.relative_to(root).as_posix(),
        "content": content,
        "size_bytes": len(data),
        "truncated": omitted > 0,
        "omitted_bytes": omitted,
    }


def _missing_required_paths(root: Path) -> list[str]:
    required = list(TOP_LEVEL_DIRS) + list(DEFAULT_FILES)
    return [relative.as_posix() for relative in required if not (root / relative).exists()]


def _queue_status(root: Path) -> dict[str, Any]:
    queue_path = root / ".llm-wiki" / "ingest-queue.json"
    if not queue_path.exists():
        return {"path": queue_path.relative_to(root).as_posix(), "total": 0, "counts": {}}
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": queue_path.relative_to(root).as_posix(), "total": 0, "counts": {}, "error": str(exc)}
    queue = data if isinstance(data, list) else []
    counts: dict[str, int] = {}
    for item in queue:
        status = str(item.get("status", "unknown")) if isinstance(item, dict) else "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {"path": queue_path.relative_to(root).as_posix(), "total": len(queue), "counts": counts}


def _codegraph_status() -> dict[str, Any]:
    executable = shutil.which("codegraph") or shutil.which("codegraph.cmd")
    return {"available": executable is not None, "executable": executable or ""}


def _package_version() -> str:
    try:
        return metadata.version("netsuite-llm-wiki-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


def _public_roots(root: Path, root_name: str) -> list[Path] | None:
    if root_name == "wiki":
        return [root / "wiki"]
    if root_name == "sources":
        return [root / "raw" / "sources"]
    if root_name == "all":
        return [root / "wiki", root / "raw" / "sources"]
    return None


def _file_item(root: Path, path: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    item: dict[str, Any] = {"path": rel, "is_dir": path.is_dir()}
    if path.is_file():
        try:
            item["size_bytes"] = path.stat().st_size
        except OSError:
            item["size_bytes"] = 0
    return item


def _resolve_public_text_path(root: Path, value: str) -> dict[str, Any]:
    relative = Path(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return {"ok": False, "code": "path_escape", "error": "path must be relative and stay inside the vault"}

    normalized = Path(*relative.parts)
    if not (_starts_with(normalized, Path("wiki")) or _starts_with(normalized, Path("raw/sources"))):
        return {"ok": False, "code": "path_not_allowed", "error": "path must be under wiki/ or raw/sources/"}
    if normalized.suffix.lower() not in _TEXT_EXTENSIONS:
        return {"ok": False, "code": "unsupported_file_type", "error": "only text-like files can be read"}

    target = (root / normalized).resolve()
    if not target.is_relative_to(root):
        return {"ok": False, "code": "path_escape", "error": "resolved path escapes wiki root"}
    return {"ok": True, "absolute_path": target}


def _starts_with(path: Path, prefix: Path) -> bool:
    return path == prefix or path.is_relative_to(prefix)


def _clamp_limit(value: int | None, default: int, hard_max: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), hard_max))


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, int]:
    used = 0
    chars: list[str] = []
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > max_bytes:
            break
        chars.append(char)
        used += size
    return "".join(chars), used

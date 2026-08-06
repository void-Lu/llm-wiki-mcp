"""Build the optional related-page section for Wiki page bodies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from wiki.wiki_paths import WikiPathError, safe_segment
from wiki.wikilinks import format_wikilink, iter_wikilinks

_WIKI_PREFIX = "wiki/"
_RAW_SOURCES_PREFIX = "raw/sources/"
_REFERENCE_HEADING = "## 参考来源"


def build_reference_section(
    root: str | Path,
    body: str,
    related_pages: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, list[dict[str, str]]]:
    """Append a deduplicated ``参考来源`` section to *body*.

    Only existing Markdown files below ``wiki/`` are eligible.  Invalid input
    is reported in the returned skip list so callers can keep the main write
    operation successful.
    """

    if related_pages is None:
        return body, []

    vault_root = Path(root).expanduser().resolve()
    skipped: list[dict[str, str]] = []
    existing_targets = {_target_key(token.target) for token in iter_wikilinks(body)}
    seen_targets = set(existing_targets)
    links: list[str] = []

    for item in related_pages:
        if not isinstance(item, Mapping):
            skipped.append({"path": str(item), "reason": "invalid_entry"})
            continue
        raw_path = item.get("path")
        path_value = raw_path if isinstance(raw_path, str) else str(raw_path or "")
        normalized, reason = _validated_relative_path(
            vault_root,
            path_value,
            prefix=_WIKI_PREFIX,
            require_markdown=True,
        )
        if normalized is None:
            if reason == "path_not_allowed" and _normalize_path(path_value).startswith(_RAW_SOURCES_PREFIX):
                reason = "raw_source_use_sources"
            skipped.append({"path": path_value, "reason": reason or "invalid_path"})
            continue

        target = normalized[:-3]
        target_key = _target_key(target)
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)

        title = item.get("title")
        label = str(title) if title is not None and str(title) else Path(normalized).stem
        links.append(format_wikilink(target, label))

    if not links:
        return body, skipped

    section = f"{_REFERENCE_HEADING}\n\n" + "\n".join(f"- {link}" for link in links)
    if not body:
        return section, skipped
    separator = "" if body.endswith("\n\n") else "\n" if body.endswith("\n") else "\n\n"
    return f"{body}{separator}{section}", skipped


def validate_raw_sources(
    root: str | Path,
    sources: Iterable[object] | object | None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Return existing vault-relative ``raw/sources`` files and skipped items."""

    if sources is None:
        return [], []
    values = [sources] if isinstance(sources, (str, Path)) else sources
    if not isinstance(values, Iterable):
        values = [values]

    vault_root = Path(root).expanduser().resolve()
    valid: list[str] = []
    skipped: list[dict[str, str]] = []
    for item in values:
        path_value = item if isinstance(item, str) else str(item or "")
        normalized, reason = _validated_relative_path(
            vault_root,
            path_value,
            prefix=_RAW_SOURCES_PREFIX,
            require_markdown=False,
        )
        if normalized is None:
            skipped.append({"path": path_value, "reason": reason or "invalid_path"})
            continue
        valid.append(normalized)
    return valid, skipped


def skipped_warnings(field: str, skipped: Sequence[Mapping[str, str]]) -> list[str]:
    """Format skipped-input details using the server's warning-list convention."""

    return [f"{field} skipped: {item.get('path', '')} ({item.get('reason', 'invalid_path')})" for item in skipped]


def _validated_relative_path(
    root: Path,
    value: str,
    *,
    prefix: str,
    require_markdown: bool,
) -> tuple[str | None, str | None]:
    normalized = _normalize_path(value)
    if _is_absolute_or_traversal(normalized):
        return None, "path_escape"
    if not normalized.startswith(prefix):
        return None, "path_not_allowed"

    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return None, "path_escape"
    try:
        for part in parts:
            safe_segment(part)
    except WikiPathError as exc:
        return None, exc.code

    relative = Path(*parts)
    if require_markdown and relative.suffix != ".md":
        return None, "not_markdown"

    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        return None, "path_escape"
    if not target.is_file():
        return None, "not_found"
    return normalized, None


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/")


def _is_absolute_or_traversal(value: str) -> bool:
    return value.startswith("/") or (len(value) >= 2 and value[1] == ":") or any(part in {".", ".."} for part in value.split("/"))


def _target_key(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.casefold().endswith(".md"):
        normalized = normalized[:-3]
    return normalized.casefold()

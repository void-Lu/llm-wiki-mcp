"""Build the optional related-page section for Wiki page bodies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki.source_provenance import SourceProvenanceError, SourceProvenanceResolver
from wiki.wiki_paths import WikiPathError, resolve_within_root, translate_path_error, validate_wiki_page_path
from wiki.wikilinks import format_wikilink, iter_wikilinks

_REFERENCE_HEADING = "## 参考来源"


def build_reference_section(
    root: str | Path,
    body: str,
    related_pages: Sequence[Mapping[str, Any]] | None,
    heading: str | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Append a deduplicated reference section to *body*.

    Only existing Markdown files below ``wiki/`` are eligible.  Invalid input
    is reported in the returned skip list so callers can keep the main write
    operation successful.

    *heading* overrides the default ``## 参考来源`` section title.
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
        normalized_path = path_value.replace("\\", "/")
        normalized, reason = _validated_wiki_page_path(vault_root, path_value)
        if normalized is None:
            if normalized_path.startswith("raw/sources/"):
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

    section_heading = heading.strip() if heading and heading.strip() else _REFERENCE_HEADING
    section = f"{section_heading}\n\n" + "\n".join(f"- {link}" for link in links)
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
        normalized, reason = _validated_raw_source_path(vault_root, path_value)
        if normalized is None:
            skipped.append({"path": path_value, "reason": reason or "invalid_path"})
            continue
        valid.append(normalized)
    return valid, skipped


def skipped_warnings(field: str, skipped: Sequence[Mapping[str, str]]) -> list[str]:
    """Format skipped-input details using the server's warning-list convention."""

    values: list[str] = []
    for item in skipped:
        raw_path = str(item.get("path", ""))
        try:
            safe_path = normalize_vault_relative(raw_path)
        except LocatorError:
            safe_path = "[UNSAFE_LOCATOR]"
        values.append(f"{field} skipped: {safe_path} ({item.get('reason', 'invalid_path')})")
    return values


def _validated_wiki_page_path(root: Path, value: str) -> tuple[str | None, str | None]:
    normalized = value.replace("\\", "/")
    if not normalized.casefold().endswith(".md"):
        return None, "not_markdown"
    try:
        relative = validate_wiki_page_path(normalized, allow_navigation_index=False)
        target = resolve_within_root(root, relative)
    except WikiPathError as exc:
        return None, translate_path_error(exc.code, "reference")
    if not target.is_file():
        return None, "not_found"
    return relative.as_posix(), None


def _validated_raw_source_path(root: Path, value: str) -> tuple[str | None, str | None]:
    try:
        resolved = SourceProvenanceResolver(root).resolve(value)
    except SourceProvenanceError as exc:
        return None, {
            "source_not_found": "not_found",
            "source_not_file": "not_found",
            "source_path_not_allowed": "path_not_allowed",
        }.get(exc.code, exc.code)
    return resolved.relative_path, None


def _target_key(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.casefold().endswith(".md"):
        normalized = normalized[:-3]
    return normalized.casefold()

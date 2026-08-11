from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codegraph.codegraph_policy import is_codegraph_managed_path
from common.redaction import count_redactions
from common.privacy_policy import LocatorError, PrivacyPolicy
from wiki.atomic_file import AtomicFileError, atomic_write_text
from wiki.wiki_models import WikiPage
from wiki.wiki_paths import safe_segment


class WikiWriteError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedWikiPage:
    target: Path
    relative_path: Path
    text: str
    title: str
    frontmatter: dict[str, Any]
    redacted_count: int


_ALLOWED_PREFIXES = (
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/entities"),
)

_FORBIDDEN_PARTS = {"objects"}
_FORBIDDEN_PREFIXES = (
    Path("wiki/code"),
    Path("wiki/decisions"),
    Path("wiki/troubleshooting"),
    Path("wiki/requirements"),
    Path("wiki/knowledge"),
    Path("wiki/synthesis"),
    Path("wiki/comparisons"),
    Path("wiki/maintenance"),
    Path("projects"),
)

_PROJECT_SUBDIRS = {
    "specs",
    "plans",
    "architecture",
    "pipelines",
    "troubleshooting",
    "researches",
}

_RESERVED_STRUCTURE_PARTS = {
    "wiki",
    "projects",
    "concepts",
    "entities",
    "archives",
    *_PROJECT_SUBDIRS,
}


def read_markdown_page(path: str | Path, vault_root: str | Path | None = None) -> WikiPage:
    file_path = Path(path).resolve()
    text = file_path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    title = _extract_title(body) or str(frontmatter.get("title", file_path.stem))
    body_without_title = _remove_first_heading(body).strip()
    relative_path = file_path.relative_to(Path(vault_root).expanduser().resolve()) if vault_root is not None else Path(file_path.name)
    return WikiPage(
        relative_path=relative_path,
        frontmatter=frontmatter,
        title=title,
        body=body_without_title,
    )


def write_wiki_page(
    vault_root: str | Path,
    page: WikiPage,
    overwrite_generated_only: bool = True,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    prepared = prepare_wiki_page(root, page, overwrite_generated_only=overwrite_generated_only)
    try:
        written = atomic_write_text(prepared.target, prepared.text)
    except AtomicFileError as exc:
        raise WikiWriteError(exc.code, "page could not be written") from exc

    result: dict[str, Any] = {
        "ok": True,
        "path": prepared.relative_path.as_posix(),
        "page_hash": written.content_hash,
        "redacted_count": prepared.redacted_count,
    }
    try:
        result["retrieval_index"] = refresh_page_retrieval(root, prepared.target)
    except Exception as exc:
        result["retrieval_index"] = {"ok": False, "state": "stale", "code": "index_update_failed", "error": str(exc)}
    return result


def prepare_wiki_page(
    vault_root: str | Path,
    page: WikiPage,
    *,
    overwrite_generated_only: bool = True,
) -> PreparedWikiPage:
    """Validate and render a page without changing any durable state."""

    root = Path(vault_root).expanduser().resolve()
    relative_path = _validate_relative_path(Path(page.relative_path))
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise WikiWriteError("path_escape", "resolved page path escapes wiki root")
    if target.exists() and overwrite_generated_only:
        existing_frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8"))
        if is_codegraph_managed_path(relative_path, existing_frontmatter):
            raise WikiWriteError("codegraph_managed_page", f"CodeGraph-managed page is tool-owned: {relative_path.as_posix()}")
        if existing_frontmatter.get("generated") is not True:
            raise WikiWriteError("manual_page_exists", f"refusing to overwrite non-generated wiki page: {relative_path.as_posix()}")
    elif target.exists():
        existing_frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8"))
        if is_codegraph_managed_path(relative_path, existing_frontmatter):
            raise WikiWriteError("codegraph_managed_page", f"CodeGraph-managed page is tool-owned: {relative_path.as_posix()}")

    policy = PrivacyPolicy()
    title = policy.redact_display_text(page.title)
    body = policy.redact_display_text(page.body)
    try:
        projected_frontmatter = policy.redact_metadata(dict(page.frontmatter))
    except LocatorError as exc:
        raise WikiWriteError(exc.code, "frontmatter contains an unsafe locator") from exc
    frontmatter = projected_frontmatter if isinstance(projected_frontmatter, dict) else {}
    removed_fields = sorted({key for key in ("source_capsules", "source_capsule") if key in frontmatter})
    if removed_fields:
        raise WikiWriteError("source_capsules_removed", "source capsule provenance fields are retired; use raw sources instead")
    frontmatter.setdefault("title", title)
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    text = f"---\n{yaml_text}\n---\n\n# {title}\n\n{strip_leading_h1(body).strip()}\n"
    original_text = f"{page.title}\n{page.frontmatter}\n{page.body}"
    redacted_text = f"{title}\n{frontmatter}\n{body}"
    return PreparedWikiPage(
        target=target,
        relative_path=relative_path,
        text=text,
        title=title,
        frontmatter=frontmatter,
        redacted_count=count_redactions(original_text, redacted_text),
    )


def refresh_page_retrieval(vault_root: str | Path, target: str | Path) -> dict[str, object]:
    """Refresh one existing active-page retrieval record without building a store."""

    root = Path(vault_root).expanduser().resolve()
    page_path = Path(target).resolve()
    from retrieval.retrieval_index import RetrievalIndexStore, page_from_file

    store = RetrievalIndexStore(root)
    indexed = page_from_file(root, page_path, scope="active")
    if indexed is None or not store.path.exists():
        return {"ok": True, "state": "not_indexed"}
    return store.update_page(indexed)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    cleaned = text.lstrip("﻿")
    lines = cleaned.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except (StopIteration, yaml.YAMLError):
        return {}, "\n".join(lines[1:]).strip()
    frontmatter = loaded if isinstance(loaded, dict) else {}
    return frontmatter, "\n".join(lines[end + 1 :]).strip()


def strip_leading_h1(body: str) -> str:
    # Drop a leading markdown H1 so the writer can inject the canonical title
    # heading from frontmatter without producing a duplicate. Only the first
    # non-blank line is considered (must start with "# "); content headings are
    # preserved. Returns the body unchanged when no leading H1 is present.
    lines = body.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or not lines[index].startswith("# "):
        return body
    rest = lines[index + 1 :]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(rest)


def _validate_relative_path(path: Path) -> Path:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WikiWriteError("path_escape", "page path must stay inside wiki root")
    normalized = Path(*path.parts)
    if normalized.suffix.lower() != ".md":
        raise WikiWriteError("invalid_wiki_path", "wiki page must be a markdown file")
    if any(_starts_with(normalized, prefix) for prefix in _FORBIDDEN_PREFIXES):
        raise WikiWriteError("invalid_wiki_path", f"page path is outside the confirmed wiki structure: {normalized.as_posix()}")
    if any(part.casefold() in _FORBIDDEN_PARTS for part in normalized.parts):
        raise WikiWriteError("invalid_wiki_path", f"objects directories are not part of the confirmed wiki structure: {normalized.as_posix()}")
    if not any(_starts_with(normalized, prefix) for prefix in _ALLOWED_PREFIXES):
        raise WikiWriteError("invalid_wiki_path", f"page path is outside the confirmed wiki structure: {normalized.as_posix()}")
    if _starts_with(normalized, Path("wiki/projects")) and not _is_valid_project_path(normalized):
        raise WikiWriteError("invalid_wiki_path", f"project page path is outside the confirmed project structure: {normalized.as_posix()}")
    for part in normalized.parts:
        if part in _RESERVED_STRUCTURE_PARTS:
            continue
        stem = Path(part).stem if part.endswith(".md") else part
        try:
            safe_segment(stem)
        except ValueError as exc:
            code = getattr(exc, "code", "invalid_path_component")
            raise WikiWriteError(code, str(exc)) from exc
    return normalized


def _is_valid_project_path(path: Path) -> bool:
    parts = path.parts
    if len(parts) == 4 and parts[3] == "index.md":
        return True
    return len(parts) >= 5 and parts[3] in _PROJECT_SUBDIRS


def _starts_with(path: Path, prefix: Path) -> bool:
    return path.parts[: len(prefix.parts)] == prefix.parts


def _extract_title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _remove_first_heading(body: str) -> str:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join(lines[index + 1 :])
    return body

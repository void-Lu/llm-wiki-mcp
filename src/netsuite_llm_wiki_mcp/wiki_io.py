from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.redaction import count_redactions, redact_sensitive_text
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import safe_segment


class WikiWriteError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_ALLOWED_PREFIXES = (
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/chatlog"),
    Path("wiki/sources"),
    Path("wiki/queries"),
    Path("wiki/comparisons"),
    Path("wiki/maintenance"),
)

_FORBIDDEN_PARTS = {"objects"}
_FORBIDDEN_PREFIXES = (
    Path("wiki/code"),
    Path("wiki/decisions"),
    Path("wiki/troubleshooting"),
    Path("wiki/requirements"),
    Path("wiki/knowledge"),
    Path("wiki/synthesis"),
    Path("projects"),
)

_PROJECT_SUBDIRS = {
    "specs",
    "plans",
    "architecture",
    "pipelines",
    "troubleshooting",
    "researches",
    "sources",
}

_RESERVED_STRUCTURE_PARTS = {
    "wiki",
    "projects",
    "concepts",
    "chatlog",
    "sources",
    "queries",
    "comparisons",
    "maintenance",
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
    relative_path = _validate_relative_path(page.relative_path)
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise WikiWriteError("path_escape", "resolved page path escapes wiki root")
    if target.exists() and overwrite_generated_only:
        existing_frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8"))
        if existing_frontmatter.get("generated") is not True:
            raise WikiWriteError("manual_page_exists", f"refusing to overwrite non-generated wiki page: {relative_path.as_posix()}")

    title = redact_sensitive_text(page.title)
    body = redact_sensitive_text(page.body)
    frontmatter = _redact_value(dict(page.frontmatter))
    frontmatter.setdefault("title", title)
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    text = f"---\n{yaml_text}\n---\n\n# {title}\n\n{body.strip()}\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    original_text = f"{page.title}\n{page.frontmatter}\n{page.body}"
    redacted_text = f"{title}\n{frontmatter}\n{body}"
    return {
        "ok": True,
        "path": relative_path.as_posix(),
        "absolute_path": str(target),
        "redacted_count": count_redactions(original_text, redacted_text),
    }


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


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


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

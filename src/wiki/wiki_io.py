from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from common.redaction import count_redactions
from common.privacy_policy import LocatorError, PrivacyPolicy
from wiki.wiki_models import WikiPage
from wiki.wiki_paths import WikiPathError, resolve_within_root, translate_path_error, validate_wiki_page_path


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


def is_manual_page(path: Path) -> bool:
    if not path.exists():
        return False
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter.get("generated") is not True


def prepare_wiki_page(
    vault_root: str | Path,
    page: WikiPage,
    *,
    overwrite_generated_only: bool = True,
    allow_navigation_index: bool = False,
) -> PreparedWikiPage:
    """Validate and render a page without changing any durable state."""

    root = Path(vault_root).expanduser().resolve()
    relative_path = _validate_relative_path(Path(page.relative_path), allow_navigation_index=allow_navigation_index)
    try:
        target = resolve_within_root(root, relative_path)
    except WikiPathError as exc:
        raise WikiWriteError(translate_path_error(exc.code, "io"), "resolved page path escapes wiki root") from exc
    if overwrite_generated_only and is_manual_page(target):
        raise WikiWriteError("manual_page_exists", f"refusing to overwrite non-generated wiki page: {relative_path.as_posix()}")

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


def render_page(frontmatter: Mapping[str, object], body: str) -> str:
    """Render frontmatter and body using the canonical Wiki page envelope."""

    yaml_text = yaml.safe_dump(dict(frontmatter), allow_unicode=True, sort_keys=False).strip()
    body_text = body.strip()
    return f"---\n{yaml_text}\n---\n\n{body_text}\n"


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


def _validate_relative_path(path: Path, *, allow_navigation_index: bool = False) -> Path:
    try:
        return validate_wiki_page_path(path, allow_navigation_index=allow_navigation_index)
    except WikiPathError as exc:
        raise WikiWriteError(translate_path_error(exc.code, "io"), str(exc)) from exc


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

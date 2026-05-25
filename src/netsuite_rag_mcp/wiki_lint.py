from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.wiki_io import split_frontmatter

_REQUIRED_FILES = (
    Path("purpose.md"),
    Path("schema.md"),
    Path("wiki/index.md"),
    Path("wiki/log.md"),
    Path("wiki/overview.md"),
)
_REQUIRED_DIRS = (
    Path("raw/sources"),
    Path("raw/assets"),
    Path("wiki/projects"),
    Path("wiki/concepts"),
    Path("wiki/sources"),
    Path("wiki/queries"),
    Path("wiki/synthesis"),
    Path("wiki/comparisons"),
)
_OLD_PATHS = (
    Path("wiki/code"),
    Path("wiki/decisions"),
    Path("wiki/troubleshooting"),
    Path("wiki/requirements"),
    Path("wiki/knowledge"),
)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def wiki_lint(vault_root: str | Path, max_page_bytes: int = 200_000) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    issues: list[dict[str, str]] = []
    for relative in _REQUIRED_FILES:
        if not (root / relative).is_file():
            issues.append(_issue("missing_required_file", f"missing required file: {relative.as_posix()}", relative))
    for relative in _REQUIRED_DIRS:
        if not (root / relative).is_dir():
            issues.append(_issue("missing_required_directory", f"missing required directory: {relative.as_posix()}", relative))
    for relative in _OLD_PATHS:
        if (root / relative).exists():
            issues.append(_issue("old_structure_present", f"old wiki structure remains: {relative.as_posix()}", relative))
    projects_root = root / "wiki" / "projects"
    if projects_root.exists():
        for objects_dir in projects_root.glob("*/objects"):
            issues.append(_issue("old_structure_present", "objects directory is not part of the LLM Wiki structure", objects_dir.relative_to(root)))
    wiki_root = root / "wiki"
    if wiki_root.exists():
        pages = sorted(wiki_root.rglob("*.md"))
        for page in pages:
            rel = page.relative_to(root)
            text = page.read_text(encoding="utf-8")
            frontmatter, body = split_frontmatter(text)
            if not frontmatter and page.name not in {"log.md"}:
                issues.append(_issue("missing_frontmatter", "page is missing YAML frontmatter", rel))
            if frontmatter.get("generated") is True and not frontmatter.get("sources") and page.name not in {"index.md", "overview.md"}:
                issues.append(_issue("generated_missing_sources", "generated page must cite sources", rel))
            if len(text.encode("utf-8")) > max_page_bytes:
                issues.append(_issue("oversized_page", "page exceeds configured size threshold", rel))
            for target in _WIKILINK_RE.findall(text):
                target_path = Path(target)
                if target_path.suffix != ".md":
                    target_path = target_path.with_suffix(".md")
                resolved = (page.parent / target_path).resolve()
                if resolved.is_relative_to(root) and not resolved.exists():
                    issues.append(_issue("broken_wikilink", f"wikilink target does not exist: {target}", rel))
        index_path = root / "wiki" / "index.md"
        if index_path.exists():
            for target in _WIKILINK_RE.findall(index_path.read_text(encoding="utf-8")):
                target_path = Path(target)
                if target_path.suffix != ".md":
                    target_path = target_path.with_suffix(".md")
                resolved = (root / "wiki" / target_path).resolve()
                if resolved.is_relative_to(root) and not resolved.exists():
                    issues.append(_issue("index_target_missing", f"index target does not exist: {target}", Path("wiki/index.md")))
    return {"ok": not issues, "issues": issues}


def _issue(code: str, message: str, path: Path) -> dict[str, str]:
    return {"code": code, "message": message, "path": path.as_posix()}

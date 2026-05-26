from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter

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
            if frontmatter.get("generated") is True or rel.as_posix().startswith("wiki/sources/"):
                source_values = frontmatter.get("sources")
                if isinstance(source_values, str):
                    source_values = [source_values]
                elif not isinstance(source_values, list):
                    source_values = []
                for source in source_values:
                    if str(source).startswith("raw/") and not (root / str(source)).is_file():
                        issues.append(_issue("source_missing", f"source path does not exist: {source}", rel))
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
        structural_pages = {"wiki/index.md", "wiki/log.md", "wiki/overview.md"}
        referenced: set[str] = set()
        by_rel = {page.relative_to(root).as_posix(): page for page in pages}
        by_stem: dict[str, list[str]] = {}
        for rel in by_rel:
            by_stem.setdefault(Path(rel).stem.casefold(), []).append(rel)
        for page in pages:
            text = page.read_text(encoding="utf-8")
            for target in _WIKILINK_RE.findall(text):
                target_path = Path(target)
                if target_path.suffix != ".md":
                    target_path = target_path.with_suffix(".md")
                for candidate in [(page.parent / target_path).resolve(), (root / "wiki" / target_path).resolve(), (root / target_path).resolve()]:
                    try:
                        rel_candidate = candidate.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if rel_candidate in by_rel:
                        referenced.add(rel_candidate)
                        break
                else:
                    stem_matches = by_stem.get(Path(target).stem.casefold(), [])
                    if len(stem_matches) == 1:
                        referenced.add(stem_matches[0])
        for rel in by_rel:
            if rel in structural_pages:
                continue
            if rel.endswith("/index.md"):
                continue
            if rel not in referenced:
                issues.append(_issue("orphan_page", f"page is not referenced by any wikilink or index: {rel}", Path(rel), severity="warning"))
    cache_root = root / ".llm-wiki" / "ingest-cache"
    if cache_root.exists():
        for cache_file in sorted(cache_root.rglob("*.json")):
            cache_rel = cache_file.relative_to(root)
            try:
                cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                issues.append(_issue("cache_invalid_json", "ingest cache file is not valid JSON", cache_rel))
                continue
            manifest = cache_data.get("manifest", [])
            if not isinstance(manifest, list):
                continue
            for item in manifest:
                if not isinstance(item, dict):
                    continue
                source_path = str(item.get("path") or "")
                if not source_path:
                    continue
                target = root / source_path
                if not target.is_file():
                    issues.append(_issue("cache_manifest_path_missing", f"cache manifest path does not exist: {source_path}", cache_rel))
                    continue
                expected_hash = str(item.get("stored_sha256") or "")
                if expected_hash:
                    actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                    if actual_hash != expected_hash:
                        issues.append(_issue("cache_manifest_hash_mismatch", f"cache manifest hash mismatch: {source_path}", cache_rel, severity="warning"))
    return {"ok": not issues, "issues": issues}


def _issue(code: str, message: str, path: Path, severity: str = "error") -> dict[str, str]:
    return {"code": code, "message": message, "path": path.as_posix(), "severity": severity}

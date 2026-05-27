from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes
from netsuite_llm_wiki_mcp.wiki_log import append_log_entry
from netsuite_llm_wiki_mcp.wiki_models import WikiLogEntry
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


def wiki_lint(
    vault_root: str | Path,
    max_page_bytes: int = 200_000,
    stage: str = "structure",
    project: str | None = None,
    semantic_review: str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    if stage == "prepare_semantic_review":
        return _prepare_semantic_review(root, project, language)
    if stage == "apply_semantic_review":
        return _apply_semantic_review(root, semantic_review, project, language)
    if stage != "structure":
        return {"ok": False, "code": "invalid_stage", "error": "stage must be 'structure', 'prepare_semantic_review', or 'apply_semantic_review'"}
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


def _prepare_semantic_review(root: Path, project: str | None, language: str) -> dict[str, Any]:
    context = _semantic_review_context(root, project)
    prompt = "\n".join([
        "You are reviewing an LLM Wiki for semantic health.",
        f"Language: {language}",
        f"Project filter: {project or ''}",
        "Look for contradictions, stale claims, missing important concepts, weak source traceability, duplicated concepts, and data gaps.",
        "Return markdown with sections: contradictions, stale claims, missing concepts, source gaps, recommended follow-up sources, and safe edits.",
        "Do not modify files. Output only the review body; no frontmatter.",
        "",
        "## Wiki Context",
        context,
    ])
    return {
        "ok": True,
        "stage": "prepare_semantic_review",
        "project": project or "",
        "prompt": prompt,
        "expected_response_schema": {"body": "markdown semantic review body only, no frontmatter"},
        "next_call": {"tool": "wiki_lint", "stage": "apply_semantic_review", "required": ["semantic_review"]},
    }


def _apply_semantic_review(root: Path, semantic_review: str | None, project: str | None, language: str) -> dict[str, Any]:
    if not semantic_review or not semantic_review.strip():
        return {"ok": False, "code": "empty_semantic_review", "error": "semantic_review content is empty"}
    today = date.today().isoformat()
    filename = f"semantic-lint-{project + '-' if project else ''}{today}.md"
    rel_path = Path("wiki") / "synthesis" / filename
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    frontmatter: dict[str, Any] = {
        "type": "synthesis",
        "title": f"Semantic Lint: {project or 'vault'}",
        "generated": True,
        "origin": "semantic-lint",
        "created": today,
        "language": language,
        "summary": "LLM semantic health review for the wiki",
    }
    if project:
        frontmatter["project"] = project
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    cleaned = _strip_thinking_blocks(semantic_review).strip()
    target.write_text(f"---\n{yaml_text}\n---\n\n# Semantic Lint: {project or 'vault'}\n\n{cleaned}\n", encoding="utf-8")
    refresh_indexes(root)
    append_log_entry(root, WikiLogEntry(operation="semantic_lint", title=f"Semantic Lint: {project or 'vault'}", paths=[rel_path.as_posix()], project=project or "", status="ok"))
    return {"ok": True, "stage": "apply_semantic_review", "path": rel_path.as_posix()}


def _semantic_review_context(root: Path, project: str | None) -> str:
    parts = []
    for rel in [Path("purpose.md"), Path("schema.md"), Path("wiki/index.md"), Path("wiki/overview.md")]:
        path = root / rel
        if path.exists():
            parts.append(f"# {rel.as_posix()}\n{path.read_text(encoding='utf-8', errors='ignore')}")
    wiki_root = root / "wiki"
    if wiki_root.exists():
        pages = sorted(path for path in wiki_root.rglob("*.md") if path.name not in {"index.md", "log.md", "overview.md"})
        if project:
            pages = [path for path in pages if _page_in_project_scope(path, root, project)]
        for path in pages[:40]:
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            parts.append(f"# {rel}\n{text[:4000]}")
    return "\n\n".join(parts)


def _page_in_project_scope(path: Path, root: Path, project: str) -> bool:
    rel = path.relative_to(root).as_posix()
    return rel.startswith(f"wiki/projects/{project}/") or rel.startswith(("wiki/concepts/", "wiki/sources/", "wiki/synthesis/", "wiki/comparisons/"))


def _strip_thinking_blocks(text: str) -> str:
    text = re.sub(r"<think(?:ing)?>\s*[\s\S]*?</think(?:ing)?>\s*", "", text)
    text = re.sub(r"<think(?:ing)?>\s*[\s\S]*$", "", text)
    return text.lstrip()


def _issue(code: str, message: str, path: Path, severity: str = "error") -> dict[str, str]:
    return {"code": code, "message": message, "path": path.as_posix(), "severity": severity}

"""Duplicate entity/concept detection and merge for wiki maintenance.

Three stages:
  1. extract_page_summaries: walk wiki pages, pull slug/title/description/tags.
  2. detect stage: return a prompt for LLM to identify duplicate groups.
  3. merge stage: given confirmed group + canonical slug, rewrite cross-refs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter


def wiki_dedup(
    vault_root: str | Path,
    stage: str = "detect",
    groups: list[dict[str, Any]] | str | None = None,
    not_duplicates: list[list[str]] | None = None,
) -> dict[str, Any]:
    """Three-stage dedup workflow.

    stage="detect": scan wiki, return LLM prompt for duplicate detection.
    stage="confirm": parse LLM response, return candidate groups for user review.
    stage="merge": apply confirmed merges (rewrite cross-references).
    """
    root = Path(vault_root).expanduser().resolve()

    if stage == "detect":
        return _detect(root)
    elif stage == "confirm":
        return _confirm(root, groups, not_duplicates or [])
    elif stage == "merge":
        return _merge(root, groups)
    else:
        return {"ok": False, "code": "invalid_stage", "error": f"stage must be detect/confirm/merge"}


def extract_page_summaries(root: Path) -> list[dict[str, Any]]:
    """Extract summaries from all wiki entity/concept/source pages."""
    summaries: list[dict[str, Any]] = []
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return summaries

    scan_dirs = [
        wiki_dir / "concepts",
        wiki_dir / "sources",
    ]
    projects_dir = wiki_dir / "projects"
    if projects_dir.exists():
        for project_dir in sorted(projects_dir.iterdir()):
            if project_dir.is_dir():
                code_dir = project_dir / "code"
                if code_dir.exists():
                    scan_dirs.append(code_dir)

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for path in sorted(scan_dir.rglob("*.md")):
            if path.name == "index.md":
                continue
            summary = _extract_one_summary(path, root)
            if summary:
                summaries.append(summary)

    return summaries


def _extract_one_summary(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        return None
    slug = path.stem
    rel = path.relative_to(root).as_posix()
    title = str(frontmatter.get("title") or slug)
    page_type = str(frontmatter.get("type") or "unknown")
    tags = frontmatter.get("tags", [])
    if not isinstance(tags, list):
        tags = [str(tags)] if tags else []
    description = str(frontmatter.get("description") or "")
    if not description:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("|"):
                description = line[:200]
                break
    return {
        "slug": slug,
        "path": rel,
        "type": page_type,
        "title": title,
        "description": description,
        "tags": [str(t) for t in tags],
    }


def _detect(root: Path) -> dict[str, Any]:
    summaries = extract_page_summaries(root)
    if len(summaries) < 2:
        return {"ok": True, "stage": "detect", "groups": [], "message": "fewer than 2 pages — nothing to deduplicate"}

    prompt = _build_detect_prompt(summaries)
    return {
        "ok": True,
        "stage": "detect",
        "page_count": len(summaries),
        "prompt": prompt,
        "instruction": "Feed this prompt to an LLM. Pass the JSON response back via stage='confirm' with groups parameter.",
    }


def _confirm(
    root: Path,
    groups: list[dict[str, Any]] | str | None,
    not_duplicates: list[list[str]],
) -> dict[str, Any]:
    parsed = _parse_groups(groups)
    if parsed is None:
        return {"ok": False, "code": "invalid_groups", "error": "groups must be a list or JSON string with {groups: [...]}"}

    summaries = extract_page_summaries(root)
    valid_slugs = {s["slug"] for s in summaries}
    not_dup_keys = {_group_key(g) for g in not_duplicates}

    confirmed: list[dict[str, Any]] = []
    for group in parsed:
        slugs = [s for s in group.get("slugs", []) if s in valid_slugs]
        if len(slugs) < 2:
            continue
        if _group_key(slugs) in not_dup_keys:
            continue
        confirmed.append({
            "slugs": slugs,
            "reason": group.get("reason", ""),
            "confidence": group.get("confidence", "low"),
        })

    return {
        "ok": True,
        "stage": "confirm",
        "groups": confirmed,
        "instruction": "Review these groups. For confirmed duplicates, call stage='merge' with the groups to merge (add 'canonical' field to each group).",
    }


def _merge(root: Path, groups: list[dict[str, Any]] | str | None) -> dict[str, Any]:
    parsed = _parse_groups(groups)
    if parsed is None:
        return {"ok": False, "code": "invalid_groups", "error": "groups must specify slugs and canonical"}

    summaries = extract_page_summaries(root)
    slug_to_path = {s["slug"]: s["path"] for s in summaries}
    results: list[dict[str, Any]] = []

    for group in parsed:
        slugs = group.get("slugs", [])
        canonical = group.get("canonical", "")
        if not canonical or canonical not in slugs:
            results.append({"slugs": slugs, "error": "canonical slug missing or not in group"})
            continue

        redirects = {s: canonical for s in slugs if s != canonical}
        rewritten_count = _rewrite_cross_references(root, redirects)
        deleted = _delete_non_canonical(root, slugs, canonical, slug_to_path)

        results.append({
            "canonical": canonical,
            "merged_slugs": [s for s in slugs if s != canonical],
            "cross_refs_rewritten": rewritten_count,
            "pages_deleted": deleted,
        })

    return {"ok": True, "stage": "merge", "results": results}


def _rewrite_cross_references(root: Path, redirects: dict[str, str]) -> int:
    wiki_dir = root / "wiki"
    if not wiki_dir.exists():
        return 0
    count = 0
    for path in sorted(wiki_dir.rglob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_content = content
        for old_slug, new_slug in redirects.items():
            escaped = re.escape(old_slug)
            pattern = re.compile(r"\[\[" + escaped + r"(\|[^\]]+)?\]\]")
            new_content = pattern.sub(lambda m: f"[[{new_slug}{m.group(1) or ''}]]", new_content)
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            count += 1
    return count


def _delete_non_canonical(
    root: Path,
    slugs: list[str],
    canonical: str,
    slug_to_path: dict[str, str],
) -> list[str]:
    deleted: list[str] = []
    for slug in slugs:
        if slug == canonical:
            continue
        rel = slug_to_path.get(slug)
        if not rel:
            continue
        target = (root / rel).resolve()
        if target.exists() and target.is_relative_to(root):
            target.unlink()
            deleted.append(rel)
    return deleted


def _build_detect_prompt(summaries: list[dict[str, Any]]) -> str:
    lines = []
    for s in summaries:
        tag_part = f" [{', '.join(s['tags'])}]" if s["tags"] else ""
        desc_part = f" — {s['description']}" if s.get("description") else ""
        lines.append(f"- type={s['type']}, slug={s['slug']}, title={json.dumps(s['title'], ensure_ascii=False)}{tag_part}{desc_part}")

    return (
        "You are a wiki maintenance assistant. Identify groups of pages that likely refer to the same topic under different names.\n\n"
        "Examples of duplicates:\n"
        "- Same name in two languages (English vs Chinese)\n"
        "- Plural vs singular (dpao vs dpaos)\n"
        "- Abbreviation vs full form (vfa vs volatile-fatty-acids)\n"
        "- Synonyms in the same language\n\n"
        "Output ONLY valid JSON:\n"
        '{"groups": [{"slugs": ["slug-a", "slug-b"], "reason": "...", "confidence": "high|medium|low"}]}\n\n'
        "Rules:\n"
        "- Only include groups of 2+ slugs from the input.\n"
        "- high = clearly the same entity. medium = likely. low = uncertain.\n"
        "- Never invent slugs not in the input.\n"
        '- If no duplicates, output {"groups": []}.\n'
        "- Pages of different type usually should NOT be grouped.\n\n"
        f"## Wiki pages ({len(summaries)} entries)\n\n" + "\n".join(lines)
    )


def _parse_groups(groups: list[dict[str, Any]] | str | None) -> list[dict[str, Any]] | None:
    if groups is None:
        return None
    if isinstance(groups, str):
        try:
            parsed = json.loads(groups)
        except json.JSONDecodeError:
            parsed = _extract_json(groups)
            if parsed is None:
                return None
        if isinstance(parsed, dict) and "groups" in parsed:
            parsed = parsed["groups"]
        if not isinstance(parsed, list):
            return None
        return parsed
    if isinstance(groups, list):
        return groups
    return None


def _extract_json(text: str) -> Any | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _group_key(slugs: list[str]) -> str:
    return ",".join(sorted(s.casefold() for s in slugs))

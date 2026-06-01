"""Page merge logic for re-ingest scenarios.

When a generated wiki page already exists and new content arrives from
a re-ingest, this module provides deterministic frontmatter merging
and body merge preparation (LLM-assisted body merge is the caller's
responsibility).

Three layers of protection:
  1. Frontmatter array fields (sources, tags, related) — always union-merged.
  2. Locked frontmatter fields (type, title, created) — preserved from existing.
  3. Body — if different, returns a merge prompt for LLM; caller applies result.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter

UNION_FIELDS = ("sources", "tags", "related")
LOCKED_FIELDS = ("type", "title", "created")
BODY_SHRINK_THRESHOLD = 0.7


def merge_frontmatter(
    existing_fm: dict[str, Any],
    incoming_fm: dict[str, Any],
) -> dict[str, Any]:
    """Merge frontmatter: union array fields, lock scalar fields, update timestamp."""
    merged = copy.deepcopy(incoming_fm)

    for field in LOCKED_FIELDS:
        if field in existing_fm:
            merged[field] = existing_fm[field]

    for field in UNION_FIELDS:
        existing_values = _as_list(existing_fm.get(field))
        incoming_values = _as_list(merged.get(field))
        combined = _dedup_list(existing_values + incoming_values)
        if combined:
            merged[field] = combined
        elif field in merged:
            del merged[field]

    merged["updated"] = date.today().isoformat()
    merged["generated"] = True

    return merged


def prepare_body_merge(
    existing_body: str,
    incoming_body: str,
    page_title: str = "",
) -> dict[str, Any]:
    """Check if bodies differ and return merge prompt if needed."""
    if _normalize_body(existing_body) == _normalize_body(incoming_body):
        return {"needs_merge": False}

    prompt = _build_merge_prompt(existing_body, incoming_body, page_title)
    return {"needs_merge": True, "prompt": prompt}


def validate_merged_body(
    existing_body: str,
    incoming_body: str,
    merged_body: str,
) -> dict[str, Any]:
    """Validate that a merged body hasn't lost too much content."""
    max_len = max(len(existing_body), len(incoming_body))
    if max_len == 0:
        return {"valid": True}

    if len(merged_body) < max_len * BODY_SHRINK_THRESHOLD:
        return {
            "valid": False,
            "reason": "merged body is significantly shorter than inputs",
            "shrink_ratio": len(merged_body) / max_len,
        }

    return {"valid": True}


def apply_page_merge(
    vault_root: str | Path,
    page_path: str,
    incoming_frontmatter: dict[str, Any],
    incoming_body: str,
    merged_body: str | None = None,
) -> dict[str, Any]:
    """Apply a merge to an existing generated page on disk."""
    root = Path(vault_root).expanduser().resolve()
    target = (root / page_path).resolve()

    if not target.is_relative_to(root):
        return {"ok": False, "code": "path_escape", "error": "page path escapes wiki root"}
    if not target.exists():
        return {"ok": False, "code": "page_not_found", "error": f"page not found: {page_path}"}

    existing_text = target.read_text(encoding="utf-8")
    existing_fm, existing_body = split_frontmatter(existing_text)

    if existing_fm.get("generated") is not True:
        return {"ok": False, "code": "manual_page", "error": "refusing to merge into non-generated page"}

    merged_fm = merge_frontmatter(existing_fm, incoming_frontmatter)
    final_body = merged_body if merged_body is not None else incoming_body

    if merged_body is not None:
        validation = validate_merged_body(existing_body, incoming_body, merged_body)
        if not validation.get("valid", False):
            return {"ok": False, "code": "merge_validation_failed", **validation}

    yaml_text = yaml.safe_dump(merged_fm, allow_unicode=True, sort_keys=False).strip()
    title = merged_fm.get("title", Path(page_path).stem)
    text = f"---\n{yaml_text}\n---\n\n# {title}\n\n{final_body.strip()}\n"
    target.write_text(text, encoding="utf-8")

    return {
        "ok": True,
        "page_path": page_path,
        "merged_fields": list(UNION_FIELDS),
        "locked_fields": list(LOCKED_FIELDS),
        "body_merged": merged_body is not None,
    }


def _build_merge_prompt(existing_body: str, incoming_body: str, page_title: str) -> str:
    title_line = f"Page title: {page_title}\n\n" if page_title else ""
    return (
        "You are a wiki maintenance assistant. Merge two versions of the same wiki page.\n\n"
        "Rules:\n"
        "- Preserve every distinct factual claim from both versions.\n"
        "- Eliminate redundancy.\n"
        "- Reorganize sections logically — don't just concatenate.\n"
        "- Use [[wikilink]] syntax where the inputs did.\n"
        "- Output ONLY the merged body content (no frontmatter, no title heading).\n\n"
        f"{title_line}"
        f"## EXISTING VERSION\n{existing_body}\n\n"
        f"## INCOMING VERSION\n{incoming_body}\n\n"
        "Output the merged body content now:"
    )


def _normalize_body(body: str) -> str:
    return " ".join(body.split()).strip().casefold()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _dedup_list(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for item in items:
        key = str(item).casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
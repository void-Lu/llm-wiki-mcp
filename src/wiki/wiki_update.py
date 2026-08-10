"""The only public-domain service for updating an active knowledge page."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any, Mapping

import yaml

from codegraph.codegraph_policy import is_codegraph_managed_path
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.reference_section import build_reference_section, skipped_warnings  # noqa: F401  placeholder
from wiki.wiki_index import refresh_indexes
from wiki.wiki_io import WikiWriteError, split_frontmatter, write_wiki_page
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry, WikiPage
from wiki.wikilink_validator import auto_normalize_wikilinks, validate_wikilinks

LOCKED_FIELDS = {"type", "concept_id", "entity_id", "entity_type", "created", "source_path", "source_hash"}
REMOVED_FIELDS = {"source_capsules", "source_capsule"}
_ALLOWED = ("wiki/concepts/", "wiki/entities/", "wiki/projects/")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _plan_id(path: str, current_hash: str, body: str, frontmatter: Mapping[str, Any]) -> str:
    return _digest("\0".join((path, current_hash, body, yaml.safe_dump(dict(frontmatter), sort_keys=True, allow_unicode=True))))


def preview_update(
    vault_root: str | Path,
    page_path: str,
    incoming_body: str,
    incoming_frontmatter: Mapping[str, Any] | None = None,
    related_pages: list[dict[str, Any]] | None = None,
    related_pages_heading: str | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    target = _target(root, page_path)
    if isinstance(target, dict):
        return target
    if not target.is_file():
        return {"ok": False, "code": "page_not_found"}
    text = target.read_text(encoding="utf-8")
    fm, old_body = split_frontmatter(text)
    if is_codegraph_managed_path(page_path, fm):
        return {"ok": False, "code": "codegraph_managed_page", "error": "CodeGraph-managed pages can only be updated by wiki_codegraph_import"}
    if fm.get("lifecycle", "active") != "active":
        return {"ok": False, "code": "inactive_page"}
    incoming = dict(incoming_frontmatter or {})
    removed_fields = sorted(REMOVED_FIELDS & incoming.keys())
    if removed_fields:
        return {"ok": False, "code": "source_capsules_removed", "fields": removed_fields}
    incoming_body, related_pages_skipped = _with_reference_section(root, incoming_body, related_pages, heading=related_pages_heading)
    incoming_body, normalized_count = auto_normalize_wikilinks(incoming_body, root)
    broken_wikilinks = validate_wikilinks(incoming_body, root)
    violations = _locked_violations(fm, incoming)
    removed_sources = set(_sources(fm)) - set(_sources(incoming)) if "sources" in incoming else set()
    result = {"ok": True, "action": "preview", "page_path": page_path, "current_hash": _digest(text), "plan_id": _plan_id(page_path, _digest(text), incoming_body, incoming), "locked_fields": sorted(LOCKED_FIELDS), "locked_field_violations": violations, "removed_sources": sorted(removed_sources), "normalized_wikilinks": normalized_count, "broken_wikilinks": broken_wikilinks, "diff": "".join(difflib.unified_diff(old_body.splitlines(True), incoming_body.splitlines(True), fromfile="current", tofile="incoming"))}
    return _attach_related_page_skips(result, related_pages, related_pages_skipped)


def apply_update(
    vault_root: str | Path,
    page_path: str,
    incoming_body: str,
    *,
    incoming_frontmatter: Mapping[str, Any] | None = None,
    expected_hash: str | None = None,
    plan_id: str | None = None,
    related_pages: list[dict[str, Any]] | None = None,
    related_pages_heading: str | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    target = _target(root, page_path)
    if isinstance(target, dict):
        return target
    if not target.is_file():
        return {"ok": False, "code": "page_not_found"}
    text = target.read_text(encoding="utf-8")
    existing, _ = split_frontmatter(text)
    if is_codegraph_managed_path(page_path, existing):
        return {"ok": False, "code": "codegraph_managed_page", "error": "CodeGraph-managed pages can only be updated by wiki_codegraph_import"}
    current_hash = _digest(text)
    incoming = dict(incoming_frontmatter or {})
    removed_fields = sorted(REMOVED_FIELDS & incoming.keys())
    if removed_fields:
        return {"ok": False, "code": "source_capsules_removed", "fields": removed_fields}
    incoming_body, related_pages_skipped = _with_reference_section(root, incoming_body, related_pages, heading=related_pages_heading)
    incoming_body, normalized_count = auto_normalize_wikilinks(incoming_body, root)
    broken_wikilinks = validate_wikilinks(incoming_body, root)
    expected_plan = _plan_id(page_path, current_hash, incoming_body, incoming)
    if expected_hash and expected_hash != current_hash:
        return {"ok": False, "code": "expected_hash_mismatch"}
    if plan_id and plan_id != expected_plan:
        return {"ok": False, "code": "plan_stale"}
    if existing.get("lifecycle", "active") != "active":
        return {"ok": False, "code": "inactive_page"}
    violations = _locked_violations(existing, incoming)
    if violations:
        return {"ok": False, "code": "locked_field", "fields": violations}
    if "sources" in incoming and not _sources(incoming):
        return {"ok": False, "code": "sources_required"}
    final = dict(existing)
    final.update(incoming)
    if existing.get("generated") is True:
        final["generated"] = existing["generated"]
        final["maintenance"] = "manual"
        final.setdefault("generation_provenance", {key: existing.get(key) for key in ("prompt_version", "schema_version", "source_hash") if key in existing})
    title = str(final.get("title") or target.stem)
    try:
        write_result = write_wiki_page(
            root,
            WikiPage(Path(page_path), final, title, incoming_body),
            overwrite_generated_only=False,
        )
    except WikiWriteError as exc:
        return _attach_related_page_skips({"ok": False, "code": exc.code, "error": str(exc)}, related_pages, related_pages_skipped)
    updated_text = target.read_text(encoding="utf-8")
    updated_hash = _digest(updated_text)
    sources = {source: "" for source in _sources(final)}
    KnowledgeDependencies(root).update_page(page_path, updated_hash, sources, generated=bool(final.get("generated")), maintenance=str(final.get("maintenance") or "manual"))
    navigation = refresh_indexes(root)
    append_log_entry(root, WikiLogEntry(operation="update", title=title, paths=[page_path], sources=list(sources), project=str(final.get("project") or ""), status="ok"))
    result = {"ok": True, "action": "apply", "page_path": page_path, "hash": updated_hash, "navigation": navigation, "retrieval_index": write_result.get("retrieval_index"), "normalized_wikilinks": normalized_count, "broken_wikilinks": broken_wikilinks}
    return _attach_related_page_skips(result, related_pages, related_pages_skipped)


def _target(root: Path, page_path: str) -> Path | dict[str, Any]:
    normalized = page_path.replace("\\", "/")
    relative = Path(normalized)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return {"ok": False, "code": "path_escape"}
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        return {"ok": False, "code": "path_escape"}
    if not any(normalized.startswith(prefix) for prefix in _ALLOWED) or path.name == "index.md":
        return {"ok": False, "code": "update_path_not_allowed"}
    return path


def _locked_violations(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> list[str]:
    return sorted(key for key in LOCKED_FIELDS if key in incoming and key in existing and incoming[key] != existing[key])


def _sources(frontmatter: Mapping[str, Any]) -> list[str]:
    value = frontmatter.get("sources", [])
    return [str(item) for item in value] if isinstance(value, list) else ([str(value)] if value else [])


def _with_reference_section(
    root: Path,
    body: str,
    related_pages: list[dict[str, Any]] | None,
    heading: str | None = None,
) -> tuple[str, list[dict[str, str]]]:
    return build_reference_section(root, body, related_pages, heading=heading)


def _attach_related_page_skips(
    result: dict[str, Any],
    related_pages: list[dict[str, Any]] | None,
    skipped: list[dict[str, str]],
) -> dict[str, Any]:
    if related_pages is None:
        return result
    enriched = dict(result)
    enriched["related_pages_skipped"] = skipped
    if skipped:
        enriched["warnings"] = [*enriched.get("warnings", []), *skipped_warnings("related_pages", skipped)]
    return enriched

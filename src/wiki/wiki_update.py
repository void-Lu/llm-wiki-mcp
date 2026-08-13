"""The only public-domain service for updating an active knowledge page."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
import secrets
from typing import Any, Mapping

import yaml

from wiki.page_mutation import PageMutationCoordinator
from wiki.atomic_file import sha256_file
from wiki.reference_section import build_reference_section, skipped_warnings  # noqa: F401  placeholder
from wiki.source_provenance import ResolvedRawSource, SourceProvenanceError, SourceProvenanceResolver, source_hash_map
from wiki.update_plan_store import UpdatePlanError, UpdatePlanStore
from wiki.wiki_io import WikiWriteError, prepare_wiki_page, split_frontmatter
from wiki.wiki_models import WikiPage
from wiki.wikilink_validator import auto_normalize_wikilinks, validate_wikilinks
from wiki.wiki_paths import WikiPathError, validate_wiki_page_path

LOCKED_FIELDS = {"type", "concept_id", "entity_id", "entity_type", "created", "source_path", "source_hash"}
REMOVED_FIELDS = {"source_capsules", "source_capsule"}
SERVER_OWNED_FIELDS = {"source_hashes"}
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
    if fm.get("lifecycle", "active") != "active":
        return {"ok": False, "code": "inactive_page"}
    incoming = dict(incoming_frontmatter or {})
    removed_fields = sorted(REMOVED_FIELDS & incoming.keys())
    if removed_fields:
        return {"ok": False, "code": "source_capsules_removed", "fields": removed_fields}
    incoming, resolved_sources, source_error = _prepare_incoming_sources(root, incoming)
    if source_error is not None:
        return source_error
    incoming_body, related_pages_skipped = _with_reference_section(root, incoming_body, related_pages, heading=related_pages_heading)
    incoming_body, normalized_count = auto_normalize_wikilinks(incoming_body, root)
    broken_wikilinks = validate_wikilinks(incoming_body, root)
    violations = _locked_violations(fm, incoming)
    removed_sources = set(_sources(fm)) - set(_sources(incoming)) if "sources" in incoming else set()
    current_hash = sha256_file(target)
    intent_hash = _plan_id(page_path, current_hash, incoming_body, incoming)
    try:
        plan = UpdatePlanStore(str(root)).issue(page_path, current_hash, intent_hash)
    except UpdatePlanError as exc:
        return {"ok": False, "code": exc.code}
    result = {"ok": True, "action": "preview", "page_path": page_path, "current_hash": current_hash, "plan_id": plan.plan_id, "plan_expires_at": plan.expires_at, "locked_fields": sorted(LOCKED_FIELDS), "locked_field_violations": violations, "removed_sources": sorted(removed_sources), "normalized_wikilinks": normalized_count, "broken_wikilinks": broken_wikilinks, "diff": "".join(difflib.unified_diff(old_body.splitlines(True), incoming_body.splitlines(True), fromfile="current", tofile="incoming"))}
    if resolved_sources is not None:
        result["source_hashes"] = source_hash_map(resolved_sources)
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
    current_hash = sha256_file(target)
    incoming = dict(incoming_frontmatter or {})
    removed_fields = sorted(REMOVED_FIELDS & incoming.keys())
    if removed_fields:
        return {"ok": False, "code": "source_capsules_removed", "fields": removed_fields}
    incoming, resolved_sources, source_error = _prepare_incoming_sources(root, incoming)
    if source_error is not None:
        return _attach_related_page_skips(source_error, related_pages, [])
    incoming_body, related_pages_skipped = _with_reference_section(root, incoming_body, related_pages, heading=related_pages_heading)
    incoming_body, normalized_count = auto_normalize_wikilinks(incoming_body, root)
    broken_wikilinks = validate_wikilinks(incoming_body, root)
    plan_store = UpdatePlanStore(str(root)) if plan_id else None
    plan_snapshot = plan_store.get(plan_id) if plan_store is not None and plan_id else None
    plan_base_hash = plan_snapshot.base_hash if plan_snapshot is not None and plan_snapshot.state == "consumed" else current_hash
    expected_plan = _plan_id(page_path, plan_base_hash, incoming_body, incoming)
    if existing.get("lifecycle", "active") != "active":
        return {"ok": False, "code": "inactive_page"}
    violations = _locked_violations(existing, incoming)
    if violations:
        return {"ok": False, "code": "locked_field", "fields": violations}
    if "sources" in incoming and not _sources(incoming):
        return {"ok": False, "code": "sources_required"}
    structural_change = bool(incoming) or related_pages is not None
    if structural_change and not plan_id:
        return _attach_related_page_skips({"ok": False, "code": "update_plan_required"}, related_pages, related_pages_skipped)
    if expected_hash is None and not (plan_snapshot is not None and plan_snapshot.state == "consumed"):
        return _attach_related_page_skips({"ok": False, "code": "expected_hash_required"}, related_pages, related_pages_skipped)
    if expected_hash != current_hash and not (plan_snapshot is not None and plan_snapshot.state == "consumed"):
        return {"ok": False, "code": "expected_hash_mismatch"}
    if resolved_sources is not None:
        try:
            resolved_sources = SourceProvenanceResolver(root).verify(resolved_sources)
        except SourceProvenanceError as exc:
            return _attach_related_page_skips({"ok": False, "code": exc.code}, related_pages, related_pages_skipped)
    final = dict(existing)
    final.update(incoming)
    if existing.get("generated") is True:
        final["generated"] = existing["generated"]
        final["maintenance"] = "manual"
        final.setdefault("generation_provenance", {key: existing.get(key) for key in ("prompt_version", "schema_version", "source_hash") if key in existing})
    if resolved_sources is not None:
        final["sources"] = [source.relative_path for source in resolved_sources]
        final["source_hashes"] = source_hash_map(resolved_sources)
        final["provenance_unverified"] = False
        final["freshness"] = "fresh"
    elif not _stored_source_hashes(existing):
        final["provenance_unverified"] = True
        final["freshness"] = "review_required"
    title = str(final.get("title") or target.stem)
    try:
        prepared = prepare_wiki_page(
            root,
            WikiPage(Path(page_path), final, title, incoming_body),
            overwrite_generated_only=False,
        )
    except WikiWriteError as exc:
        return _attach_related_page_skips({"ok": False, "code": exc.code, "error": str(exc)}, related_pages, related_pages_skipped)
    updated_hash = _digest(prepared.text)
    coordinator = PageMutationCoordinator(root, plan_store=plan_store)
    mutation = coordinator.write_and_project(
        request_key=plan_id or f"body:{secrets.token_urlsafe(18)}",
        operation_kind="update",
        page_path=page_path,
        base_hash=current_hash,
        intended_hash=updated_hash,
        text=prepared.text,
        expected_hash=current_hash,
        plan_id=plan_id,
        intent_hash=expected_plan,
    )
    mutation_dict = mutation.to_dict()
    if not mutation.ok:
        return _attach_related_page_skips(mutation_dict, related_pages, related_pages_skipped)
    if mutation.already_applied or mutation.state == "already_applied":
        replay = {
            "ok": True,
            "state": "already_applied",
            "already_applied": True,
            "operation_id": mutation.operation_id,
            "page_path": page_path,
        }
        if mutation.repair_action:
            replay["repair_action"] = mutation.repair_action
        return _attach_related_page_skips(replay, related_pages, related_pages_skipped)
    stages = mutation.stages
    dependency_projection = stages.get("dependencies", {}).get("result", {"ok": True, "state": "ready"})
    navigation = stages.get("navigation", {}).get("result")
    retrieval_index = stages.get("retrieval", {}).get("result")
    result = {"ok": True, "state": mutation.state or "completed", "action": "apply", "page_path": page_path, "operation_id": mutation.operation_id, "hash": updated_hash, "page_hash": mutation.page_hash or updated_hash, "navigation": navigation, "retrieval_index": retrieval_index, "normalized_wikilinks": normalized_count, "broken_wikilinks": broken_wikilinks, "dependency_projection": dependency_projection, "provenance_status": "verified" if _stored_source_hashes(final) else "provenance_unverified", "freshness": str(final.get("freshness") or "review_required")}
    if mutation.repair_action:
        result["repair_action"] = mutation.repair_action
    if mutation.failed_stage:
        result["failed_stage"] = mutation.failed_stage
    if resolved_sources is not None:
        result["source_hashes"] = source_hash_map(resolved_sources)
    return _attach_related_page_skips(result, related_pages, related_pages_skipped)


def _target(root: Path, page_path: str) -> Path | dict[str, Any]:
    try:
        relative = validate_wiki_page_path(page_path, allow_navigation_index=False)
    except WikiPathError as exc:
        code = "update_path_not_allowed" if exc.code in {"navigation_index_forbidden", "invalid_wiki_path"} else exc.code
        return {"ok": False, "code": code}
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        return {"ok": False, "code": "path_escape"}
    return path


def _locked_violations(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> list[str]:
    return sorted(key for key in LOCKED_FIELDS if key in incoming and key in existing and incoming[key] != existing[key])


def _sources(frontmatter: Mapping[str, Any]) -> list[str]:
    value = frontmatter.get("sources", [])
    return [str(item) for item in value] if isinstance(value, list) else ([str(value)] if value else [])


def _prepare_incoming_sources(
    root: Path,
    incoming: dict[str, Any],
) -> tuple[dict[str, Any], list[ResolvedRawSource] | None, dict[str, Any] | None]:
    if SERVER_OWNED_FIELDS & incoming.keys():
        return incoming, None, {"ok": False, "code": "source_hashes_server_owned"}
    if "sources" not in incoming:
        return incoming, None, None
    values = _sources(incoming)
    if not values:
        return incoming, None, {"ok": False, "code": "sources_required"}
    try:
        resolved = SourceProvenanceResolver(root).resolve_many(values)
    except SourceProvenanceError as exc:
        return incoming, None, {"ok": False, "code": exc.code}
    prepared = dict(incoming)
    prepared["sources"] = [source.relative_path for source in resolved]
    prepared["source_hashes"] = source_hash_map(resolved)
    return prepared, resolved, None


def _stored_source_hashes(frontmatter: Mapping[str, Any]) -> dict[str, str]:
    value = frontmatter.get("source_hashes")
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key) and str(item)}


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

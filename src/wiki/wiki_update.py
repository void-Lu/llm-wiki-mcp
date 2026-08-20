"""The only public-domain service for updating an active knowledge page."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wiki.atomic_file import sha256_file
from wiki.page_mutation import PageMutationCoordinator, explain_stage
from wiki.page_mutation_adapters import build_plan_intent
from wiki.page_operation_store import UpdatePlanError
from wiki.page_policy import provenance_status, stamp_page_policy
from wiki.reference_section import build_reference_section, skipped_warnings
from wiki.source_provenance import ResolvedRawSource, SourceProvenanceError, SourceProvenanceResolver, source_hash_map
from wiki.wiki_io import WikiWriteError, prepare_wiki_page, split_frontmatter
from wiki.wiki_models import WikiPage
from wiki.wikilink_validator import auto_normalize_wikilinks, validate_wikilinks
from wiki.wiki_paths import WikiPathError, resolve_within_root, translate_path_error, validate_wiki_page_path

LOCKED_FIELDS = {"type", "concept_id", "entity_id", "entity_type", "created", "source_path", "source_hash"}
REMOVED_FIELDS = {"source_capsules", "source_capsule"}
SERVER_OWNED_FIELDS = {"source_hashes"}


@dataclass(frozen=True)
class _PreparedIncoming:
    """Hold the side-effect-free incoming update preparation result."""

    incoming: dict[str, Any]
    prepared_body: str
    resolved_sources: list[ResolvedRawSource] | None
    related_pages_skipped: list[dict[str, str]]
    normalized_count: int
    broken_wikilinks: list[dict[str, Any]]
    removed_fields: list[str]


def _prepare_incoming(
    root: Path,
    *,
    incoming_frontmatter: Mapping[str, Any] | None,
    incoming_body: str,
    related_pages: list[dict[str, Any]] | None,
    related_pages_heading: str | None,
    existing_frontmatter: Mapping[str, Any] | None = None,
) -> _PreparedIncoming | dict[str, Any]:
    """Validate and normalize incoming content before preview/apply diverge."""

    incoming = dict(incoming_frontmatter or {})
    removed_fields = sorted(REMOVED_FIELDS & incoming.keys())
    if removed_fields:
        return {"ok": False, "code": "source_capsules_removed", "fields": removed_fields}

    incoming, resolved_sources, source_error = _prepare_incoming_sources(root, incoming)
    if source_error is not None:
        return source_error

    prepared_body, related_pages_skipped = build_reference_section(
        root,
        incoming_body,
        related_pages,
        heading=related_pages_heading,
    )
    prepared_body, normalized_count = auto_normalize_wikilinks(prepared_body, root)
    broken_wikilinks = validate_wikilinks(prepared_body, root)
    if existing_frontmatter is not None and existing_frontmatter.get("lifecycle", "active") != "active":
        return {"ok": False, "code": "inactive_page"}

    return _PreparedIncoming(
        incoming=incoming,
        prepared_body=prepared_body,
        resolved_sources=resolved_sources,
        related_pages_skipped=related_pages_skipped,
        normalized_count=normalized_count,
        broken_wikilinks=broken_wikilinks,
        removed_fields=removed_fields,
    )


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
    prepared = _prepare_incoming(
        root,
        incoming_frontmatter=incoming_frontmatter,
        incoming_body=incoming_body,
        related_pages=related_pages,
        related_pages_heading=related_pages_heading,
        existing_frontmatter=fm,
    )
    if isinstance(prepared, dict):
        return prepared
    incoming = prepared.incoming
    incoming_body = prepared.prepared_body
    resolved_sources = prepared.resolved_sources
    related_pages_skipped = prepared.related_pages_skipped
    normalized_count = prepared.normalized_count
    broken_wikilinks = prepared.broken_wikilinks
    violations = _locked_violations(fm, incoming)
    removed_sources = set(_sources(fm)) - set(_sources(incoming)) if "sources" in incoming else set()
    current_hash = sha256_file(target)
    coordinator = PageMutationCoordinator(root)
    try:
        plan = coordinator.issue_plan(
            page_path=page_path,
            base_hash=current_hash,
            intent=build_plan_intent(
                body=incoming_body,
                frontmatter=incoming,
            ),
        )
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
    prepared = _prepare_incoming(
        root,
        incoming_frontmatter=incoming_frontmatter,
        incoming_body=incoming_body,
        related_pages=related_pages,
        related_pages_heading=related_pages_heading,
        existing_frontmatter=existing,
    )
    if isinstance(prepared, dict):
        if prepared.get("code") in {"source_capsules_removed", "inactive_page"}:
            return prepared
        return _attach_related_page_skips(prepared, related_pages, [])
    incoming = prepared.incoming
    incoming_body = prepared.prepared_body
    resolved_sources = prepared.resolved_sources
    related_pages_skipped = prepared.related_pages_skipped
    normalized_count = prepared.normalized_count
    broken_wikilinks = prepared.broken_wikilinks
    violations = _locked_violations(existing, incoming)
    if violations:
        return {"ok": False, "code": "locked_field", "fields": violations}
    if "sources" in incoming and not _sources(incoming):
        return {"ok": False, "code": "sources_required"}
    structural_change = bool(incoming) or related_pages is not None
    if structural_change and not plan_id:
        return _attach_related_page_skips({"ok": False, "code": "update_plan_required"}, related_pages, related_pages_skipped)
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
    stamp_source_hashes: Mapping[str, object] | None = None
    if resolved_sources is not None:
        final["sources"] = [source.relative_path for source in resolved_sources]
        final["source_hashes"] = source_hash_map(resolved_sources)
        stamp_source_hashes = final["source_hashes"]
    try:
        policy_stamp = stamp_page_policy(final, source_hashes=stamp_source_hashes)
    except (TypeError, ValueError) as exc:
        return _attach_related_page_skips({"ok": False, "code": "invalid_page_policy", "error": str(exc)}, related_pages, related_pages_skipped)
    final.update(policy_stamp)
    title = str(final.get("title") or target.stem)
    try:
        prepared = prepare_wiki_page(
            root,
            WikiPage(Path(page_path), final, title, incoming_body),
            overwrite_generated_only=False,
        )
    except WikiWriteError as exc:
        return _attach_related_page_skips({"ok": False, "code": exc.code, "error": str(exc)}, related_pages, related_pages_skipped)
    coordinator = PageMutationCoordinator(root)
    mutation = coordinator.write_and_project(
        operation_kind="update",
        page_path=page_path,
        base_hash=current_hash,
        text=prepared.text,
        intended_hash=prepared.text_hash,
        expected_hash=expected_hash,
        plan_id=plan_id,
        plan_intent=build_plan_intent(
            body=incoming_body,
            frontmatter=incoming,
        ),
    )
    mutation_dict = mutation.to_dict()
    if not mutation.ok:
        return _attach_related_page_skips(mutation_dict, related_pages, related_pages_skipped)
    if mutation.already_applied:
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
    dependency_projection = explain_stage(mutation.stages.get("dependencies"), stage_name="dependencies")
    navigation = mutation.stage_result("navigation")
    retrieval_index = mutation.stage_result("retrieval")
    result = {"ok": True, "state": mutation.state or "completed", "action": "apply", "page_path": page_path, "operation_id": mutation.operation_id, "hash": prepared.text_hash, "page_hash": mutation.page_hash or prepared.text_hash, "navigation": navigation, "retrieval_index": retrieval_index, "normalized_wikilinks": normalized_count, "broken_wikilinks": broken_wikilinks, "dependency_projection": dependency_projection, "provenance_status": provenance_status(policy_stamp), "freshness": str(policy_stamp["freshness"])}
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
        path = resolve_within_root(root, relative)
    except WikiPathError as exc:
        return {"ok": False, "code": translate_path_error(exc.code, "update")}
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

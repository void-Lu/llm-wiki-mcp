from __future__ import annotations

import re
import string
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

from common.redaction import count_redactions
from common.privacy_policy import LocatorError, PrivacyPolicy, normalize_vault_relative
from wiki.page_mutation import PageMutationCoordinator
from wiki.page_operation_store import PageOperationError
from wiki.page_repair import PageRepairService
from wiki.wiki_io import WikiWriteError, prepare_wiki_page
from wiki.wiki_models import WikiPage
from wiki.wiki_paths import create_wiki_root
from wiki.wikilink_validator import auto_normalize_wikilinks, validate_wikilinks
from wiki.reference_section import build_reference_section, skipped_warnings
from wiki.source_provenance import ResolvedRawSource, SourceProvenanceError, SourceProvenanceResolver, source_hash_map

NOTE_TYPES = {"spec", "plan", "troubleshooting", "researches", "knowledge", "entity", "chat"}
PROJECT_NOTE_TYPES = {"spec", "plan", "troubleshooting", "researches"}
DOMAINS = {"common-errors", "integration-patterns"}
WINDOWS_RESERVED_CHARS = set('<>:"|?*')
WINDOWS_RESERVED_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"}
WINDOWS_RESERVED_DEVICE_PREFIXES = ("COM", "LPT")
WINDOWS_RESERVED_DEVICE_SUFFIXES = set("123456789¹²³")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _has_path_traversal(value: str) -> bool:
    path = Path(value)
    return "/" in value or "\\" in value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or len(path.parts) != 1


def _has_windows_reserved_character(value: str) -> bool:
    return any(char in WINDOWS_RESERVED_CHARS or ord(char) < 32 for char in value)


def _is_windows_reserved_device_name(value: str) -> bool:
    base = value.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED_DEVICE_NAMES:
        return True
    return len(base) == 4 and base[:3] in WINDOWS_RESERVED_DEVICE_PREFIXES and base[3] in WINDOWS_RESERVED_DEVICE_SUFFIXES


def _invalid_windows_component(value: str) -> bool:
    return _has_windows_reserved_character(value) or value.endswith((".", " ")) or _is_windows_reserved_device_name(value)


def _slug(value: str) -> str:
    slug = value.strip()
    punctuation = re.escape(string.punctuation)
    slug = re.sub(rf"[\s{punctuation}]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80].rstrip("-")


def _filename(title: str, filename: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if filename is None:
        stem = _slug(title)
        if not stem:
            return None, _error("empty_slug", "title does not produce a valid filename slug")
        return f"{stem}.md", None
    value = filename.strip()
    if not value:
        return None, _error("empty_slug", "filename is empty")
    if _has_path_traversal(value):
        return None, _error("path_escape", "filename must stay inside the target directory")
    if _invalid_windows_component(value):
        return None, _error("invalid_filename", "filename contains a Windows-invalid path component")
    if not value.lower().endswith(".md"):
        value = f"{value}.md"
    if Path(value).stem == "":
        return None, _error("empty_slug", "filename is empty")
    return value, None


def _safe_segment(value: str | None, missing_code: str, label: str) -> tuple[str | None, dict[str, Any] | None]:
    if not value:
        return None, _error(missing_code, f"{label} is required")
    if _has_path_traversal(value):
        return None, _error("path_escape", f"{label} must be a single path segment")
    if _invalid_windows_component(value):
        return None, _error("invalid_path_component", f"{label} contains a Windows-invalid path component")
    return value, None


def _known_or_existing(value: str, known_values: set[str], directory: Path) -> dict[str, Any] | None:
    if value in known_values or directory.is_dir():
        return None
    # Fallback: discover existing sibling subdirs under the same parent
    parent = directory.parent
    if parent.is_dir():
        existing = {p.name for p in parent.iterdir() if p.is_dir()}
        if value in existing:
            return None
    return _error("unknown_subdir", f"unknown subdir: {value}")


def _frontmatter(
    note_type: str,
    title: str,
    project: str | None,
    domain: str | None,
    related_script_types: list[str] | None,
    related_objects: list[str] | None,
    related_scripts: list[str] | None,
    tags: list[str] | None,
    zentao_urls: list[str] | None,
    decision_status: str | None,
    status: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": note_type,
        "generated": False,
        "project": project or "",
        "author": "copilot",
        "updated_at": date.today().isoformat(),
        "tags": [note_type, *(tags or [])],
        "title": title,
    }
    if note_type == "spec":
        data.update({"status": status or "", "related_objects": related_objects or [], "related_scripts": related_scripts or []})
    elif note_type == "plan":
        data.update({"status": status or "", "related_objects": related_objects or [], "related_scripts": related_scripts or []})
    elif note_type == "troubleshooting":
        data.update({"status": status or "", "related_objects": related_objects or [], "related_scripts": related_scripts or []})
    elif note_type == "researches":
        data.update({"status": status or "", "related_objects": related_objects or [], "related_scripts": related_scripts or [], "zentao_urls": zentao_urls or []})
    elif note_type in {"knowledge", "entity"}:
        data.update({"topic": title, "domain": domain or "", "related_objects": related_objects or []})
        if related_script_types is not None:
            data["related_script_types"] = related_script_types
    return data


def save_obsidian_note(
    note_type: str,
    title: str,
    content: str,
    project: str | None = None,
    domain: str | None = None,
    related_script_types: list[str] | None = None,
    script_type: str | None = None,
    object_type: str | None = None,
    related_objects: list[str] | None = None,
    related_scripts: list[str] | None = None,
    tags: list[str] | None = None,
    zentao_urls: list[str] | None = None,
    decision_status: str | None = None,
    status: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
    auto_index: bool = True,
    vault_root: str | Path | None = None,
    chat_metadata: dict[str, Any] | None = None,
    chat_derived: bool = False,
    chat_sources: list[dict[str, str]] | None = None,
    related_pages: list[dict[str, Any]] | None = None,
    related_pages_heading: str | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    if vault_root is None or not str(vault_root).strip():
        return _error("missing_vault_root", "vault_root is required")
    root = Path(vault_root).expanduser().resolve()
    policy = PrivacyPolicy()

    if note_type not in NOTE_TYPES:
        return _error("invalid_note_type", f"invalid note_type: {note_type}")
    if note_type == "chat":
        if chat_derived:
            return _error("invalid_chat_derived", "chat sources cannot be chat-derived pages")
        create_wiki_root(root)
        from wiki.chat_memory import ChatMemoryError, ChatMemoryService

        try:
            return ChatMemoryService(root).save(content, chat_metadata)
        except ChatMemoryError as exc:
            return _error(exc.code, str(exc))
        except OSError as exc:
            return _error("write_failed", str(exc))
    if type(chat_derived) is not bool:
        return _error("invalid_chat_derived", "chat_derived must be a boolean")
    if chat_derived and note_type not in {"knowledge", "entity"}:
        return _error("invalid_chat_derived", "only knowledge or entity pages may be chat-derived")
    provenance: list[dict[str, Any]] = []
    if chat_derived:
        from wiki.chat_memory import ChatMemoryService

        validated = ChatMemoryService(root).provenance(chat_sources)
        if not validated["ok"]:
            return _error(str(validated["code"]), "chat_sources must reference existing fixed chat revisions")
        provenance = list(validated["sources"])

    resolved_sources: list[ResolvedRawSource] = []
    provenance_resolver: SourceProvenanceResolver | None = None
    if chat_derived:
        source_values: object = [item["path"] for item in provenance]
    elif sources is not None:
        source_values = sources
    else:
        source_values = None
    if source_values is not None:
        provenance_resolver = SourceProvenanceResolver(root)
        try:
            resolved_sources = provenance_resolver.resolve_many(source_values)
        except SourceProvenanceError as exc:
            return _error(exc.code, "source provenance could not be verified")
    source_paths = [item.relative_path for item in resolved_sources]
    source_hashes = source_hash_map(resolved_sources)
    safe_title = policy.redact_display_text(title)
    name, name_error = _filename(safe_title, filename)
    if name_error is not None:
        return name_error
    assert name is not None
    try:
        name = normalize_vault_relative(name)
    except LocatorError as exc:
        return _error(exc.code, "filename violates the privacy policy")

    if note_type in PROJECT_NOTE_TYPES:
        project_value, project_error = _safe_segment(project, "missing_project", "project")
        if project_error is not None:
            return project_error
        assert project_value is not None
        try:
            project_value = normalize_vault_relative(project_value)
        except LocatorError as exc:
            return _error(exc.code, "project violates the privacy policy")
        if note_type == "spec":
            relative_path = Path("wiki") / "projects" / project_value / "specs" / name
        elif note_type == "plan":
            relative_path = Path("wiki") / "projects" / project_value / "plans" / name
        elif note_type == "troubleshooting":
            relative_path = Path("wiki") / "projects" / project_value / "troubleshooting" / name
        else:
            relative_path = Path("wiki") / "projects" / project_value / "researches" / name
        project = project_value
    else:
        if project:
            return _error("knowledge_project_not_allowed", "knowledge and entity notes do not accept project")
        domain_value, domain_error = _safe_segment(domain, "missing_domain", "domain")
        if domain_error is not None:
            return domain_error
        assert domain_value is not None
        try:
            domain_value = normalize_vault_relative(domain_value)
        except LocatorError as exc:
            return _error(exc.code, "domain violates the privacy policy")
        domain_dir = root / "wiki" / ("entities" if note_type == "entity" else "concepts") / domain_value
        subdir_error = _known_or_existing(domain_value, DOMAINS, domain_dir)
        if subdir_error is not None:
            return subdir_error
        relative_path = Path("wiki") / ("entities" if note_type == "entity" else "concepts") / domain_value / name
        domain = domain_value

    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        return _error("path_escape", "resolved note path escapes vault_root")
    if target.exists() and not overwrite:
        return _error("file_exists", "target note already exists")

    redacted_content = policy.redact_display_text(content)
    redacted_count = count_redactions(f"{title}\n{content}", f"{safe_title}\n{redacted_content}")
    redacted_content, normalized_wikilink_count = auto_normalize_wikilinks(redacted_content, root)
    related_pages_skipped: list[dict[str, str]] = []
    if related_pages is not None:
        redacted_content, related_pages_skipped = build_reference_section(root, redacted_content, related_pages, heading=related_pages_heading)
    sources_skipped: list[dict[str, str]] = []
    safe_tags = policy.redact_metadata(tags or [], field="tags")
    safe_related_script_types = policy.redact_metadata(related_script_types or [], field="related_script_types")
    safe_related_objects = policy.redact_metadata(related_objects or [], field="related_objects")
    safe_related_scripts = policy.redact_metadata(related_scripts or [], field="related_scripts")
    safe_zentao_urls = policy.redact_metadata(zentao_urls or [], field="zentao_urls")
    frontmatter = _frontmatter(
        note_type,
        safe_title,
        project,
        domain,
        safe_related_script_types if isinstance(safe_related_script_types, list) else [],
        safe_related_objects if isinstance(safe_related_objects, list) else [],
        safe_related_scripts if isinstance(safe_related_scripts, list) else [],
        safe_tags if isinstance(safe_tags, list) else [],
        safe_zentao_urls if isinstance(safe_zentao_urls, list) else [],
        decision_status,
        status,
    )
    if chat_derived:
        frontmatter["chat_derived"] = True
        frontmatter["chat_sources"] = [
            {"source_id": item["source_id"], "revision": item["revision"], "redacted_hash": item["redacted_hash"]}
            for item in provenance
        ]
        frontmatter["sources"] = source_paths
        frontmatter["source_hashes"] = source_hashes
    elif sources is not None:
        frontmatter["sources"] = source_paths
        frontmatter["source_hashes"] = source_hashes
    if provenance_resolver is not None:
        try:
            resolved_sources = provenance_resolver.verify(resolved_sources)
        except SourceProvenanceError as exc:
            return _error(exc.code, "source provenance could not be verified")
        source_paths = [item.relative_path for item in resolved_sources]
        source_hashes = source_hash_map(resolved_sources)
        if chat_derived or sources is not None:
            frontmatter["sources"] = source_paths
            frontmatter["source_hashes"] = source_hashes
    try:
        create_wiki_root(root)
    except OSError:
        return _error("write_failed", "wiki root could not be prepared")
    try:
        prepared = prepare_wiki_page(
            root,
            WikiPage(relative_path, frontmatter, safe_title, redacted_content),
            overwrite_generated_only=False,
        )
    except WikiWriteError as exc:
        return _error(exc.code, str(exc))
    base_hash = sha256(target.read_bytes()).hexdigest() if target.is_file() else None
    intended_hash = sha256(prepared.text.encode("utf-8")).hexdigest()
    coordinator = PageMutationCoordinator(root)
    try:
        operation = coordinator.prepare(
            request_key=f"note:{relative_path.as_posix()}:{base_hash or 'missing'}:{intended_hash}",
            operation_kind="update" if base_hash is not None else "create",
            page_path=relative_path.as_posix(),
            base_hash=base_hash,
            intended_hash=intended_hash,
        )
    except PageOperationError as exc:
        return _error(exc.code, "page operation could not be prepared")
    if operation.state == "completed":
        projection_result: dict[str, object] = {"ok": True, "state": "completed", "already_applied": True, "operation_id": operation.operation_id}
    elif operation.state == "prepared":
        commit_result = coordinator.commit(operation.operation_id, prepared.text, expected_hash=base_hash)
        if not commit_result.get("ok"):
            return dict(commit_result)
        projection_result = coordinator.run_projections(operation.operation_id, PageRepairService(root).projections_for(operation))
    elif operation.state in {"page_committed", "repair_pending"}:
        projection_result = coordinator.run_projections(operation.operation_id, PageRepairService(root).projections_for(operation))
    else:
        return _error("operation_not_committed", "page operation is not ready for projection")
    if not projection_result.get("ok"):
        return dict(projection_result)
    completed_operation = coordinator.store.get_operation(operation.operation_id)
    stages = completed_operation.stages if completed_operation is not None else {}
    dependency_projection = stages.get("dependencies", {}).get("result", {"ok": True, "state": "ready"})
    result: dict[str, Any] = {
        "ok": True,
        "state": projection_result.get("state", "completed"),
        "path": relative_path.as_posix(),
        "created": True,
        "operation_id": operation.operation_id,
        "page_hash": intended_hash,
        "redacted_count": prepared.redacted_count,
        "indexed": None,
        "wikilink_target": name[:-3] if name.endswith(".md") else name,
        "normalized_wikilinks": normalized_wikilink_count,
        "broken_wikilinks": validate_wikilinks(redacted_content, root),
        "provenance_status": "verified" if resolved_sources else "provenance_unverified",
        "freshness": "fresh" if resolved_sources else "review_required",
        "dependency_projection": dependency_projection,
    }
    if projection_result.get("state") == "repair_pending":
        result["repair_action"] = "repair_page_operation"
        result["failed_stage"] = projection_result.get("failed_stage")

    broken_wikilinks = validate_wikilinks(redacted_content, root)
    result: dict[str, Any] = {
        "ok": True,
        "path": relative_path.as_posix(),
        "created": True,
        "redacted_count": redacted_count,
        "indexed": None,
        "wikilink_target": name[:-3] if name.endswith(".md") else name,
        "normalized_wikilinks": normalized_wikilink_count,
        "broken_wikilinks": broken_wikilinks,
        "provenance_status": "verified" if resolved_sources else "provenance_unverified",
        "freshness": "fresh" if resolved_sources else "review_required",
        "dependency_projection": dependency_projection,
    }
    if related_pages is not None:
        result["related_pages_skipped"] = related_pages_skipped
    if sources is not None and not chat_derived:
        result["sources_skipped"] = sources_skipped
    skipped = [*related_pages_skipped, *sources_skipped]
    if skipped:
        result["warnings"] = [
            *skipped_warnings("related_pages", related_pages_skipped),
            *skipped_warnings("sources", sources_skipped),
        ]
    return result

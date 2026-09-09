from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from common.privacy_policy import LocatorError, PrivacyPolicy, normalize_vault_relative
from wiki.page_mutation import PageMutationCoordinator, explain_stage
from wiki.page_policy import provenance_status, stamp_page_policy
from wiki.wiki_io import WikiWriteError, prepare_wiki_page, split_frontmatter
from wiki.wiki_models import WikiPage
from wiki.wiki_paths import WikiPathError, create_wiki_root, resolve_within_root, safe_segment, slug, translate_path_error
from wiki.wikilink_validator import auto_normalize_wikilinks, validate_wikilinks
from wiki.reference_section import build_reference_section, skipped_warnings
from wiki.source_provenance import ResolvedRawSource, SourceProvenanceError, SourceProvenanceResolver, source_hash_map

NOTE_TYPES = {"spec", "plan", "troubleshooting", "researches", "knowledge", "entity", "chat"}
PROJECT_NOTE_TYPES = {"spec", "plan", "troubleshooting", "researches"}
DOMAINS = {"common-errors", "integration-patterns"}
def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message}


def _filename(title: str, filename: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if filename is None:
        stem = slug(title, lowercase=False, fallback="", ascii_punctuation=True)
        if not stem:
            return None, _error("empty_slug", "title does not produce a valid filename slug")
        return f"{stem}.md", None
    value = filename.strip()
    if not value:
        return None, _error("empty_slug", "filename is empty")
    try:
        safe_segment(value)
    except WikiPathError as exc:
        code = translate_path_error(exc.code, "note_filename")
        return None, _error(code, "filename must stay inside the target directory" if code == "path_escape" else "filename contains a Windows-invalid path component")
    if not value.lower().endswith(".md"):
        value = f"{value}.md"
    if Path(value).stem == "":
        return None, _error("empty_slug", "filename is empty")
    return value, None


def _required_segment(value: str | None, missing_code: str, label: str) -> tuple[str | None, dict[str, Any] | None]:
    if not value:
        return None, _error(missing_code, f"{label} is required")
    try:
        return safe_segment(value), None
    except WikiPathError as exc:
        code = translate_path_error(exc.code, "note_segment")
        if code == "path_escape":
            return None, _error("path_escape", f"{label} must be a single path segment")
        return None, _error(code, f"{label} contains a Windows-invalid path component")


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
    related_objects: list[str] | None = None,
    related_scripts: list[str] | None = None,
    tags: list[str] | None = None,
    zentao_urls: list[str] | None = None,
    status: str | None = None,
    filename: str | None = None,
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
    # The default filename must not expose sensitive title text. Page display
    # redaction and its count remain owned by prepare_wiki_page below.
    filename_title = policy.redact_display_text(title) if filename is None else title
    name, name_error = _filename(filename_title, filename)
    if name_error is not None:
        return name_error
    assert name is not None
    try:
        name = normalize_vault_relative(name)
    except LocatorError as exc:
        return _error(exc.code, "filename violates the privacy policy")

    if note_type in PROJECT_NOTE_TYPES:
        project_value, project_error = _required_segment(project, "missing_project", "project")
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
        domain_value, domain_error = _required_segment(domain, "missing_domain", "domain")
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

    try:
        target = resolve_within_root(root, relative_path)
    except WikiPathError as exc:
        return _error(translate_path_error(exc.code, "note_segment"), "resolved note path escapes vault_root")
    if target.exists():
        return _error("file_exists", "target note already exists")

    normalized_content, normalized_wikilink_count = auto_normalize_wikilinks(content, root)
    related_pages_skipped: list[dict[str, str]] = []
    if related_pages is not None:
        normalized_content, related_pages_skipped = build_reference_section(root, normalized_content, related_pages, heading=related_pages_heading)
    sources_skipped: list[dict[str, str]] = []
    frontmatter = _frontmatter(
        note_type,
        title,
        project,
        domain,
        related_script_types or [],
        related_objects or [],
        related_scripts or [],
        tags or [],
        zentao_urls or [],
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
        policy_stamp = stamp_page_policy(frontmatter, source_hashes=source_hashes)
    except (TypeError, ValueError) as exc:
        return _error("invalid_page_policy", str(exc))
    frontmatter.update(policy_stamp)
    try:
        create_wiki_root(root)
    except OSError:
        return _error("write_failed", "wiki root could not be prepared")
    try:
        prepared = prepare_wiki_page(
            root,
            WikiPage(relative_path, frontmatter, title, normalized_content),
            overwrite_generated_only=False,
        )
    except WikiWriteError as exc:
        return _error(exc.code, str(exc))
    coordinator = PageMutationCoordinator(root)
    projection_result = coordinator.write_and_project(
        operation_kind="note",
        page_path=relative_path.as_posix(),
        base_hash=None,
        text=prepared.text,
        intended_hash=prepared.text_hash,
    )
    if not projection_result.ok:
        return projection_result.to_dict()
    operation_id = projection_result.operation_id or ""
    dependency_projection = explain_stage(projection_result.stages.get("dependencies"), stage_name="dependencies")
    _, prepared_body = split_frontmatter(prepared.text)
    broken_wikilinks = validate_wikilinks(prepared_body, root)
    result: dict[str, Any] = {
        "ok": True,
        "state": projection_result.state or "completed",
        "path": relative_path.as_posix(),
        "created": True,
        "operation_id": operation_id,
        "page_hash": projection_result.page_hash or prepared.text_hash,
        "redacted_count": prepared.redacted_count,
        "wikilink_target": name[:-3] if name.endswith(".md") else name,
        "normalized_wikilinks": normalized_wikilink_count,
        "broken_wikilinks": broken_wikilinks,
        "provenance_status": provenance_status(policy_stamp),
        "freshness": str(policy_stamp["freshness"]),
        "dependency_projection": dependency_projection,
    }
    if projection_result.repair_action:
        result["repair_action"] = projection_result.repair_action
    if projection_result.failed_stage:
        result["failed_stage"] = projection_result.failed_stage
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

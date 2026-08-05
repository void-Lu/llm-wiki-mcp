from __future__ import annotations

import re
import string
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from common.redaction import count_redactions, redact_sensitive_text
from wiki.wiki_index import refresh_indexes
from wiki.wiki_log import append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import create_wiki_root

NOTE_TYPES = {"spec", "plan", "troubleshooting", "researches", "knowledge", "entity", "chat"}
PROJECT_NOTE_TYPES = {"spec", "plan", "troubleshooting", "researches"}
DOMAINS = {"common-errors", "integration-patterns", "netsuite-object-playbooks", "suitescript-patterns"}
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
) -> dict[str, Any]:
    if vault_root is None or not str(vault_root).strip():
        return _error("missing_vault_root", "vault_root is required")
    root = Path(vault_root).expanduser().resolve()
    create_wiki_root(root)

    if note_type not in NOTE_TYPES:
        return _error("invalid_note_type", f"invalid note_type: {note_type}")
    if note_type == "chat":
        if chat_derived:
            return _error("invalid_chat_derived", "chat sources cannot be chat-derived pages")
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
    name, name_error = _filename(title, filename)
    if name_error is not None:
        return name_error
    assert name is not None

    if note_type in PROJECT_NOTE_TYPES:
        project_value, project_error = _safe_segment(project, "missing_project", "project")
        if project_error is not None:
            return project_error
        assert project_value is not None
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

    redacted_content = redact_sensitive_text(content)
    redacted_count = count_redactions(content, redacted_content)
    frontmatter = _frontmatter(note_type, title, project, domain, related_script_types, related_objects, related_scripts, tags, zentao_urls, decision_status, status)
    if chat_derived:
        frontmatter["chat_derived"] = True
        frontmatter["chat_sources"] = [
            {"source_id": item["source_id"], "revision": item["revision"], "redacted_hash": item["redacted_hash"]}
            for item in provenance
        ]
        frontmatter["sources"] = [item["path"] for item in provenance]
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    note_text = f"---\n{yaml_text}\n---\n\n# {title}\n\n{redacted_content}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(note_text, encoding="utf-8")
        refresh_indexes(root)
        refresh_overview(root)
        append_log_entry(root, WikiLogEntry(operation="concept" if note_type == "knowledge" else "entity" if note_type == "entity" else "note", title=title, paths=[relative_path.as_posix()], sources=[item["path"] for item in provenance], project=project or "", status="ok"))
    except OSError as exc:
        return _error("write_failed", str(exc))

    return {
        "ok": True,
        "path": relative_path.as_posix(),
        "absolute_path": str(target),
        "created": True,
        "redacted_count": redacted_count,
        "indexed": None,
    }

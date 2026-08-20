from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from wiki.atomic_file import atomic_write_text
from wiki.repair_messages import page_operation_repair_message
from wiki.wiki_io import is_manual_page, split_frontmatter
from wiki.wiki_paths import (
    ARCHIVES_DIR,
    ARCHIVES_LOG_PATH,
    WikiPathError,
    filesystem_path,
    projection_files_initialized,
    validate_wiki_page_path,
)
from wiki.wikilinks import format_wikilink

_TOP_LEVEL_GROUPS = (
    ("Projects", Path("wiki/projects")),
    ("Concepts", Path("wiki/concepts")),
    ("Entities", Path("wiki/entities")),
    ("Archives", ARCHIVES_DIR),
)

_PROJECT_GROUPS = (
    ("Specs", "specs"),
    ("Plans", "plans"),
    ("Architecture", "architecture"),
    ("Pipelines", "pipelines"),
    ("Troubleshooting", "troubleshooting"),
    ("Researches", "researches"),
)
def refresh_navigation(
    vault_root: str | Path,
    changed_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh generated navigation pages.

    ``changed_path`` is the durable page mutation hint.  The default ``None``
    path deliberately keeps the existing full rebuild behavior for explicit
    maintenance callers; page mutation projections use the incremental path.
    """

    root = filesystem_path(vault_root)
    if changed_path is not None:
        return _refresh_navigation_incremental(root, changed_path)
    return _refresh_navigation_full(root)


def _refresh_navigation_full(root: Path) -> dict[str, Any]:
    # Use the extended-length form so deep source trees over MAX_PATH are
    # walked and indexed instead of being skipped.
    written: list[str] = []
    changed: list[str] = []
    projects_root = root / "wiki" / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
        result = _write_project_index(root, project_dir.name, changed=changed)
        if result is not None:
            return result
        written.append((Path("wiki/projects") / project_dir.name / "index.md").as_posix())
    for writer in (_write_concepts_indexes, _write_entities_index):
        result = writer(root, changed=changed)
        if isinstance(result, dict):
            return result
        written.extend(result)
    result = _write_top_index(root, changed=changed)
    if result is not None:
        return result
    written.append("wiki/index.md")
    return {
        "ok": True,
        "written": written,
        "changed": changed,
        "batch": {"kind": "navigation", "affected_count": len(written)},
    }


def _refresh_navigation_incremental(root: Path, changed_path: str | Path) -> dict[str, Any]:
    try:
        relative = validate_wiki_page_path(changed_path, allow_navigation_index=False)
    except WikiPathError:
        return {
            "ok": False,
            "code": "changed_path_invalid",
            "error": "changed path must identify an eligible Wiki page",
        }

    wiki_root = root / "wiki"
    if not wiki_root.is_dir():
        return _incremental_missing("Wiki navigation root", wiki_root, root)

    parts = relative.parts
    if len(parts) < 3 or parts[0].casefold() != "wiki":
        return {
            "ok": False,
            "code": "changed_path_invalid",
            "error": "changed path must stay inside the Wiki page tree",
        }

    if parts[1].casefold() == "projects":
        return _refresh_project_navigation_incremental(root, relative)
    if parts[1].casefold() == "concepts":
        return _refresh_concept_navigation_incremental(root, relative)
    if parts[1].casefold() == "entities":
        return _refresh_entity_navigation_incremental(root, relative)
    return {
        "ok": False,
        "code": "changed_path_invalid",
        "error": "changed path is outside the navigable Wiki categories",
    }


def _refresh_project_navigation_incremental(root: Path, relative: Path) -> dict[str, Any]:
    project = relative.parts[2]
    project_dir = root / "wiki" / "projects" / project
    project_index = project_dir / "index.md"
    top_index = root / "wiki" / "index.md"
    written: list[str] = []
    changed: list[str] = []
    bootstrap = not projection_files_initialized(root)

    if project_dir.is_dir():
        index_existed = project_index.is_file()
        if index_existed is False and top_index.is_file() and _has_wikilink(top_index, f"projects/{project}/index.md"):
            return _incremental_missing("project navigation index", project_index, root)
        if not top_index.is_file() and not bootstrap:
            return _incremental_missing("top navigation index", top_index, root)
        result = _write_project_index(root, project, changed=changed)
        if result is not None:
            return result
        written.append(project_index.relative_to(root).as_posix())
        top_entry_missing = not _has_wikilink(top_index, f"projects/{project}/index.md")
        if not index_existed or top_entry_missing or bootstrap and not top_index.is_file():
            result = _write_top_index(root, changed=changed)
            if result is not None:
                return result
            if "wiki/index.md" not in written:
                written.append("wiki/index.md")
        return _navigation_success(written, changed)

    if not top_index.is_file():
        return _incremental_missing("project navigation scope", top_index, root)
    if not _has_wikilink(top_index, f"projects/{project}/index.md"):
        return _incremental_missing("project navigation scope", top_index, root)
    result = _write_top_index(root, changed=changed)
    if result is not None:
        return result
    written.append("wiki/index.md")
    return _navigation_success(written, changed)


def _refresh_concept_navigation_incremental(root: Path, relative: Path) -> dict[str, Any]:
    concepts_root = root / "wiki" / "concepts"
    concepts_index = concepts_root / "index.md"
    written: list[str] = []
    changed: list[str] = []
    bootstrap = not projection_files_initialized(root)

    if not concepts_root.is_dir():
        return _incremental_missing("concept navigation scope", concepts_root, root)

    # A page directly below wiki/concepts is an entry in the aggregate index;
    # a page below a domain only affects that domain's index.
    if len(relative.parts) == 3:
        if not concepts_index.is_file() and not bootstrap:
            return _incremental_missing("concept navigation index", concepts_index, root)
        result = _patch_page_listing_index(
            root,
            concepts_index,
            Path(*relative.parts[2:]),
            base_dir=concepts_root,
            group="concepts",
            changed=changed,
        )
        if result is not None:
            return result
        written.append(concepts_index.relative_to(root).as_posix())
        result = _ensure_top_category_link(root, "concepts/index.md", written, changed, bootstrap=bootstrap)
        if result is not None:
            return result
        return _navigation_success(written, changed)

    domain = relative.parts[2]
    domain_dir = concepts_root / domain
    domain_index = domain_dir / "index.md"
    if domain_dir.is_dir():
        index_existed = domain_index.is_file()
        if not concepts_index.is_file() and not bootstrap:
            return _incremental_missing("concept navigation index", concepts_index, root)
        if not index_existed and concepts_index.is_file() and _has_wikilink(concepts_index, f"{domain}/index.md"):
            return _incremental_missing("concept domain index", domain_index, root)
        result = _write_listing_index(
            root,
            domain_index,
            title=domain,
            entries=_page_entries(domain_dir, base_dir=domain_dir),
            frontmatter={"type": "index", "generated": True, "domain": domain},
            changed=changed,
        )
        if result is not None:
            return result
        written.append(domain_index.relative_to(root).as_posix())
        if not index_existed or not _has_wikilink(concepts_index, f"{domain}/index.md"):
            result = _ensure_concepts_index(root, changed=changed)
            if result is not None:
                return result
            if "wiki/concepts/index.md" not in written:
                written.append("wiki/concepts/index.md")
        result = _ensure_top_category_link(root, "concepts/index.md", written, changed, bootstrap=bootstrap)
        if result is not None:
            return result
        return _navigation_success(written, changed)

    if not concepts_index.is_file() or not _has_wikilink(concepts_index, f"{domain}/index.md"):
        return _incremental_missing("concept navigation scope", concepts_index, root)
    result = _patch_named_listing_entry(
        root,
        concepts_index,
        f"{domain}/index.md",
        None,
        group="concepts",
        changed=changed,
    )
    if result is not None:
        return result
    written.append(concepts_index.relative_to(root).as_posix())
    result = _ensure_top_category_link(root, "concepts/index.md", written, changed, bootstrap=bootstrap)
    if result is not None:
        return result
    return _navigation_success(written, changed)


def _refresh_entity_navigation_incremental(root: Path, relative: Path) -> dict[str, Any]:
    entities_root = root / "wiki" / "entities"
    entities_index = root / "wiki" / "entities" / "index.md"
    changed: list[str] = []
    written: list[str] = []
    bootstrap = not projection_files_initialized(root)
    if not entities_root.is_dir():
        return _incremental_missing("entity navigation scope", entities_root, root)
    if not entities_index.is_file() and not bootstrap:
        return _incremental_missing("entity navigation index", entities_index, root)
    if not entities_index.is_file():
        result = _write_section_index(root, Path("wiki/entities"), "Entities", changed=changed)
        if isinstance(result, dict):
            return result
        written.extend(result)
    else:
        result = _patch_page_listing_index(
            root,
            entities_index,
            Path(*relative.parts[2:]),
            base_dir=entities_root,
            group="all",
            changed=changed,
        )
        if result is not None:
            return result
        written.append(entities_index.relative_to(root).as_posix())
    result = _ensure_top_category_link(root, "entities/index.md", written, changed, bootstrap=bootstrap)
    if result is not None:
        return result
    return _navigation_success(written, changed)


def _navigation_success(written: list[str], changed: list[str]) -> dict[str, Any]:
    return {
        "ok": True,
        "written": written,
        "changed": changed,
        "batch": {"kind": "navigation", "affected_count": len(written)},
    }


def _incremental_missing(scope: str, target: Path, root: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "incremental_navigation_index_missing",
        "path": _relative_path(root, target),
        "error": page_operation_repair_message(f"{scope} is missing"),
    }


def _ensure_top_category_link(
    root: Path,
    entry_target: str,
    written: list[str],
    changed: list[str],
    *,
    bootstrap: bool,
) -> dict[str, Any] | None:
    target = root / "wiki" / "index.md"
    if not target.is_file() and not bootstrap:
        return _incremental_missing("top navigation index", target, root)
    if target.is_file() and _has_wikilink(target, entry_target):
        return None
    result = _write_top_index(root, changed=changed)
    if result is not None:
        return result
    relative = target.relative_to(root).as_posix()
    if relative not in written:
        written.append(relative)
    return None


def _relative_path(root: Path, target: Path) -> str:
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def rebuild_retrieval_index(vault_root: str | Path) -> dict[str, object]:
    """Explicit administrator/initialization boundary for full retrieval builds."""

    from wiki.ingest_service import sync_retrieval_index

    return sync_retrieval_index(filesystem_path(vault_root), full_build=True)


def refresh_indexes(vault_root: str | Path) -> dict[str, Any]:
    """Compatibility maintenance command: refresh navigation and rebuild indexes.

    Page mutation projections call :func:`refresh_navigation` directly. This
    combined wrapper remains for explicit initialization and existing CLI/test
    callers that intentionally request a complete maintenance refresh.
    """

    navigation = refresh_navigation(vault_root)
    if not navigation.get("ok"):
        return navigation
    retrieval = rebuild_retrieval_index(vault_root)
    return {**navigation, "retrieval_index": retrieval}


def _write_top_index(
    root: Path,
    *,
    changed: list[str] | None = None,
) -> dict[str, Any] | None:
    target = root / "wiki" / "index.md"
    if is_manual_page(target):
        return _manual_page_error(target, root)
    lines = ["---", "type: index", "generated: true", "---", "", "# Index", ""]
    for title, relative_dir in _TOP_LEVEL_GROUPS:
        lines.append(f"## {title}")
        entries = _top_level_entries(root, title, relative_dir)
        lines.extend(entries or ["- 无"])
        lines.append("")
    _write_rendered_index(target, "\n".join(lines).rstrip() + "\n", root=root, changed=changed)
    return None


def _write_project_index(
    root: Path,
    project: str,
    *,
    changed: list[str] | None = None,
) -> dict[str, Any] | None:
    project_dir = root / "wiki" / "projects" / project
    target = project_dir / "index.md"
    if is_manual_page(target):
        return _manual_page_error(target, root)
    lines = ["---", "type: project_index", "generated: true", f"project: {project}", "---", "", f"# {project}", ""]
    for heading, subdir in _PROJECT_GROUPS:
        lines.append(f"## {heading}")
        entries = _page_entries(project_dir / subdir, base_dir=project_dir)
        lines.extend(entries or ["- 无"])
        lines.append("")
    _write_rendered_index(target, "\n".join(lines).rstrip() + "\n", root=root, changed=changed)
    return None


def _top_level_entries(root: Path, title: str, relative_dir: Path) -> list[str]:
    directory = root / relative_dir
    if not directory.exists() and title != "Archives":
        return []
    if title == "Projects":
        entries = []
        for project_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
            rel = Path("projects") / project_dir.name / "index.md"
            entries.append(f"- {format_wikilink(rel.as_posix(), project_dir.name)}")
        return entries
    if title == "Archives":
        log_path = root / ARCHIVES_LOG_PATH
        return [f"- {format_wikilink(ARCHIVES_LOG_PATH.as_posix(), 'Archives Log')}"] if log_path.exists() else []
    index_path = relative_dir / "index.md"
    return [f"- {format_wikilink(index_path.relative_to('wiki').as_posix(), title)}"] if (root / index_path).exists() else []


def _write_concepts_indexes(
    root: Path,
    *,
    changed: list[str] | None = None,
) -> list[str] | dict[str, Any]:
    concepts_root = root / "wiki" / "concepts"
    concepts_root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for domain_dir in sorted(path for path in concepts_root.iterdir() if path.is_dir()):
        target = domain_dir / "index.md"
        result = _write_listing_index(
            root,
            target,
            title=domain_dir.name,
            entries=_page_entries(domain_dir, base_dir=domain_dir),
            frontmatter={"type": "index", "generated": True, "domain": domain_dir.name},
            changed=changed,
        )
        if result is not None:
            return result
        written.append(target.relative_to(root).as_posix())
    entries = []
    for domain_dir in sorted(path for path in concepts_root.iterdir() if path.is_dir()):
        if (domain_dir / "index.md").exists():
            rel = Path(domain_dir.name) / "index.md"
            entries.append(f"- {format_wikilink(rel.as_posix(), domain_dir.name)}")
    entries.extend(_direct_page_entries(concepts_root, base_dir=concepts_root))
    result = _write_listing_index(
        root,
        concepts_root / "index.md",
        "Concepts",
        entries,
        {"type": "index", "generated": True},
        changed=changed,
    )
    if result is not None:
        return result
    written.append("wiki/concepts/index.md")
    return written



def _write_entities_index(
    root: Path,
    *,
    changed: list[str] | None = None,
) -> list[str] | dict[str, Any]:
    return _write_section_index(root, Path("wiki/entities"), "Entities", changed=changed)


def _write_section_index(
    root: Path,
    relative_dir: Path,
    title: str,
    *,
    changed: list[str] | None = None,
) -> list[str] | dict[str, Any]:
    directory = root / relative_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "index.md"
    result = _write_listing_index(
        root,
        target,
        title,
        _page_entries(directory, base_dir=directory),
        {"type": "index", "generated": True},
        changed=changed,
    )
    if result is not None:
        return result
    return [target.relative_to(root).as_posix()]


def _write_listing_index(
    root: Path,
    target: Path,
    title: str,
    entries: list[str],
    frontmatter: dict[str, Any],
    *,
    changed: list[str] | None = None,
) -> dict[str, Any] | None:
    if is_manual_page(target):
        return _manual_page_error(target, root)
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    lines = ["---", *yaml_text.splitlines(), "---", "", f"# {title}", "", *(entries or ["- 无"])]
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_rendered_index(target, "\n".join(lines).rstrip() + "\n", root=root, changed=changed)
    return None


def _ensure_concepts_index(root: Path, *, changed: list[str]) -> dict[str, Any] | None:
    """Bootstrap the aggregate concepts index without scanning page bodies."""

    concepts_root = root / "wiki" / "concepts"
    concepts_root.mkdir(parents=True, exist_ok=True)
    target = concepts_root / "index.md"
    if is_manual_page(target):
        return _manual_page_error(target, root)
    entries: list[str] = []
    for domain_dir in sorted(path for path in concepts_root.iterdir() if path.is_dir()):
        if (domain_dir / "index.md").is_file():
            entries.append(f"- {format_wikilink((Path(domain_dir.name) / 'index.md').as_posix(), domain_dir.name)}")
    entries.extend(_direct_page_entries(concepts_root, base_dir=concepts_root))
    return _write_listing_index(
        root,
        target,
        "Concepts",
        entries,
        {"type": "index", "generated": True},
        changed=changed,
    )


def _patch_page_listing_index(
    root: Path,
    target: Path,
    changed_relative: Path,
    *,
    base_dir: Path,
    group: str,
    changed: list[str],
) -> dict[str, Any] | None:
    if not target.is_file():
        if group == "concepts":
            return _ensure_concepts_index(root, changed=changed)
        return _incremental_missing("entity navigation index", target, root)
    if is_manual_page(target):
        return _manual_page_error(target, root)

    page = base_dir / changed_relative
    line = _render_page_entry(page, base_dir=base_dir) if page.is_file() else None
    return _patch_named_listing_entry(
        root,
        target,
        changed_relative.as_posix(),
        line,
        group=group,
        changed=changed,
    )


def _patch_named_listing_entry(
    root: Path,
    target: Path,
    entry_target: str,
    line: str | None,
    *,
    group: str,
    changed: list[str],
) -> dict[str, Any] | None:
    if not target.is_file():
        return _incremental_missing("incremental navigation index", target, root)
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    entry_indexes = [index for index, value in enumerate(lines) if _wikilink_target(value) is not None]
    placeholder_indexes = [index for index, value in enumerate(lines) if value.strip() == "- 无"]
    replace_indexes = [*entry_indexes, *placeholder_indexes]
    if replace_indexes:
        first = min(replace_indexes)
        last = max(replace_indexes)
        prefix = lines[:first]
        suffix = lines[last + 1 :]
    else:
        prefix = lines
        suffix = []

    entries = [(target_value, value) for value in lines if (target_value := _wikilink_target(value)) is not None]
    entries = [(target_value, value) for target_value, value in entries if target_value != entry_target]
    if line is not None:
        entries.append((entry_target, line))
    entries.sort(key=lambda item: _entry_sort_key(item[0], group))
    rendered_entries = [value for _, value in entries] or ["- 无"]
    rendered = "\n".join([*prefix, *rendered_entries, *suffix]).rstrip() + "\n"
    _write_rendered_index(target, rendered, root=root, changed=changed)
    return None


def _entry_sort_key(target: str, group: str) -> tuple[int, str]:
    # The full concepts renderer puts domain index links before direct pages;
    # preserve that order while keeping each group deterministic.
    if group == "concepts":
        return (0 if "/" in target else 1, target)
    return (0, target)


def _wikilink_target(line: str) -> str | None:
    value = line.strip()
    if not value.startswith("- [["):
        return None
    closing = value.find("]]", 4)
    if closing < 0:
        return None
    content = value[4:closing]
    return content.split("|", 1)[0]


def _has_wikilink(target: Path, entry_target: str) -> bool:
    if not target.is_file():
        return False
    return any(_wikilink_target(line) == entry_target for line in target.read_text(encoding="utf-8").splitlines())


def _write_rendered_index(
    target: Path,
    text: str,
    *,
    root: Path,
    changed: list[str] | None,
) -> None:
    """Atomically write a generated index only when its UTF-8 bytes change."""

    encoded = text.encode("utf-8")
    if target.is_file() and target.read_bytes() == encoded:
        return
    atomic_write_text(target, text)
    if changed is not None:
        changed.append(target.relative_to(root).as_posix())


def _page_entries(directory: Path, base_dir: Path) -> list[str]:
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.rglob("*.md")):
        if path.name == "index.md":
            continue
        entries.append(_render_page_entry(path, base_dir=base_dir))
    return entries


def _direct_page_entries(directory: Path, base_dir: Path) -> list[str]:
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "index.md":
            continue
        entries.append(_render_page_entry(path, base_dir=base_dir))
    return entries


def _render_page_entry(path: Path, *, base_dir: Path) -> str:
    frontmatter, heading = _read_page_metadata(path)
    title = str(frontmatter.get("title") or heading or path.stem)
    summary = str(frontmatter.get("summary") or "").strip()
    rel = path.relative_to(base_dir).as_posix()
    line = f"- {format_wikilink(rel, title)}"
    if summary:
        line += f" — {summary}"
    return line


def _read_page_metadata(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    heading = ""
    for line in body.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            break
    return frontmatter, heading


def _manual_page_error(path: Path, root: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "manual_page_exists",
        "path": path.relative_to(root).as_posix(),
        "error": "refusing to overwrite non-generated wiki page",
    }

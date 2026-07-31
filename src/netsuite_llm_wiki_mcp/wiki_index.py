from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter
from netsuite_llm_wiki_mcp.wiki_limits import (
    MAX_NAVIGATION_ENTRIES,
    TARGET_PAGE_BYTES,
    partition_rendered_units,
    render_units,
)

_TOP_LEVEL_GROUPS = (
    ("Projects", Path("wiki/projects")),
    ("Concepts", Path("wiki/concepts")),
    ("Sources", Path("wiki/sources")),
    ("Entities", Path("wiki/entities")),
    ("Archives", Path("wiki/archives")),
)

_PROJECT_GROUPS = (
    ("Specs", "specs"),
    ("Plans", "plans"),
    ("Architecture", "architecture"),
    ("Pipelines", "pipelines"),
    ("Troubleshooting", "troubleshooting"),
    ("Researches", "researches"),
)
_SOURCES_NAVIGATION_PAGE_RE = re.compile(r"^index(?:-\d{2,})?\.md$")


def refresh_indexes(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root)
    written = []
    projects_root = root / "wiki" / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
        result = _write_project_index(root, project_dir.name)
        if result is not None:
            return result
        written.append((Path("wiki/projects") / project_dir.name / "index.md").as_posix())
    for writer in (_write_concepts_indexes, _write_sources_index, _write_entities_index):
        result = writer(root)
        if isinstance(result, dict):
            return result
        written.extend(result)
    result = _write_top_index(root)
    if result is not None:
        return result
    written.append("wiki/index.md")
    # Navigation generation is an explicit maintenance/write boundary, so it
    # is safe to rebuild the FTS projection here. Query traffic never does it.
    from netsuite_llm_wiki_mcp.ingest_service import sync_retrieval_index

    try:
        retrieval = sync_retrieval_index(root)
    except Exception as exc:
        return {"ok": False, "code": "retrieval_index_stale", "written": written, "error": str(exc)}
    return {"ok": True, "written": written, "retrieval_index": retrieval}


def _write_top_index(root: Path) -> dict[str, Any] | None:
    target = root / "wiki" / "index.md"
    if _is_manual_page(target):
        return _manual_page_error(target, root)
    lines = ["---", "type: index", "generated: true", "---", "", "# Index", ""]
    for title, relative_dir in _TOP_LEVEL_GROUPS:
        lines.append(f"## {title}")
        entries = _top_level_entries(root, title, relative_dir)
        lines.extend(entries or ["- 无"])
        lines.append("")
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return None


def _write_project_index(root: Path, project: str) -> dict[str, Any] | None:
    project_dir = root / "wiki" / "projects" / project
    target = project_dir / "index.md"
    if _is_manual_page(target):
        return _manual_page_error(target, root)
    lines = ["---", "type: project_index", "generated: true", f"project: {project}", "---", "", f"# {project}", ""]
    for heading, subdir in _PROJECT_GROUPS:
        lines.append(f"## {heading}")
        entries = _page_entries(project_dir / subdir, base_dir=project_dir)
        lines.extend(entries or ["- 无"])
        lines.append("")
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return None


def _top_level_entries(root: Path, title: str, relative_dir: Path) -> list[str]:
    directory = root / relative_dir
    if not directory.exists() and title != "Archives":
        return []
    if title == "Projects":
        entries = []
        for project_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
            rel = Path("projects") / project_dir.name / "index.md"
            entries.append(f"- [[{rel.as_posix()}|{project_dir.name}]]")
        return entries
    if title == "Archives":
        log_path = root / "wiki" / "archives" / "log.md"
        return ["- [[archives/log.md|Archives Log]]"] if log_path.exists() else []
    index_path = relative_dir / "index.md"
    return [f"- [[{index_path.relative_to('wiki').as_posix()}|{title}]]"] if (root / index_path).exists() else []


def _write_concepts_indexes(root: Path) -> list[str] | dict[str, Any]:
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
        )
        if result is not None:
            return result
        written.append(target.relative_to(root).as_posix())
    entries = []
    for domain_dir in sorted(path for path in concepts_root.iterdir() if path.is_dir()):
        if (domain_dir / "index.md").exists():
            rel = Path(domain_dir.name) / "index.md"
            entries.append(f"- [[{rel.as_posix()}|{domain_dir.name}]]")
    entries.extend(_direct_page_entries(concepts_root, base_dir=concepts_root))
    result = _write_listing_index(root, concepts_root / "index.md", "Concepts", entries, {"type": "index", "generated": True})
    if result is not None:
        return result
    written.append("wiki/concepts/index.md")
    return written



def _write_sources_index(root: Path) -> list[str] | dict[str, Any]:
    sources_root = root / "wiki" / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)
    contents: dict[Path, str] = {}
    result = _collect_sources_navigation(root, sources_root, title="Sources", contents=contents)
    if result is not None:
        return result
    return _write_generated_navigation_pages(root, contents)


def _collect_sources_navigation(
    root: Path,
    directory: Path,
    title: str,
    contents: dict[Path, str],
) -> dict[str, Any] | None:
    """Build bounded navigation pages for one sources directory only.

    Source-index leaves own their existing ``index.md`` / ``_entries*.md``
    navigation.  This writer only creates the directory hierarchy around them,
    so a high-volume source tree cannot be flattened back into its root page.
    """

    target = directory / "index.md"
    if _is_source_index_leaf(target):
        return None
    if _is_manual_page(target):
        return _manual_page_error(target, root)

    entries: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.md")):
        if _is_sources_navigation_page(path):
            continue
        frontmatter, heading = _read_page_metadata(path)
        page_title = str(frontmatter.get("title") or heading or path.stem)
        summary = str(frontmatter.get("summary") or "").strip()
        line = f"- [[{path.name}|{page_title}]]"
        if summary:
            line += f" — {summary}"
        entries.append((path.name.casefold(), line))

    for child in sorted(path for path in directory.iterdir() if path.is_dir()):
        result = _collect_sources_navigation(root, child, title=child.name, contents=contents)
        if result is not None:
            return result
        child_index = child / "index.md"
        if child_index.exists() or child_index in contents:
            rel = child_index.relative_to(directory).as_posix()
            entries.append((f"{child.name.casefold()}/", f"- [[{rel}|{child.name}]]"))

    pages = _render_sources_navigation_pages(title, [line for _, line in sorted(entries)])
    for number, text in enumerate(pages, 1):
        page = directory / ("index.md" if number == 1 else f"index-{number:02d}.md")
        if _is_manual_page(page):
            return _manual_page_error(page, root)
        contents[page] = text
    return None


def _render_sources_navigation_pages(title: str, entries: list[str]) -> list[str]:
    header = "\n".join(
        [
            "---",
            "type: index",
            "generated: true",
            "navigation: true",
            "---",
            "",
            f"# {title}",
        ]
    )
    placeholder = "← [[index-9999.md|Previous]] · [[index-9999.md|Next]] →"
    units = entries or ["- 无"]
    groups, oversized = partition_rendered_units(
        units,
        header,
        placeholder,
        TARGET_PAGE_BYTES,
        max_units=MAX_NAVIGATION_ENTRIES,
    )
    if oversized:
        raise ValueError("sources navigation entry cannot fit into a bounded page")
    groups = groups or [["- 无"]]
    total = len(groups)
    rendered: list[str] = []
    for number, group in enumerate(groups, 1):
        page_header = header if total == 1 else f"{header}\n\nPage {number}/{total}"
        links: list[str] = []
        if number > 1:
            previous = "index.md" if number == 2 else f"index-{number - 1:02d}.md"
            links.append(f"← [[{previous}|Previous]]")
        if number < total:
            links.append(f"[[index-{number + 1:02d}.md|Next]] →")
        rendered.append(render_units(page_header, group, " · ".join(links)))
    return rendered


def _write_generated_navigation_pages(root: Path, contents: dict[Path, str]) -> list[str]:
    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    new_targets = {target for target in contents if not target.exists()}
    by_directory: dict[Path, set[Path]] = {}
    for target in contents:
        by_directory.setdefault(target.parent, set()).add(target)
    stale_pages = [
        stale
        for directory, keep in by_directory.items()
        for stale in directory.glob("index-*.md")
        if stale not in keep and _is_sources_navigation_page(stale)
    ]
    try:
        for target, text in contents.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(text, encoding="utf-8")
            staged.append((target, temporary))
        for target, temporary in staged:
            if target.exists():
                backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
                target.replace(backup)
                backups.append((target, backup))
            temporary.replace(target)
        for stale in stale_pages:
            backup = stale.with_name(f".{stale.name}.{uuid.uuid4().hex}.bak")
            stale.replace(backup)
            backups.append((stale, backup))
    except BaseException:
        # Files are individually atomic, but the navigation set is not.  Move
        # replaced pages back before surfacing a commit failure so callers never
        # observe a mixture of old and new pagination links.
        for target in new_targets:
            if target.exists():
                target.unlink()
        for target, backup in reversed(backups):
            if target.exists():
                target.unlink()
            if backup.exists():
                backup.replace(target)
        raise
    else:
        for _, backup in backups:
            backup.unlink(missing_ok=True)
    finally:
        for _, temporary in staged:
            if temporary.exists():
                temporary.unlink()
    return [target.relative_to(root).as_posix() for target in sorted(contents)]


def _is_source_index_leaf(path: Path) -> bool:
    if not path.exists():
        return False
    frontmatter, _ = _read_page_metadata(path)
    return frontmatter.get("type") == "source_index"


def _is_sources_navigation_page(path: Path) -> bool:
    if not _SOURCES_NAVIGATION_PAGE_RE.match(path.name):
        return False
    if path.name == "index.md":
        return True
    if not path.exists():
        return False
    frontmatter, _ = _read_page_metadata(path)
    return frontmatter.get("generated") is True and frontmatter.get("navigation") is True



def _write_entities_index(root: Path) -> list[str] | dict[str, Any]:
    return _write_section_index(root, Path("wiki/entities"), "Entities")


def _write_section_index(root: Path, relative_dir: Path, title: str) -> list[str] | dict[str, Any]:
    directory = root / relative_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "index.md"
    result = _write_listing_index(root, target, title, _page_entries(directory, base_dir=directory), {"type": "index", "generated": True})
    if result is not None:
        return result
    return [target.relative_to(root).as_posix()]


def _write_listing_index(root: Path, target: Path, title: str, entries: list[str], frontmatter: dict[str, Any]) -> dict[str, Any] | None:
    if _is_manual_page(target):
        return _manual_page_error(target, root)
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    lines = ["---", *yaml_text.splitlines(), "---", "", f"# {title}", "", *(entries or ["- 无"])]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return None


def _page_entries(directory: Path, base_dir: Path) -> list[str]:
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.rglob("*.md")):
        if path.name == "index.md":
            continue
        frontmatter, heading = _read_page_metadata(path)
        title = str(frontmatter.get("title") or heading or path.stem)
        summary = str(frontmatter.get("summary") or "").strip()
        rel = path.relative_to(base_dir).as_posix()
        line = f"- [[{rel}|{title}]]"
        if summary:
            line += f" — {summary}"
        entries.append(line)
    return entries


def _direct_page_entries(directory: Path, base_dir: Path) -> list[str]:
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "index.md":
            continue
        frontmatter, heading = _read_page_metadata(path)
        title = str(frontmatter.get("title") or heading or path.stem)
        summary = str(frontmatter.get("summary") or "").strip()
        rel = path.relative_to(base_dir).as_posix()
        line = f"- [[{rel}|{title}]]"
        if summary:
            line += f" — {summary}"
        entries.append(line)
    return entries


def _read_page_metadata(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    heading = ""
    for line in body.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            break
    return frontmatter, heading


def _is_manual_page(path: Path) -> bool:
    if not path.exists():
        return False
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter.get("generated") is not True


def _manual_page_error(path: Path, root: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "manual_page_exists",
        "path": path.relative_to(root).as_posix(),
        "error": "refusing to overwrite non-generated wiki page",
    }

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from wiki.atomic_file import FaultBarrier, atomic_write_text
from wiki.wiki_io import is_manual_page, split_frontmatter
from wiki.wiki_paths import ARCHIVES_DIR, ARCHIVES_LOG_PATH, filesystem_path
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
def refresh_navigation(vault_root: str | Path, *, fault: FaultBarrier | None = None) -> dict[str, Any]:
    # Use the extended-length form so deep source trees over MAX_PATH are
    # walked and indexed instead of being skipped.
    root = filesystem_path(vault_root)
    written: list[str] = []
    changed: list[str] = []
    projects_root = root / "wiki" / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
        result = _write_project_index(root, project_dir.name, fault=fault, changed=changed)
        if result is not None:
            return result
        written.append((Path("wiki/projects") / project_dir.name / "index.md").as_posix())
    for writer in (_write_concepts_indexes, _write_entities_index):
        result = writer(root, fault=fault, changed=changed)
        if isinstance(result, dict):
            return result
        written.extend(result)
    result = _write_top_index(root, fault=fault, changed=changed)
    if result is not None:
        return result
    written.append("wiki/index.md")
    return {
        "ok": True,
        "written": written,
        "changed": changed,
        "batch": {"kind": "navigation", "affected_count": len(written)},
    }


def rebuild_retrieval_index(vault_root: str | Path) -> dict[str, object]:
    """Explicit administrator/initialization boundary for full retrieval builds."""

    from wiki.ingest_service import sync_retrieval_index

    return sync_retrieval_index(filesystem_path(vault_root), full_build=True)


def refresh_indexes(vault_root: str | Path, *, fault: FaultBarrier | None = None) -> dict[str, Any]:
    """Compatibility maintenance command: refresh navigation and rebuild indexes.

    Page mutation projections call :func:`refresh_navigation` directly. This
    combined wrapper remains for explicit initialization and existing CLI/test
    callers that intentionally request a complete maintenance refresh.
    """

    navigation = refresh_navigation(vault_root, fault=fault)
    if not navigation.get("ok"):
        return navigation
    retrieval = rebuild_retrieval_index(vault_root)
    return {**navigation, "retrieval_index": retrieval}


def _write_top_index(
    root: Path,
    *,
    fault: FaultBarrier | None = None,
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
    _write_rendered_index(target, "\n".join(lines).rstrip() + "\n", root=root, fault=fault, changed=changed)
    return None


def _write_project_index(
    root: Path,
    project: str,
    *,
    fault: FaultBarrier | None = None,
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
    _write_rendered_index(target, "\n".join(lines).rstrip() + "\n", root=root, fault=fault, changed=changed)
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
    fault: FaultBarrier | None = None,
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
            fault=fault,
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
        fault=fault,
        changed=changed,
    )
    if result is not None:
        return result
    written.append("wiki/concepts/index.md")
    return written



def _write_entities_index(
    root: Path,
    *,
    fault: FaultBarrier | None = None,
    changed: list[str] | None = None,
) -> list[str] | dict[str, Any]:
    return _write_section_index(root, Path("wiki/entities"), "Entities", fault=fault, changed=changed)


def _write_section_index(
    root: Path,
    relative_dir: Path,
    title: str,
    *,
    fault: FaultBarrier | None = None,
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
        fault=fault,
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
    fault: FaultBarrier | None = None,
    changed: list[str] | None = None,
) -> dict[str, Any] | None:
    if is_manual_page(target):
        return _manual_page_error(target, root)
    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    lines = ["---", *yaml_text.splitlines(), "---", "", f"# {title}", "", *(entries or ["- 无"])]
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_rendered_index(target, "\n".join(lines).rstrip() + "\n", root=root, fault=fault, changed=changed)
    return None


def _write_rendered_index(
    target: Path,
    text: str,
    *,
    root: Path,
    fault: FaultBarrier | None,
    changed: list[str] | None,
) -> None:
    """Atomically write a generated index only when its UTF-8 bytes change."""

    encoded = text.encode("utf-8")
    if target.is_file() and target.read_bytes() == encoded:
        return
    atomic_write_text(target, text, fault=fault)
    if changed is not None:
        changed.append(target.relative_to(root).as_posix())


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
        line = f"- {format_wikilink(rel, title)}"
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
        line = f"- {format_wikilink(rel, title)}"
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


def _manual_page_error(path: Path, root: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "manual_page_exists",
        "path": path.relative_to(root).as_posix(),
        "error": "refusing to overwrite non-generated wiki page",
    }

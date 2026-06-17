from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter

_TOP_LEVEL_GROUPS = (
    ("Projects", Path("wiki/projects")),
    ("Concepts", Path("wiki/concepts")),
    ("Chatlog", Path("wiki/chatlog")),
    ("Sources", Path("wiki/sources")),
    ("Queries", Path("wiki/queries")),
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
    for writer in (_write_concepts_indexes, _write_chatlog_index, _write_sources_index, _write_queries_index, _write_entities_index):
        result = writer(root)
        if isinstance(result, dict):
            return result
        written.extend(result)
    result = _write_top_index(root)
    if result is not None:
        return result
    written.append("wiki/index.md")
    return {"ok": True, "written": written}


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


def _write_chatlog_index(root: Path) -> list[str] | dict[str, Any]:
    return _write_section_index(root, Path("wiki/chatlog"), "Chatlog")


def _write_sources_index(root: Path) -> list[str] | dict[str, Any]:
    return _write_section_index(root, Path("wiki/sources"), "Sources")


def _write_queries_index(root: Path) -> list[str] | dict[str, Any]:
    return _write_section_index(root, Path("wiki/queries"), "Queries")


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

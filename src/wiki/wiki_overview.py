from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki.atomic_file import FaultBarrier, atomic_write_text
from wiki.wiki_io import is_manual_page, split_frontmatter
from wiki.wiki_log import read_recent_log_entries
from wiki.wiki_paths import filesystem_path


def refresh_overview(vault_root: str | Path, *, fault: FaultBarrier | None = None) -> dict[str, Any]:
    # Extended-length form keeps deep source subtrees over MAX_PATH countable.
    root = filesystem_path(vault_root)
    wiki_root = root / "wiki"
    target = wiki_root / "overview.md"
    if is_manual_page(target):
        return {
            "ok": False,
            "code": "manual_page_exists",
            "path": target.relative_to(root).as_posix(),
            "error": "refusing to overwrite non-generated wiki page",
        }
    projects = [path for path in (wiki_root / "projects").iterdir() if path.is_dir()] if (wiki_root / "projects").exists() else []
    markdown_pages = [path for path in wiki_root.rglob("*.md") if path.name not in {"index.md", "log.md", "overview.md"}]
    generated = 0
    manual = 0
    for path in markdown_pages:
        frontmatter = _read_frontmatter(path)
        if frontmatter.get("generated") is True:
            generated += 1
        else:
            manual += 1

    recent = read_recent_log_entries(root, limit=5)
    lines = [
        "---",
        "type: overview",
        "generated: true",
        "---",
        "",
        "# Overview",
        "",
        "## Counts",
        f"- Projects: {len(projects)}",
        f"- Generated pages: {generated}",
        f"- Manual pages: {manual}",
        "",
        "## Recent Log Entries",
        *(recent or ["- 无"]),
        "",
    ]
    atomic_write_text(target, "\n".join(lines), fault=fault)
    return {"ok": True, "path": "wiki/overview.md"}


def _read_frontmatter(path: Path) -> dict[str, Any]:
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter

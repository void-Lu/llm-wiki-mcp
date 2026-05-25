from __future__ import annotations

from pathlib import Path
from typing import Any

from netsuite_rag_mcp.wiki_io import split_frontmatter
from netsuite_rag_mcp.wiki_log import read_recent_log_entries


def refresh_overview(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root)
    wiki_root = root / "wiki"
    target = wiki_root / "overview.md"
    if _is_manual_page(target):
        return {
            "ok": False,
            "code": "manual_page_exists",
            "path": target.relative_to(root).as_posix(),
            "error": "refusing to overwrite non-generated wiki page",
        }
    projects = [path for path in (wiki_root / "projects").iterdir() if path.is_dir()] if (wiki_root / "projects").exists() else []
    markdown_pages = [path for path in wiki_root.rglob("*.md") if path.name not in {"index.md", "log.md", "overview.md"}]
    source_pages = [path for path in (wiki_root / "sources").rglob("*.md")] if (wiki_root / "sources").exists() else []
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
        f"- Source pages: {len(source_pages)}",
        f"- Generated pages: {generated}",
        f"- Manual pages: {manual}",
        "",
        "## Recent Log Entries",
        *(recent or ["- 无"]),
        "",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "path": "wiki/overview.md"}


def _read_frontmatter(path: Path) -> dict[str, Any]:
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter


def _is_manual_page(path: Path) -> bool:
    if not path.exists():
        return False
    frontmatter = _read_frontmatter(path)
    return frontmatter.get("generated") is not True

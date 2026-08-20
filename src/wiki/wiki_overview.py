from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki.atomic_file import atomic_write_text
from wiki.repair_messages import page_operation_repair_message
from wiki.wiki_io import is_manual_page, render_page, split_frontmatter
from wiki.wiki_log import read_recent_log_entries
from wiki.wiki_paths import (
    WikiPathError,
    filesystem_path,
    projection_files_initialized,
    resolve_within_root,
    validate_wiki_page_path,
)


_OVERVIEW_BOOTSTRAP_BODY = "# Overview"


def refresh_overview(
    vault_root: str | Path,
    changed_path: str | Path | None = None,
    *,
    created: bool | None = None,
) -> dict[str, Any]:
    """Refresh the generated overview.

    ``changed_path`` selects the bounded projection used by page mutations.
    ``created=True`` is the only incremental count hint.  Deletions are not
    incrementally reconciled here; the repair full channel is responsible for
    correcting any resulting count drift.
    """

    root = filesystem_path(vault_root)
    if changed_path is not None:
        return _refresh_overview_incremental(
            root,
            changed_path,
            created=created,
        )
    return _refresh_overview_full(root)


def _refresh_overview_full(root: Path) -> dict[str, Any]:
    # Extended-length form keeps deep source subtrees over MAX_PATH countable.
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
    return _write_overview(target, len(projects), generated, manual, recent)


def _refresh_overview_incremental(
    root: Path,
    changed_path: str | Path,
    *,
    created: bool | None,
) -> dict[str, Any]:
    try:
        relative = validate_wiki_page_path(changed_path, allow_navigation_index=False)
        page = resolve_within_root(root, relative)
    except WikiPathError:
        return {
            "ok": False,
            "code": "changed_path_invalid",
            "error": "changed path must identify an eligible Wiki page",
        }

    wiki_root = root / "wiki"
    target = wiki_root / "overview.md"
    if is_manual_page(target):
        return {
            "ok": False,
            "code": "manual_page_exists",
            "path": target.relative_to(root).as_posix(),
            "error": "refusing to overwrite non-generated wiki page",
        }
    # Navigation runs first during bare-vault repair and may have created only
    # wiki/index.md.  The shared predicate requires the complete projection
    # set; the still-missing log distinguishes that stage from an initialized
    # vault whose overview was later removed and must fail loudly.
    bootstrap = not projection_files_initialized(root) and not (wiki_root / "log.md").is_file()
    if not wiki_root.is_dir():
        return {
            "ok": False,
            "code": "incremental_overview_structure_missing",
            "error": page_operation_repair_message("Wiki root is missing"),
        }

    if not target.is_file() and not bootstrap:
        return {
            "ok": False,
            "code": "incremental_overview_structure_missing",
            "path": "wiki/overview.md",
            "error": page_operation_repair_message("overview structure is missing"),
        }
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    counts = _overview_counts(existing)
    if counts is None and not bootstrap:
        return {
            "ok": False,
            "code": "incremental_overview_structure_missing",
            "path": "wiki/overview.md",
            "error": page_operation_repair_message("overview counters are missing or malformed"),
        }
    projects_root = wiki_root / "projects"
    projects = [path for path in projects_root.iterdir() if path.is_dir()] if projects_root.exists() else []

    current_generated: bool | None = None
    if page.is_file():
        current_generated = _read_frontmatter(page).get("generated") is True
    else:
        return {
            "ok": False,
            "code": "changed_path_missing",
            "error": "changed Wiki page is missing",
        }

    generated, manual = counts or (0, 0)
    # Archive deletion is retrieval-only and does not enter this incremental
    # projection; G1's repair full channel corrects any count drift.
    if created is True:
        if current_generated:
            generated += 1
        else:
            manual += 1

    recent = read_recent_log_entries(root, limit=5)
    return _write_overview(target, len(projects), generated, manual, recent)


def _write_overview(target: Path, projects: int, generated: int, manual: int, recent: list[str]) -> dict[str, Any]:
    lines = [
        "# Overview",
        "",
        "## Counts",
        f"- Projects: {projects}",
        f"- Generated pages: {generated}",
        f"- Manual pages: {manual}",
        "",
        "## Recent Log Entries",
        *(recent or ["- 无"]),
        "",
    ]
    rendered = render_page({"type": "overview", "generated": True}, "\n".join(lines), title_heading=None)
    encoded = rendered.encode("utf-8")
    if not target.is_file() or target.read_bytes() != encoded:
        atomic_write_text(target, rendered)
    return {"ok": True, "path": "wiki/overview.md"}


def _overview_counts(text: str) -> tuple[int, int] | None:
    values: dict[str, int] = {}
    for line in text.splitlines():
        for label in ("Projects", "Generated pages", "Manual pages"):
            prefix = f"- {label}: "
            if line.startswith(prefix):
                try:
                    values[label] = int(line[len(prefix) :])
                except ValueError:
                    return None
    if set(values) == set():
        frontmatter, body = split_frontmatter(text)
        if frontmatter.get("type") == "overview" and frontmatter.get("generated") is True and body.strip() == _OVERVIEW_BOOTSTRAP_BODY:
            return 0, 0
    if set(values) != {"Projects", "Generated pages", "Manual pages"}:
        return None
    return values["Generated pages"], values["Manual pages"]


def _read_frontmatter(path: Path) -> dict[str, Any]:
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter

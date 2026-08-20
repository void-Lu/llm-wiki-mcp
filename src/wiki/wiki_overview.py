from __future__ import annotations

from pathlib import Path
from typing import Any

from wiki.atomic_file import atomic_write_text
from wiki.repair_messages import page_operation_repair_message
from wiki.wiki_io import is_manual_page, split_frontmatter
from wiki.wiki_log import read_recent_log_entries
from wiki.wiki_paths import WikiPathError, filesystem_path, resolve_within_root, validate_wiki_page_path


_OVERVIEW_BOOTSTRAP_BODY = "# Overview"


def refresh_overview(
    vault_root: str | Path,
    changed_path: str | Path | None = None,
    *,
    changed_page_state: str | None = None,
    previous_generated: bool | None = None,
) -> dict[str, Any]:
    """Refresh the generated overview.

    ``changed_path`` selects the bounded projection used by page mutations.
    ``changed_page_state`` is an internal adapter hint for page creation or
    deletion; callers that only update an existing page need to pass only the
    path.  With no path the historical full scan remains available to
    explicit maintenance callers.
    """

    root = filesystem_path(vault_root)
    if changed_path is not None:
        return _refresh_overview_incremental(
            root,
            changed_path,
            changed_page_state=changed_page_state,
            previous_generated=previous_generated,
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
    changed_page_state: str | None,
    previous_generated: bool | None,
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
    bootstrap = _is_uninitialized_vault(root)
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
    elif changed_page_state != "deleted":
        return {
            "ok": False,
            "code": "changed_path_missing",
            "error": "changed Wiki page is missing",
        }

    generated, manual = counts or (0, 0)
    if changed_page_state not in {None, "created", "deleted", "updated"}:
        return {
            "ok": False,
            "code": "changed_page_state_invalid",
            "error": "changed page state must be created, updated, or deleted",
        }
    if changed_page_state == "created":
        if current_generated:
            generated += 1
        else:
            manual += 1
    elif changed_page_state == "deleted":
        if previous_generated is None:
            return {
                "ok": False,
                "code": "changed_page_state_required",
                "error": "deleted page projections require its previous generated state",
            }
        if previous_generated:
            generated = max(0, generated - 1)
        else:
            manual = max(0, manual - 1)
    elif previous_generated is not None and current_generated is not None and previous_generated != current_generated:
        if previous_generated:
            generated = max(0, generated - 1)
        else:
            manual = max(0, manual - 1)
        if current_generated:
            generated += 1
        else:
            manual += 1

    recent = read_recent_log_entries(root, limit=5)
    return _write_overview(target, len(projects), generated, manual, recent)


def _write_overview(target: Path, projects: int, generated: int, manual: int, recent: list[str]) -> dict[str, Any]:
    lines = [
        "---",
        "type: overview",
        "generated: true",
        "---",
        "",
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
    atomic_write_text(target, "\n".join(lines))
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


def _is_uninitialized_vault(root: Path) -> bool:
    wiki_root = root / "wiki"
    # Navigation may bootstrap ``wiki/index.md`` immediately before overview
    # runs in a bare-vault repair.  The durable overview/log pair is the
    # marker for an initialized projection set; a missing one in an otherwise
    # initialized vault remains a repairable structural failure.
    return not any((wiki_root / name).is_file() for name in ("overview.md", "log.md"))


def _read_frontmatter(path: Path) -> dict[str, Any]:
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter

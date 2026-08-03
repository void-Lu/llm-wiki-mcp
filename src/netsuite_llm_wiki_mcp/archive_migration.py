"""One-time, explicit migration away from legacy wiki lifecycle folders."""

from __future__ import annotations

from hashlib import sha256
import json
import shutil
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.archive_service import ArchiveService
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter


_STRUCTURAL = {"index.md", "log.md", "overview.md"}
_IGNORED_LEGACY_PLACEHOLDERS = {".gitkeep", ".DS_Store"}
_LEGACY_ARCHIVE_MARKER = ".llm-wiki/legacy-archive-migration-v2.json"


def _legacy_archive_targets(root: Path) -> list[Path]:
    """Return every historical payload that must leave the active vault."""

    directories = (
        root / "wiki" / "chatlog",
        root / "raw" / "sources" / "chat" / "legacy",
        root / "wiki" / "archives",
    )
    return sorted(
        {
            path
            for directory in directories
            if directory.exists()
            for path in directory.rglob("*")
            if path.is_file() and path.name not in _IGNORED_LEGACY_PLACEHOLDERS
        },
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _remove_legacy_directories(root: Path) -> None:
    """Remove only migration-owned empty/structural legacy directories."""

    for directory in (
        root / "wiki" / "queries",
        root / "wiki" / "chatlog",
        root / "raw" / "sources" / "chat" / "legacy",
        root / "wiki" / "sources" / "chatlog",
        root / "wiki" / "archives",
    ):
        if directory.exists():
            shutil.rmtree(directory)


def plan_legacy_migration(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    moves: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    queries = root / "wiki" / "queries"
    if queries.exists():
        non_structural = [
            path
            for path in queries.rglob("*")
            if path.is_file() and path.name not in _STRUCTURAL and path.name not in _IGNORED_LEGACY_PLACEHOLDERS
        ]
        if non_structural:
            blockers.extend({"code": "legacy_queries_not_empty", "path": path.relative_to(root).as_posix()} for path in non_structural)
    source_indexes = root / "wiki" / "sources" / "chatlog"
    if source_indexes.exists():
        for source in sorted(source_indexes.rglob("*.md")):
            fm, _ = split_frontmatter(source.read_text(encoding="utf-8"))
            sources = fm.get("sources", [])
            values = sources if isinstance(sources, list) else [sources]
            if any(str(value).startswith("raw/sources/chat/") and not (root / str(value)).exists() for value in values):
                blockers.append({"code": "legacy_chat_source_orphan", "path": source.relative_to(root).as_posix()})
    for source in _legacy_archive_targets(root):
        source_name = source.relative_to(root).as_posix()
        moves.append(
            {
                "source": source_name,
                "target": "archives/bundles/<migration-bundle>/" + source_name,
                "hash": "sha256:" + sha256(source.read_bytes()).hexdigest(),
            }
        )
    return {"ok": not blockers, "dry_run": True, "moves": moves, "blockers": blockers, "migration_version": 2}


def apply_legacy_migration(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    plan = plan_legacy_migration(root)
    if not plan["ok"]: return plan
    marker = root / _LEGACY_ARCHIVE_MARKER
    sources = [str(item["source"]) for item in plan["moves"]]
    if marker.exists() and not sources:
        return {"ok": True, "already_migrated": True, "migration_version": 2}

    archive_id: str | None = None
    if sources:
        service = ArchiveService(root, actor="legacy-migration")
        archive_plan = service.plan_archive(sources, reason="migration")
        if not archive_plan["ok"]:
            return {**archive_plan, "migration_version": 2}
        applied = service.apply(str(archive_plan["plan_id"]))
        if not applied["ok"]:
            return {**applied, "migration_version": 2}
        archive_id = str(applied["archive_id"])

    _remove_legacy_directories(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"migration_version": 2, "moved": sources, "archive_id": archive_id}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return {"ok": True, "migration_version": 2, "moved": sources, "archive_id": archive_id}

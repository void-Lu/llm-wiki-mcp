"""One-time, explicit migration away from legacy wiki lifecycle folders."""

from __future__ import annotations

from hashlib import sha256
import json
import shutil
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.content_redaction import redact_for_index
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore, page_from_file
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter


_STRUCTURAL = {"index.md", "log.md", "overview.md"}


def _legacy_chatlog_payload(source: Path, source_name: str) -> bytes:
    """Keep the raw body while recording the redacted projection identity."""
    text = source.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    redacted = redact_for_index(body)
    frontmatter.update({
        "source_kind": "legacy_chatlog",
        "migration_provenance": source_name,
        "original_content_hash": "sha256:" + redacted.original_hash,
        "redacted_content_hash": "sha256:" + redacted.redacted_hash,
    })
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items())
    return ("---\n" + header + "\n---\n\n" + body).encode("utf-8")


def plan_legacy_migration(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    moves: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    queries = root / "wiki" / "queries"
    if queries.exists():
        non_structural = [path for path in queries.rglob("*") if path.is_file() and path.name not in _STRUCTURAL]
        if non_structural:
            blockers.extend({"code": "legacy_queries_not_empty", "path": path.relative_to(root).as_posix()} for path in non_structural)
    chatlog = root / "wiki" / "chatlog"
    if chatlog.exists():
        for source in sorted(chatlog.rglob("*")):
            if not source.is_file(): continue
            target = root / "raw" / "sources" / "chat" / "legacy" / source.relative_to(chatlog)
            source_name = source.relative_to(root).as_posix()
            try:
                expected = _legacy_chatlog_payload(source, source_name)
            except (OSError, UnicodeDecodeError):
                blockers.append({"code": "legacy_chatlog_unreadable", "path": source_name})
                continue
            moves.append({"source": source_name, "target": target.relative_to(root).as_posix(), "hash": "sha256:" + sha256(source.read_bytes()).hexdigest(), "target_hash": "sha256:" + sha256(expected).hexdigest()})
            if target.exists() and target.read_bytes() != expected: blockers.append({"code": "migration_target_conflict", "path": target.relative_to(root).as_posix()})
    source_indexes = root / "wiki" / "sources" / "chatlog"
    if source_indexes.exists():
        for source in sorted(source_indexes.rglob("*.md")):
            fm, _ = split_frontmatter(source.read_text(encoding="utf-8"))
            sources = fm.get("sources", [])
            values = sources if isinstance(sources, list) else [sources]
            if any(str(value).startswith("raw/sources/chat/") and not (root / str(value)).exists() for value in values):
                blockers.append({"code": "legacy_chat_source_orphan", "path": source.relative_to(root).as_posix()})
    legacy_archives = root / "wiki" / "archives"
    if legacy_archives.exists():
        for source in sorted(path for path in legacy_archives.rglob("*") if path.is_file()):
            moves.append({"source": source.relative_to(root).as_posix(), "target": "archives/log.md", "hash": "sha256:" + sha256(source.read_bytes()).hexdigest()})
    return {"ok": not blockers, "dry_run": True, "moves": moves, "blockers": blockers, "migration_version": 1}


def apply_legacy_migration(vault_root: str | Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    plan = plan_legacy_migration(root)
    if not plan["ok"]: return plan
    marker = root / ".llm-wiki" / "legacy-archive-migration-v1.json"
    if marker.exists(): return {"ok": True, "already_migrated": True, "migration_version": 1}
    moved: list[str] = []
    for item in plan["moves"]:
        source, target = root / item["source"], root / item["target"]
        if not source.exists(): continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_relative_to(root / "wiki" / "chatlog"):
            target.write_bytes(_legacy_chatlog_payload(source, item["source"]))
            # ``page_from_file`` applies the shared credential redactor before
            # the retrieval projection is written; raw migration itself stays
            # byte-preserving apart from the documented provenance header.
            page = page_from_file(root, target, scope="active")
            if page is not None:
                RetrievalIndexStore(root).update_page(page)
        elif target.exists():
            source_bytes = source.read_bytes()
            if source_bytes not in target.read_bytes():
                target.write_bytes(target.read_bytes() + b"\n" + source_bytes)
        else:
            shutil.copy2(source, target)
        moved.append(item["source"])
    for legacy in (root / "wiki" / "queries", root / "wiki" / "chatlog", root / "wiki" / "sources" / "chatlog", root / "wiki" / "archives"):
        if legacy.exists(): shutil.rmtree(legacy)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"migration_version": 1, "moved": moved}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return {"ok": True, "migration_version": 1, "moved": moved}

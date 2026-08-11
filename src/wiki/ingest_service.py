"""Single-file ingest domain boundary shared by MCP and worker callers."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from wiki.generation_queue import GenerationQueue
from wiki.ingest_snapshot import IngestSnapshotError, IngestSnapshotter, TEXT_SOURCE_SUFFIXES
from wiki.knowledge_dependencies import KnowledgeDependencies
from retrieval.retrieval_index import RetrievalIndexStore, page_from_file
from wiki.source_provenance import source_path_key
from wiki.wiki_paths import safe_segment

def sync_retrieval_index(vault_root: str | Path, *, full_build: bool = False) -> dict[str, object]:
    """Shared post-write projection boundary for MCP, batch, and adapters."""

    active = RetrievalIndexStore(vault_root)
    raw = RetrievalIndexStore(vault_root, scope="raw")
    active_result = active.reconcile() if active.path.exists() and not full_build else active.build(active.iter_vault_pages())
    raw_result = raw.reconcile() if raw.path.exists() and not full_build else raw.build(raw.iter_vault_pages())
    return {
        "ok": bool(active_result.get("ok")) and bool(raw_result.get("ok")),
        "active": active_result,
        "raw": raw_result,
    }


def ingest_file(*, vault_root: str | Path, source_path: str | Path, source_name: str, project: str = "", source_type: str = "file") -> dict[str, Any]:
    """Snapshot one explicit source and index it only when it is text knowledge.

    This function intentionally accepts neither a directory nor model/index
    configuration. Text sources are copied to ``raw/sources`` and projected to
    raw FTS; every other file is copied byte-for-byte to ``raw/assets`` without
    semantic indexing.
    """

    root = Path(vault_root).expanduser().resolve()
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        return {"ok": False, "code": "source_not_file", "error": "wiki_ingest accepts one existing file"}
    try:
        type_value = safe_segment(source_type)
        name_value = safe_segment(source_name)
        project_value = safe_segment(project) if project else "default"
    except ValueError as exc:
        return {"ok": False, "code": getattr(exc, "code", "invalid_path_component"), "error": str(exc)}
    try:
        target_name = safe_segment(source.name)
    except ValueError as exc:
        return {"ok": False, "code": getattr(exc, "code", "invalid_path_component"), "error": "source filename is not a safe target component"}
    try:
        snapshot = IngestSnapshotter().snapshot(source)
    except IngestSnapshotError as exc:
        return {"ok": False, "code": exc.code, "error": "source snapshot could not be completed"}
    is_text_source = snapshot.is_text
    target = (
        root / "raw" / "sources" / type_value / project_value / name_value / target_name
        if is_text_source
        else root / "raw" / "assets" / project_value / name_value / target_name
    )
    incoming_hash = snapshot.content_hash
    try:
        previous_hash = _hash_file(target) if target.is_file() else None
        if previous_hash != incoming_hash:
            snapshot.commit_to(target)
        else:
            snapshot.cleanup()
    except IngestSnapshotError as exc:
        snapshot.cleanup()
        return {"ok": False, "code": exc.code, "error": "source snapshot could not be committed"}
    except OSError as exc:
        snapshot.cleanup()
        del exc
        return {"ok": False, "code": "source_write_failed", "error": "source snapshot could not be committed"}
    operation = "new" if previous_hash is None else "unchanged" if previous_hash == incoming_hash else "modified"
    if not is_text_source:
        return {
            "ok": True,
            "operation": operation,
            "source": target.relative_to(root).as_posix(),
            "content_hash": incoming_hash,
            "storage_kind": "asset",
            "semantic_indexed": False,
            "index_scope": None,
            "index": {"ok": True, "state": "not_indexed", "code": "asset_not_indexed"},
        }
    # Raw snapshots are the source of truth.  A changed snapshot only
    # invalidates dependent page provenance; it never creates a derived page.
    provenance_result: dict[str, Any] | None = None
    if type_value != "chat" and operation != "unchanged":
        provenance_result = _invalidate_raw_provenance(root, target.relative_to(root), incoming_hash)
    index_scope = "active" if type_value == "chat" else "raw"
    indexed = page_from_file(root, target, scope=index_scope)
    if indexed is None:
        index = {"ok": True, "state": "not_eligible", "code": "not_eligible"}
    elif RetrievalIndexStore(root, scope=index_scope).path.exists():
        index = RetrievalIndexStore(root, scope=index_scope).update_page(indexed)
    else:
        store = RetrievalIndexStore(root, scope=index_scope)
        index = store.build(store.iter_vault_pages())
    response = {"ok": bool(index.get("ok")), "operation": operation, "source": target.relative_to(root).as_posix(), "content_hash": incoming_hash, "storage_kind": "text_source", "semantic_indexed": bool(index.get("ok")), "index_scope": index_scope, "index": index}
    if provenance_result is not None:
        response["generation"] = provenance_result["generation"]
        response["stale_pages"] = provenance_result["stale"]
        response["superseded_jobs"] = provenance_result["superseded"]
    return response


def _invalidate_raw_provenance(root: Path, relative_path: Path, source_hash: str) -> dict[str, Any]:
    """Mark raw dependents stale without invoking the retired compiler path."""

    relative = relative_path.as_posix()
    stale = KnowledgeDependencies(root).source_changed(source_path_key(relative), source_hash)
    superseded = GenerationQueue(root).supersede_sources({relative})
    return {
        "stale": stale,
        "superseded": superseded,
        "generation": {"enabled": False, "reason": "raw_only"},
    }


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_text_knowledge_source(source: Path) -> bool:
    """Return the suffix-side text hint for compatibility callers.

    ``ingest_file`` uses :class:`IngestSnapshotter` so the final decision is
    made from the bytes read through its single source handle.
    """

    return source.suffix.casefold() in TEXT_SOURCE_SUFFIXES

"""Single-file ingest domain boundary shared by MCP and worker callers."""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore, page_from_file
from netsuite_llm_wiki_mcp.knowledge_compiler import KnowledgeCompiler
from netsuite_llm_wiki_mcp.wiki_paths import safe_segment


def sync_retrieval_index(vault_root: str | Path, *, full_build: bool = False) -> dict[str, object]:
    """Shared post-write projection boundary for MCP, batch, and adapters."""

    store = RetrievalIndexStore(vault_root)
    return store.reconcile() if store.path.exists() and not full_build else store.build(store.iter_vault_pages())


def ingest_file(*, vault_root: str | Path, source_path: str | Path, source_name: str, project: str = "", source_type: str = "file") -> dict[str, Any]:
    """Snapshot one explicit source and synchronise its eligible FTS projection.

    This function intentionally accepts neither a directory nor model/index
    configuration. Raw sources are copied byte-for-byte; redaction happens only
    in retrieval projections.
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
    target = root / "raw" / "sources" / type_value / project_value / name_value / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    incoming_hash = _hash_file(source)
    previous_hash = _hash_file(target) if target.exists() else None
    if previous_hash != incoming_hash:
        shutil.copyfile(source, target)
    operation = "new" if previous_hash is None else "unchanged" if previous_hash == incoming_hash else "modified"
    # Raw snapshots are the source of truth.  Knowledge compilation is queued
    # only after a new/changed non-chat snapshot exists; queue deduplication is
    # content-addressed and durable independently of retrieval state.
    compiler_result: dict[str, Any] | None = None
    if type_value != "chat" and operation != "unchanged":
        compiler = KnowledgeCompiler(root)
        compiler_result = compiler.raw_changed(target.relative_to(root))
    indexed = page_from_file(root, target, scope="active")
    if indexed is None:
        index = {"ok": True, "state": "not_eligible", "code": "not_eligible"}
    elif RetrievalIndexStore(root).path.exists():
        index = RetrievalIndexStore(root).update_page(indexed)
    else:
        index = sync_retrieval_index(root)
    response = {"ok": bool(index.get("ok")), "operation": operation, "source": target.relative_to(root).as_posix(), "content_hash": incoming_hash, "index": index}
    if compiler_result is not None:
        response["generation"] = compiler_result.get("enqueued")
        response["stale_pages"] = compiler_result.get("stale", [])
    return response


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

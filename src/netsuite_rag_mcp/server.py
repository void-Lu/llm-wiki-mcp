from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.indexer import index_sources as run_index_sources
from netsuite_rag_mcp.indexer import index_vault as run_index_vault
from netsuite_rag_mcp.manifest import read_manifest
from netsuite_rag_mcp.note_writer import save_obsidian_note as run_save_obsidian_note
from netsuite_rag_mcp.retriever import ask_netsuite_rag as run_ask_netsuite_rag
from netsuite_rag_mcp.retriever import search_netsuite_knowledge as run_search_netsuite_knowledge
from netsuite_rag_mcp.runtime_config import RuntimeConfig, RuntimeConfigError, resolve_runtime_config
from netsuite_rag_mcp.vector_store import Embedder
from netsuite_rag_mcp.wiki_generator import generate_suitecloud_wiki as run_generate_suitecloud_wiki
from netsuite_rag_mcp.wiki_generator import write_wiki_summaries as run_write_wiki_summaries

mcp = FastMCP("netsuite-obsidian-rag")


def _runtime_error_payload(exc: RuntimeConfigError) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": exc.code, "error": str(exc)}
    if exc.config_path is not None:
        payload["config_path"] = str(exc.config_path)
    return payload


def _resolve_runtime(vault_root: str | None = None) -> RuntimeConfig:
    return resolve_runtime_config(vault_root_arg=vault_root)


def index_vault_tool(
    vault_root: str | None = None,
    mode: str = "incremental",
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    if mode not in {"full", "incremental"}:
        return {"error": "mode must be 'full' or 'incremental'", "mode": mode}
    try:
        runtime = _resolve_runtime(vault_root)
        return run_index_vault(runtime.vault_root, mode=mode, embedder=embedder)
    except RuntimeConfigError as exc:
        return _runtime_error_payload(exc)


def index_sources_tool(
    vault_root: str | None = None,
    source_names: list[str] | None = None,
    source_kind: str | None = None,
    mode: str = "incremental",
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    if mode not in {"full", "incremental"}:
        return {"ok": False, "code": "invalid_mode", "error": "mode must be 'full' or 'incremental'", "mode": mode}
    try:
        runtime = _resolve_runtime(vault_root)
        return run_index_sources(
            runtime.vault_root, source_names=source_names, source_kind=source_kind, mode=mode, embedder=embedder
        )
    except RuntimeConfigError as exc:
        return _runtime_error_payload(exc)
    except ValueError as exc:
        return {"ok": False, "code": "invalid_index_request", "error": str(exc)}


def search_netsuite_knowledge_tool(
    question: str,
    vault_root: str | None = None,
    project: str | None = None,
    script_type: str | None = None,
    related_objects: str | None = None,
    related_scripts: str | None = None,
    object_type: str | None = None,
    status: str | None = None,
    source_kind: str | None = None,
    source_name: str | None = None,
    top_k: int = 5,
    content_type: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    filters = _build_filters(
        project, script_type, related_objects, related_scripts, object_type, status,
        source_kind, source_name, content_type,
    )
    try:
        runtime = _resolve_runtime(vault_root)
        return run_search_netsuite_knowledge(
            runtime.vault_root, question, filters=filters, top_k=top_k,
            source_kind=source_kind, source_name=source_name,
            content_type=content_type, include_archived=include_archived,
        )
    except RuntimeConfigError as exc:
        return _runtime_error_payload(exc)


def ask_netsuite_rag_tool(
    question: str,
    vault_root: str | None = None,
    project: str | None = None,
    script_type: str | None = None,
    related_objects: str | None = None,
    related_scripts: str | None = None,
    object_type: str | None = None,
    status: str | None = None,
    source_kind: str | None = None,
    source_name: str | None = None,
    top_k: int = 5,
    content_type: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    filters = _build_filters(
        project, script_type, related_objects, related_scripts, object_type, status,
        source_kind, source_name, content_type,
    )
    try:
        runtime = _resolve_runtime(vault_root)
        return run_ask_netsuite_rag(
            runtime.vault_root, question, filters=filters, top_k=top_k,
            source_kind=source_kind, source_name=source_name,
            content_type=content_type, include_archived=include_archived,
        )
    except RuntimeConfigError as exc:
        return _runtime_error_payload(exc)


def get_index_status_tool(vault_root: str | None = None) -> dict[str, Any]:
    try:
        runtime = _resolve_runtime(vault_root)
        config = load_config(runtime.vault_root, runtime_config=runtime)
    except RuntimeConfigError as exc:
        return _runtime_error_payload(exc)
    manifest_path = config.manifest_path

    base: dict[str, Any] = {
        "ok": True,
        "vault_root": str(runtime.vault_root),
        "resolution_source": runtime.resolution_source,
        "config_path": str(runtime.global_config_path),
        "global_config_path": str(runtime.global_config_path),
        "sources_config_path": str(runtime.sources_config_path),
        "sources_config_exists": runtime.sources_config_path.exists(),
        "data_root": str(runtime.data_root),
        "user_data_root": str(runtime.user_data_root),
        "vault_data_root": str(runtime.vault_data_root),
        "vault_storage_dir": str(runtime.vault_storage_dir),
        "vault_storage_id": runtime.vault_storage_id,
        "chroma_path": str(config.chroma_path),
        "manifest_path": str(config.manifest_path),
        "embedding_cache_path": str(config.embedding_cache_path),
        "model_cache_path": str(config.embedding_cache_path),
        "manifest_exists": manifest_path.exists(),
    }

    # Try to get collection count without needing a full embedder
    count = 0
    if config.chroma_path.exists():
        import chromadb
        client = chromadb.PersistentClient(path=str(config.chroma_path))
        try:
            collection = client.get_collection(name=config.collection_name)
            count = collection.count()
        except chromadb.errors.NotFoundError:
            count = 0
    base["indexed"] = count > 0
    base["collection_count"] = count

    # Build per-source statistics from manifest
    sources: dict[str, dict[str, Any]] = {}
    manifest = read_manifest(manifest_path) if manifest_path.exists() else {}

    for source in config.sources:
        name = source.source_name
        # Count files and find last indexed time for this source
        source_entries = [
            entry for entry in manifest.values()
            if entry.source_name == name
        ]
        file_count = len(source_entries)
        last_indexed = ""
        if source_entries:
            last_indexed = max(
                (entry.indexed_at for entry in source_entries if entry.indexed_at),
                default="",
            )

        source_info: dict[str, Any] = {
            "source_kind": source.source_kind,
            "file_count": file_count,
            "last_indexed": last_indexed,
        }

        # Add git info for code sources
        if source.source_kind == "code" and source_entries:
            from netsuite_rag_mcp.git_utils import get_git_commit, is_git_dirty, get_git_branch
            git_commit = ""
            git_branch = ""
            is_dirty = False
            if source.root.exists():
                git_commit = get_git_commit(source.root)
                git_branch = get_git_branch(source.root)
                is_dirty = is_git_dirty(source.root)
            source_info["git"] = {
                "commit": git_commit,
                "branch": git_branch,
                "dirty": is_dirty,
            }

        sources[name] = source_info

    base["sources"] = sources
    return base


def save_obsidian_note_tool(
    note_type: str,
    title: str,
    content: str,
    project: str | None = None,
    domain: str | None = None,
    related_script_types: list[str] | None = None,
    script_type: str | None = None,
    object_type: str | None = None,
    related_objects: list[str] | None = None,
    related_scripts: list[str] | None = None,
    tags: list[str] | None = None,
    zentao_urls: list[str] | None = None,
    decision_status: str | None = None,
    status: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
    auto_index: bool = True,
    vault_root: str | None = None,
) -> dict[str, Any]:
    return run_save_obsidian_note(
        note_type=note_type,
        title=title,
        content=content,
        project=project,
        domain=domain,
        related_script_types=related_script_types,
        script_type=script_type,
        object_type=object_type,
        related_objects=related_objects,
        related_scripts=related_scripts,
        tags=tags,
        zentao_urls=zentao_urls,
        decision_status=decision_status,
        status=status,
        filename=filename,
        overwrite=overwrite,
        auto_index=auto_index,
        vault_root=vault_root,
    )


def generate_suitecloud_wiki_tool(
    project: str,
    source_name: str,
    vault_root: str | None = None,
    auto_index: bool = True,
    llm_summary: bool = False,
) -> dict[str, Any]:
    return run_generate_suitecloud_wiki(
        vault_root=vault_root,
        project=project,
        source_name=source_name,
        auto_index=auto_index,
        llm_summary=llm_summary,
    )


def _build_filters(
    project: str | None,
    script_type: str | None,
    related_objects: str | None,
    related_scripts: str | None,
    object_type: str | None,
    status: str | None,
    source_kind: str | None = None,
    source_name: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for key, value in {
        "project": project,
        "script_type": script_type,
        "related_objects": related_objects,
        "related_scripts": related_scripts,
        "object_type": object_type,
        "status": status,
        "source_kind": source_kind,
        "source_name": source_name,
        "type": content_type,
    }.items():
        if value:
            filters[key] = value
    return filters


@mcp.tool()
def index_vault(vault_root: str | None = None, mode: str = "incremental") -> dict[str, Any]:
    """Index the Obsidian vault into the local ChromaDB collection.

    Args:
        vault_root: Root path of the Obsidian vault. Defaults to NETSUITE_RAG_VAULT_ROOT env or global config.
        mode: "full" to rebuild index from scratch, "incremental" to only index changed files.
    """
    return index_vault_tool(vault_root=vault_root, mode=mode)


@mcp.tool()
def index_sources(
    vault_root: str | None = None,
    source_names: list[str] | None = None,
    source_kind: str | None = None,
    mode: str = "incremental",
) -> dict[str, Any]:
    """Index specific sources by name or kind.

    Args:
        vault_root: Root path of the Obsidian vault.
        source_names: Optional list of source names to index (e.g., ["obsidian", "netsuite_repo"]).
        source_kind: Optional source kind filter ("note" or "code").
        mode: "full" to reset and re-index, "incremental" for delta.
    """
    return index_sources_tool(
        vault_root=vault_root, source_names=source_names, source_kind=source_kind, mode=mode,
    )


@mcp.tool()
def search_netsuite_knowledge(
    question: str,
    vault_root: str | None = None,
    project: str | None = None,
    script_type: str | None = None,
    related_objects: str | None = None,
    related_scripts: str | None = None,
    object_type: str | None = None,
    status: str | None = None,
    source_kind: str | None = None,
    source_name: str | None = None,
    top_k: int = 5,
    content_type: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Search NetSuite Obsidian knowledge and return retrieved chunks with citations.

    Args:
        question: The search query in natural language.
        vault_root: Root path of the Obsidian vault.
        project: Filter by project name.
        script_type: Filter by script type (restlet, suitelet, userevent, mapreduce, clientscript).
        related_objects: Filter by related NetSuite records.
        related_scripts: Filter by related script IDs.
        object_type: Filter by object type (savedsearch, customlist, customrecord, workflow, role, deployment).
        status: Filter by status (active, inactive).
        source_kind: Filter by source kind (note, code).
        source_name: Filter by source name (e.g., obsidian, netsuite_repo).
        content_type: Filter by metadata type (e.g., generated_wiki).
        include_archived: Include archived generated Wiki pages.
        top_k: Number of results to return.
    """
    return search_netsuite_knowledge_tool(
        question,
        vault_root,
        project,
        script_type,
        related_objects,
        related_scripts,
        object_type,
        status,
        source_kind,
        source_name,
        top_k,
        content_type,
        include_archived,
    )


@mcp.tool()
def ask_netsuite_rag(
    question: str,
    vault_root: str | None = None,
    project: str | None = None,
    script_type: str | None = None,
    related_objects: str | None = None,
    related_scripts: str | None = None,
    object_type: str | None = None,
    status: str | None = None,
    source_kind: str | None = None,
    source_name: str | None = None,
    top_k: int = 5,
    content_type: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Return RAG context, sources, and answer policy for the Copilot model.

    Args:
        question: The question to answer.
        vault_root: Root path of the Obsidian vault.
        project: Filter by project name.
        script_type: Filter by script type.
        related_objects: Filter by related NetSuite records.
        related_scripts: Filter by related script IDs.
        object_type: Filter by object type.
        status: Filter by status.
        source_kind: Filter by source kind (note, code).
        source_name: Filter by source name (e.g., obsidian, netsuite_repo).
        content_type: Filter by metadata type (e.g., generated_wiki).
        include_archived: Include archived generated Wiki pages.
        top_k: Number of chunks to retrieve.
    """
    return ask_netsuite_rag_tool(
        question,
        vault_root,
        project,
        script_type,
        related_objects,
        related_scripts,
        object_type,
        status,
        source_kind,
        source_name,
        top_k,
        content_type,
        include_archived,
    )


@mcp.tool()
def get_index_status(vault_root: str | None = None) -> dict[str, Any]:
    """Return current index status: collection name, chunk count, last index time.

    Args:
        vault_root: Root path of the Obsidian vault.
    """
    return get_index_status_tool(vault_root)


@mcp.tool()
def save_obsidian_note(
    note_type: str,
    title: str,
    content: str,
    project: str | None = None,
    domain: str | None = None,
    related_script_types: list[str] | None = None,
    script_type: str | None = None,
    object_type: str | None = None,
    related_objects: list[str] | None = None,
    related_scripts: list[str] | None = None,
    tags: list[str] | None = None,
    zentao_urls: list[str] | None = None,
    decision_status: str | None = None,
    status: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
    auto_index: bool = True,
    vault_root: str | None = None,
) -> dict[str, Any]:
    """Save an Obsidian note into the configured vault and optionally re-index."""
    return save_obsidian_note_tool(
        note_type=note_type,
        title=title,
        content=content,
        project=project,
        domain=domain,
        related_script_types=related_script_types,
        script_type=script_type,
        object_type=object_type,
        related_objects=related_objects,
        related_scripts=related_scripts,
        tags=tags,
        zentao_urls=zentao_urls,
        decision_status=decision_status,
        status=status,
        filename=filename,
        overwrite=overwrite,
        auto_index=auto_index,
        vault_root=vault_root,
    )


@mcp.tool()
def generate_suitecloud_wiki(
    project: str,
    source_name: str,
    vault_root: str | None = None,
    auto_index: bool = True,
    llm_summary: bool = False,
) -> dict[str, Any]:
    """Generate code-fact Obsidian Wiki pages for a SuiteCloud code source.

    Args:
        project: Project directory name under projects/<project>/wiki.
        source_name: Code source name from rag/sources.yaml.
        vault_root: Root path of the Obsidian vault.
        auto_index: Whether to incrementally index the obsidian source after writing pages.
        llm_summary: Whether to return summary_prompts for the calling model to generate business summaries.
    """
    return generate_suitecloud_wiki_tool(
        project=project,
        source_name=source_name,
        vault_root=vault_root,
        auto_index=auto_index,
        llm_summary=llm_summary,
    )


@mcp.tool()
def write_wiki_summaries(
    project: str,
    summaries: list[dict[str, str]],
    vault_root: str | None = None,
) -> dict[str, Any]:
    """Write LLM-generated business summaries into existing wiki pages.

    Call this after generate_suitecloud_wiki returns summary_prompts.
    Each summary dict must have 'wiki_path' (relative path from vault root) and 'summary' (the generated text).

    Args:
        project: Project directory name.
        summaries: List of {wiki_path, summary} dicts to write into wiki pages.
        vault_root: Root path of the Obsidian vault.
    """
    try:
        runtime = _resolve_runtime(vault_root)
    except RuntimeConfigError as exc:
        return _runtime_error_payload(exc)
    return run_write_wiki_summaries(runtime.vault_root, project, summaries)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
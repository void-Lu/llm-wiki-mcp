from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from netsuite_rag_mcp.note_writer import save_obsidian_note as run_save_obsidian_note
from netsuite_rag_mcp.wiki_ingest import ingest_codegraph as run_ingest_codegraph
from netsuite_rag_mcp.wiki_lint import wiki_lint as run_wiki_lint
from netsuite_rag_mcp.wiki_paths import create_wiki_root
from netsuite_rag_mcp.wiki_query import wiki_query as run_wiki_query

mcp = FastMCP("netsuite-llm-wiki-mcp")


def _deprecated_rag_tool(replacement: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "deprecated_rag_tool",
        "error": "RAG/vector indexing has been removed from the main workflow; use the LLM Wiki tools instead.",
        "replacement": replacement,
    }


def index_vault_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _deprecated_rag_tool("wiki_ingest")


def index_sources_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _deprecated_rag_tool("wiki_ingest")


def search_netsuite_knowledge_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _deprecated_rag_tool("wiki_query")


def ask_netsuite_rag_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _deprecated_rag_tool("wiki_query")


def get_index_status_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _deprecated_rag_tool("wiki_lint")


def wiki_init_tool(vault_root: str) -> dict[str, Any]:
    paths = create_wiki_root(vault_root)
    return {"ok": True, "vault_root": str(paths.root)}


def wiki_ingest_tool(
    vault_root: str,
    source_type: str,
    source_name: str | None = None,
    query: str | None = None,
    project: str | None = None,
    codegraph_project_path: str | None = None,
) -> dict[str, Any]:
    if source_type != "codegraph":
        return {"ok": False, "code": "unsupported_source_type", "error": "only codegraph ingest is implemented"}
    if not project:
        return {"ok": False, "code": "missing_project", "error": "project is required for codegraph ingest"}
    if not source_name:
        return {"ok": False, "code": "missing_source_name", "error": "source_name is required for codegraph ingest"}
    return run_ingest_codegraph(
        vault_root=vault_root,
        project=project,
        source_name=source_name,
        query=query or "project code overview",
        codegraph_project_path=codegraph_project_path,
    )


def wiki_query_tool(
    vault_root: str,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    include_content: bool = True,
) -> dict[str, Any]:
    return run_wiki_query(
        vault_root=vault_root,
        question=question,
        project=project,
        top_k=top_k,
        include_content=include_content,
    )


def wiki_lint_tool(vault_root: str) -> dict[str, Any]:
    return run_wiki_lint(vault_root)


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
    if vault_root is None:
        return {"ok": False, "code": "missing_vault_root", "error": "vault_root is required"}
    return wiki_ingest_tool(
        vault_root=vault_root,
        source_type="codegraph",
        source_name=source_name,
        project=project,
    )


def write_wiki_summaries_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _deprecated_rag_tool("wiki_ingest")


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
def wiki_init(vault_root: str) -> dict[str, Any]:
    """Create the confirmed external Obsidian LLM Wiki structure."""
    return wiki_init_tool(vault_root)


@mcp.tool()
def wiki_ingest(
    vault_root: str,
    source_type: str,
    source_name: str | None = None,
    query: str | None = None,
    project: str | None = None,
    codegraph_project_path: str | None = None,
) -> dict[str, Any]:
    """Ingest a source into the LLM Wiki. The first supported source_type is codegraph."""
    return wiki_ingest_tool(vault_root, source_type, source_name, query, project, codegraph_project_path)


@mcp.tool()
def wiki_query(
    vault_root: str,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    include_content: bool = True,
) -> dict[str, Any]:
    """Query persisted wiki pages without vector embeddings."""
    return wiki_query_tool(vault_root, question, project, top_k, include_content)


@mcp.tool()
def wiki_lint(vault_root: str) -> dict[str, Any]:
    """Check LLM Wiki structure, frontmatter, wikilinks, and stale directories."""
    return wiki_lint_tool(vault_root)


@mcp.tool()
def index_vault(vault_root: str | None = None, mode: str = "incremental") -> dict[str, Any]:
    """Deprecated: use wiki_ingest."""
    return index_vault_tool(vault_root=vault_root, mode=mode)


@mcp.tool()
def index_sources(
    vault_root: str | None = None,
    source_names: list[str] | None = None,
    source_kind: str | None = None,
    mode: str = "incremental",
) -> dict[str, Any]:
    """Deprecated: use wiki_ingest."""
    return index_sources_tool(vault_root=vault_root, source_names=source_names, source_kind=source_kind, mode=mode)


@mcp.tool()
def search_netsuite_knowledge(question: str, vault_root: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Deprecated: use wiki_query."""
    return search_netsuite_knowledge_tool(question=question, vault_root=vault_root, **kwargs)


@mcp.tool()
def ask_netsuite_rag(question: str, vault_root: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Deprecated: use wiki_query."""
    return ask_netsuite_rag_tool(question=question, vault_root=vault_root, **kwargs)


@mcp.tool()
def get_index_status(vault_root: str | None = None) -> dict[str, Any]:
    """Deprecated: use wiki_lint."""
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
    """Save a curated wiki note."""
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
    """Deprecated compatibility wrapper around wiki_ingest source_type=codegraph."""
    return generate_suitecloud_wiki_tool(project, source_name, vault_root, auto_index, llm_summary)


@mcp.tool()
def write_wiki_summaries(project: str, summaries: list[dict[str, str]], vault_root: str | None = None) -> dict[str, Any]:
    """Deprecated: generated wiki summaries are handled through wiki_ingest."""
    return write_wiki_summaries_tool(project=project, summaries=summaries, vault_root=vault_root)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

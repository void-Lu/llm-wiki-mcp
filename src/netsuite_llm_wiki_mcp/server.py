from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from netsuite_llm_wiki_mcp.note_writer import save_obsidian_note as run_write_note
from netsuite_llm_wiki_mcp.page_merge import apply_page_merge as run_apply_page_merge
from netsuite_llm_wiki_mcp.page_merge import prepare_body_merge as run_prepare_body_merge
from netsuite_llm_wiki_mcp.wiki_batch import wiki_ingest_batch as run_wiki_ingest_batch
from netsuite_llm_wiki_mcp.wiki_dedup import wiki_dedup as run_wiki_dedup
from netsuite_llm_wiki_mcp.wiki_delete import wiki_delete_source as run_wiki_delete_source
from netsuite_llm_wiki_mcp.wiki_enrich import wiki_enrich as run_wiki_enrich
from netsuite_llm_wiki_mcp.wiki_gap import wiki_gap as run_wiki_gap
from netsuite_llm_wiki_mcp.wiki_ingest import ingest_codegraph as run_ingest_codegraph
from netsuite_llm_wiki_mcp.wiki_ingest import rescan_source as run_rescan_source
from netsuite_llm_wiki_mcp.wiki_ingest import staged_wiki_ingest as run_staged_wiki_ingest
from netsuite_llm_wiki_mcp.wiki_ingest_url import wiki_ingest_url as run_wiki_ingest_url
from netsuite_llm_wiki_mcp.wiki_insights import wiki_insights as run_wiki_insights
from netsuite_llm_wiki_mcp.wiki_lint import wiki_lint as run_wiki_lint
from netsuite_llm_wiki_mcp.wiki_log import parse_log_entries as run_parse_log_entries
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_query import wiki_query as run_wiki_query
from netsuite_llm_wiki_mcp.wiki_query import wiki_query_debug as run_wiki_query_debug
from netsuite_llm_wiki_mcp.wiki_research import wiki_research as run_wiki_research
from netsuite_llm_wiki_mcp.wiki_synthesis import wiki_synthesis as run_wiki_synthesis
from netsuite_llm_wiki_mcp.wiki_verify import wiki_verify as run_wiki_verify

mcp = FastMCP("netsuite-llm-wiki-mcp")


def wiki_init_tool(vault_root: str) -> dict[str, Any]:
    paths = create_wiki_root(vault_root)
    return {"ok": True, "vault_root": str(paths.root)}


def wiki_ingest_codegraph_tool(
    vault_root: str,
    project: str,
    source_name: str,
    query: str = "project code overview",
    codegraph_project_path: str | None = None,
    include_extensions: list[str] | None = None,
    profile: str = "generic",
) -> dict[str, Any]:
    return run_ingest_codegraph(
        vault_root=vault_root,
        project=project,
        source_name=source_name,
        query=query,
        codegraph_project_path=codegraph_project_path,
        include_extensions=include_extensions,
        profile=profile,
    )


def wiki_query_tool(
    vault_root: str,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    include_content: bool = True,
    context_window_tokens: int = 16_000,
    include_context_pack: bool = True,
    chat_history: list[dict[str, str]] | None = None,
    language: str = "zh-CN",
    enable_vector: bool = False,
    vector_config: dict[str, Any] | None = None,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
    filter_type: str | None = None,
    filter_tags: list[str] | None = None,
) -> dict[str, Any]:
    return run_wiki_query(
        vault_root=vault_root,
        question=question,
        project=project,
        top_k=top_k,
        include_content=include_content,
        context_window_tokens=context_window_tokens,
        include_context_pack=include_context_pack,
        chat_history=chat_history,
        language=language,
        enable_vector=enable_vector,
        vector_config=vector_config,
        max_graph_hops=max_graph_hops,
        include_raw_sources=include_raw_sources,
        filter_type=filter_type,
        filter_tags=filter_tags,
    )


def wiki_query_debug_tool(
    vault_root: str,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
) -> dict[str, Any]:
    return run_wiki_query_debug(
        vault_root=vault_root,
        question=question,
        project=project,
        top_k=top_k,
        max_graph_hops=max_graph_hops,
        include_raw_sources=include_raw_sources,
    )


def wiki_ingest_llm_tool(
    vault_root: str,
    stage: str,
    project: str,
    source_name: str,
    source_path: str | None = None,
    source_type: str = "file",
    language: str = "zh-CN",
    analysis: dict[str, Any] | str | None = None,
    generation: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    return run_staged_wiki_ingest(
        vault_root=vault_root,
        stage=stage,
        project=project,
        source_name=source_name,
        source_path=source_path,
        source_type=source_type,
        language=language,
        analysis=analysis,
        generation=generation,
    )



def wiki_rescan_tool(
    vault_root: str,
    project: str,
    source_name: str,
    source_path: str,
    source_type: str = "file",
    language: str = "zh-CN",
) -> dict[str, Any]:
    return run_rescan_source(
        vault_root=vault_root,
        project=project,
        source_name=source_name,
        source_path=source_path,
        source_type=source_type,
        language=language,
    )



def wiki_lint_tool(
    vault_root: str,
    stage: str = "structure",
    project: str | None = None,
    semantic_review: str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    return run_wiki_lint(
        vault_root=vault_root,
        stage=stage,
        project=project,
        semantic_review=semantic_review,
        language=language,
    )


def wiki_synthesis_tool(
    vault_root: str,
    question: str,
    stage: str = "prepare",
    context_pages: list[dict[str, Any]] | None = None,
    synthesis: str | None = None,
    title: str | None = None,
    project: str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    return run_wiki_synthesis(
        vault_root=vault_root,
        question=question,
        stage=stage,
        context_pages=context_pages,
        synthesis=synthesis,
        title=title,
        project=project,
        language=language,
    )


def wiki_changelog_tool(vault_root: str, limit: int = 10) -> dict[str, Any]:
    entries = run_parse_log_entries(vault_root, limit=limit)
    return {"ok": True, "entries": entries, "count": len(entries)}


def wiki_write_note_tool(
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
    return run_write_note(
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
def wiki_ingest_codegraph(
    vault_root: str,
    project: str,
    source_name: str,
    query: str = "project code overview",
    codegraph_project_path: str | None = None,
    include_extensions: list[str] | None = None,
    profile: str = "generic",
) -> dict[str, Any]:
    """Ingest CodeGraph symbols and code facts into the LLM Wiki (synchronous, no LLM needed)."""
    return wiki_ingest_codegraph_tool(
        vault_root, project, source_name, query, codegraph_project_path,
        include_extensions=include_extensions,
        profile=profile,
    )


@mcp.tool()
def wiki_query(
    vault_root: str,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    include_content: bool = True,
    context_window_tokens: int = 16_000,
    include_context_pack: bool = True,
    chat_history: list[dict[str, str]] | None = None,
    language: str = "zh-CN",
    enable_vector: bool = False,
    vector_config: dict[str, Any] | None = None,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
    filter_type: str | None = None,
    filter_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Query persisted wiki pages and return a budgeted context pack."""
    return wiki_query_tool(
        vault_root,
        question,
        project,
        top_k,
        include_content,
        context_window_tokens,
        include_context_pack,
        chat_history,
        language,
        enable_vector,
        vector_config,
        max_graph_hops,
        include_raw_sources,
        filter_type,
        filter_tags,
    )


@mcp.tool()
def wiki_query_debug(
    vault_root: str,
    question: str,
    project: str | None = None,
    top_k: int = 8,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
) -> dict[str, Any]:
    """Explain wiki query scoring and graph-expansion reasons."""
    return wiki_query_debug_tool(vault_root, question, project, top_k, max_graph_hops, include_raw_sources)


@mcp.tool()
def wiki_ingest_llm(
    vault_root: str,
    stage: str,
    project: str,
    source_name: str,
    source_path: str | None = None,
    source_type: str = "file",
    language: str = "zh-CN",
    analysis: dict[str, Any] | str | None = None,
    generation: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Run staged LLM-assisted ingest. Recommended two-stage flow: stage='prepare' (returns prompt) then stage='apply' (writes pages). Legacy three-stage (prepare_analysis/prepare_generation/apply_generation) still supported."""
    return wiki_ingest_llm_tool(vault_root, stage, project, source_name, source_path, source_type, language, analysis, generation)



@mcp.tool()
def wiki_rescan(
    vault_root: str,
    project: str,
    source_name: str,
    source_path: str,
    source_type: str = "file",
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Rescan a local source, persist snapshots/cache, and report whether it changed."""
    return wiki_rescan_tool(vault_root, project, source_name, source_path, source_type, language)


def wiki_ingest_url_tool(
    vault_root: str,
    urls: list[str],
    project: str,
    source_name: str,
    language: str = "zh-CN",
) -> dict[str, Any]:
    return run_wiki_ingest_url(vault_root=vault_root, urls=urls, project=project, source_name=source_name, language=language)


@mcp.tool()
def wiki_ingest_url(
    vault_root: str,
    urls: list[str],
    project: str,
    source_name: str,
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Fetch URLs, convert HTML to Markdown, snapshot and redact, return LLM prompt. Follow up with wiki_ingest_llm stage='apply'."""
    return wiki_ingest_url_tool(vault_root, urls, project, source_name, language)



@mcp.tool()
def wiki_lint(
    vault_root: str,
    stage: str = "structure",
    project: str | None = None,
    semantic_review: str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Check LLM Wiki structure and run staged semantic health reviews."""
    return wiki_lint_tool(vault_root, stage, project, semantic_review, language)


@mcp.tool()
def wiki_changelog(vault_root: str, limit: int = 10) -> dict[str, Any]:
    """Return recent wiki log entries as structured data."""
    return wiki_changelog_tool(vault_root, limit)


@mcp.tool()
def wiki_write_note(
    title: str,
    content: str,
    note_type: str | None = None,
    noteType: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    related_script_types: list[str] | None = None,
    relatedScriptTypes: list[str] | None = None,
    script_type: str | None = None,
    scriptType: str | None = None,
    object_type: str | None = None,
    objectType: str | None = None,
    related_objects: list[str] | None = None,
    relatedObjects: list[str] | None = None,
    related_scripts: list[str] | None = None,
    relatedScripts: list[str] | None = None,
    tags: list[str] | None = None,
    zentao_urls: list[str] | None = None,
    zentaoUrls: list[str] | None = None,
    decision_status: str | None = None,
    decisionStatus: str | None = None,
    status: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
    auto_index: bool = True,
    autoIndex: bool | None = None,
    vault_root: str | None = None,
    vaultRoot: str | None = None,
) -> dict[str, Any]:
    """Write a human-curated note into the wiki. Supports decision, troubleshooting, requirement, and knowledge note types."""
    resolved_note_type = note_type or noteType
    if not resolved_note_type:
        return {"ok": False, "code": "missing_note_type", "error": "note_type is required"}
    return wiki_write_note_tool(
        note_type=resolved_note_type,
        title=title,
        content=content,
        project=project,
        domain=domain,
        related_script_types=related_script_types or relatedScriptTypes,
        script_type=script_type or scriptType,
        object_type=object_type or objectType,
        related_objects=related_objects or relatedObjects,
        related_scripts=related_scripts or relatedScripts,
        tags=tags,
        zentao_urls=zentao_urls or zentaoUrls,
        decision_status=decision_status or decisionStatus,
        status=status,
        filename=filename,
        overwrite=overwrite,
        auto_index=autoIndex if autoIndex is not None else auto_index,
        vault_root=vault_root or vaultRoot,
    )


@mcp.tool()
def wiki_enrich(
    vault_root: str,
    page_path: str,
    stage: str = "prepare",
    links: list[dict[str, str]] | str | None = None,
) -> dict[str, Any]:
    """Enrich a wiki page with [[wikilinks]] to existing pages. Two stages: prepare returns a prompt, apply writes links."""
    return run_wiki_enrich(vault_root=vault_root, page_path=page_path, stage=stage, links=links)


@mcp.tool()
def wiki_page_merge(
    vault_root: str,
    page_path: str,
    incoming_frontmatter: dict[str, Any] | None = None,
    incoming_body: str = "",
    merged_body: str | None = None,
) -> dict[str, Any]:
    """Merge incoming content into an existing generated wiki page, preserving locked fields and unioning array fields."""
    return run_apply_page_merge(
        vault_root=vault_root,
        page_path=page_path,
        incoming_frontmatter=incoming_frontmatter or {},
        incoming_body=incoming_body,
        merged_body=merged_body,
    )


@mcp.tool()
def wiki_dedup(
    vault_root: str,
    stage: str = "detect",
    groups: list[dict[str, Any]] | str | None = None,
    not_duplicates: list[list[str]] | None = None,
) -> dict[str, Any]:
    """Detect and merge duplicate wiki pages. Stages: detect → confirm → merge."""
    return run_wiki_dedup(vault_root=vault_root, stage=stage, groups=groups, not_duplicates=not_duplicates)


@mcp.tool()
def wiki_insights(
    vault_root: str,
    project: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Analyze wiki graph structure: find orphans, bridges, and surprising connections."""
    return run_wiki_insights(vault_root=vault_root, project=project, limit=limit)


@mcp.tool()
def wiki_delete_source(
    vault_root: str,
    project: str,
    source_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a source and cascade-clean derived wiki pages and cross-references."""
    return run_wiki_delete_source(vault_root=vault_root, project=project, source_name=source_name, dry_run=dry_run)


@mcp.tool()
def wiki_research(
    vault_root: str,
    topic: str,
    stage: str = "prepare",
    search_results: list[dict[str, str]] | None = None,
    synthesis: str | None = None,
    language: str = "zh-CN",
    project: str | None = None,
) -> dict[str, Any]:
    """Deep research: synthesize web search results into a wiki page. Stages: prepare → apply."""
    return run_wiki_research(
        vault_root=vault_root, topic=topic, stage=stage,
        search_results=search_results, synthesis=synthesis,
        language=language, project=project,
    )


@mcp.tool()
def wiki_synthesis(
    vault_root: str,
    question: str,
    stage: str = "prepare",
    context_pages: list[dict[str, Any]] | None = None,
    synthesis: str | None = None,
    title: str | None = None,
    project: str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Persist a valuable query answer or analysis as a wiki synthesis page. Stages: prepare → apply."""
    return wiki_synthesis_tool(vault_root, question, stage, context_pages, synthesis, title, project, language)


@mcp.tool()
def wiki_ingest_batch(
    vault_root: str,
    action: str = "status",
    tasks: list[dict[str, str]] | None = None,
    task_id: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manage persistent ingest queue: enqueue, next, complete, fail, retry, status, cancel, clear_done."""
    return run_wiki_ingest_batch(vault_root=vault_root, action=action, tasks=tasks, task_id=task_id, result=result)


def wiki_verify_tool(
    vault_root: str,
    stage: str,
    project: str | None = None,
    page_path: str | None = None,
    verification_result: dict[str, Any] | str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    return run_wiki_verify(vault_root=vault_root, stage=stage, project=project, page_path=page_path, verification_result=verification_result, language=language)


@mcp.tool()
def wiki_verify(
    vault_root: str,
    stage: str,
    project: str | None = None,
    page_path: str | None = None,
    verification_result: dict[str, Any] | str | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Verify generated wiki pages against raw sources for faithfulness. Two-stage: prepare (returns LLM prompt) then apply (records results)."""
    return wiki_verify_tool(vault_root, stage, project, page_path, verification_result, language)


@mcp.tool()
def wiki_gap(
    vault_root: str,
    stage: str = "analyze",
    project: str | None = None,
    taxonomy: list[str] | None = None,
    language: str = "zh-CN",
) -> dict[str, Any]:
    """Analyze wiki coverage gaps: find missing concepts, shallow pages, dangling links, and uningested sources. Stages: analyze (report gaps), suggest (recommend actions)."""
    return run_wiki_gap(vault_root=vault_root, stage=stage, project=project, taxonomy=taxonomy, language=language)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

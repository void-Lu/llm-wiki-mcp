from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from netsuite_llm_wiki_mcp.note_writer import save_obsidian_note as run_write_note
from netsuite_llm_wiki_mcp.wiki_update import apply_update as run_apply_update
from netsuite_llm_wiki_mcp.wiki_update import preview_update as run_preview_update
from netsuite_llm_wiki_mcp.runtime_config import (
    ConfigRegistry,
    ResolvedVault,
    RuntimeConfigError,
    VAULT_ROOT_ENV,
    VaultSettings,
)
from netsuite_llm_wiki_mcp.runtime_provenance import RUNTIME_PROVENANCE
from netsuite_llm_wiki_mcp.wiki_batch import wiki_ingest_batch as run_wiki_ingest_batch
from netsuite_llm_wiki_mcp.wiki_files import wiki_status as run_wiki_status
from netsuite_llm_wiki_mcp.ingest_service import ingest_file as run_ingest_file
from netsuite_llm_wiki_mcp.knowledge_compiler import KnowledgeCompiler
from netsuite_llm_wiki_mcp.wiki_ingest import staged_wiki_ingest as run_staged_wiki_ingest
from netsuite_llm_wiki_mcp.wiki_query import DEFAULT_TOP_K, wiki_query as run_wiki_query
from netsuite_llm_wiki_mcp.query_pipeline import QueryFilters, run_query_v2
from netsuite_llm_wiki_mcp.vector_index import vector_settings_from_embedding
from netsuite_llm_wiki_mcp.archive_service import ArchiveService


@dataclass(frozen=True)
class ToolVaultResolution:
    root: Path
    logical_name: str
    resolved: ResolvedVault
    warnings: tuple[str, ...] = ()


def _load_registry() -> ConfigRegistry:
    # Configuration is an immutable startup snapshot. It is deliberately not
    # reloaded by individual MCP requests.
    return ConfigRegistry.from_file()


CONFIG_REGISTRY = _load_registry()
mcp = FastMCP("netsuite-llm-wiki-mcp")
mcp._mcp_server.version = RUNTIME_PROVENANCE.server_version


def attach_warnings(payload: dict[str, Any], warnings: tuple[str, ...] | list[str]) -> dict[str, Any]:
    if not warnings:
        return payload
    result = dict(payload)
    result["warnings"] = [*result.get("warnings", []), *warnings]
    return result


def _legacy_vault(value: str, registry: ConfigRegistry) -> ToolVaultResolution:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError("legacy vault_root must be an absolute path", code="invalid_vault_root", config_path=registry.config_path)
    root = path.resolve()
    name = next((item.name for item in registry.config.vaults.values() if item.root == root), root.name)
    settings = registry.config.vaults.get(name)
    resolved = ResolvedVault(name, root, settings if settings is not None else VaultSettings(name, root), "legacy")
    return ToolVaultResolution(root, name, resolved, ("deprecated_vault_root",))


def resolve_tool_vault(*, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, registry: ConfigRegistry | None = None) -> ToolVaultResolution:
    """Resolve all public vault selectors in one place with no silent precedence."""
    active_registry = registry or CONFIG_REGISTRY
    legacy_values = [value for value in (vault_root, vaultRoot) if value]
    if len(set(legacy_values)) > 1:
        raise RuntimeConfigError("vault_root and vaultRoot disagree", code="ambiguous_vault_selector", config_path=active_registry.config_path)
    if vault and legacy_values:
        raise RuntimeConfigError("vault cannot be combined with vault_root", code="ambiguous_vault_selector", config_path=active_registry.config_path)
    if legacy_values:
        return _legacy_vault(legacy_values[0], active_registry)
    resolved = active_registry.resolve_vault(vault)
    return ToolVaultResolution(resolved.root, resolved.name, resolved)


def _tool_error(exc: RuntimeConfigError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "error": str(exc)}


def _resolve_vault_root(vault_root: str | None = None, vaultRoot: str | None = None) -> str:
    """Deprecated internal shim retained for CLI/internal callers only."""
    return str(resolve_tool_vault(vault_root=vault_root, vaultRoot=vaultRoot).root)


def wiki_status_tool(vault_root: str) -> dict[str, Any]:
    """Domain-level status helper; it intentionally accepts an explicit root."""
    return run_wiki_status(vault_root)


def wiki_query_tool(vault_root: str, question: str, project: str | None = None, top_k: int = DEFAULT_TOP_K, include_content: bool = True, context_window_tokens: int = 16_000, include_context_pack: bool = True, chat_history: list[dict[str, str]] | None = None, language: str = "zh-CN", enable_vector: bool = False, vector_config: dict[str, Any] | None = None, max_graph_hops: int = 2, include_raw_sources: bool = False, filter_type: str | None = None, filter_tags: list[str] | None = None, retrieval_mode: str = "hybrid") -> dict[str, Any]:
    """Compatibility helper for internal callers; not an MCP schema."""
    return run_wiki_query(vault_root=vault_root, question=question, project=project, top_k=top_k, include_content=include_content, context_window_tokens=context_window_tokens, include_context_pack=include_context_pack, chat_history=chat_history, language=language, enable_vector=enable_vector, vector_config=vector_config, max_graph_hops=max_graph_hops, include_raw_sources=include_raw_sources, filter_type=filter_type, filter_tags=filter_tags, retrieval_mode=retrieval_mode)


def wiki_write_note_tool(*, note_type: str, title: str, content: str, vault_root: str, **kwargs: Any) -> dict[str, Any]:
    return run_write_note(note_type=note_type, title=title, content=content, vault_root=vault_root, **kwargs)


def _register(function: Any) -> Any:
    return mcp.tool()(function)


@_register
def wiki_status(detail: str = "summary", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
    """Return read-only health, index and policy status for a logical vault."""
    if detail not in {"summary", "indexes", "generation", "archive"}:
        return {"ok": False, "code": "invalid_status_detail", "error": "detail must be summary, indexes, generation, or archive"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    status = dict(wiki_status_tool(str(resolution.root)))
    status.pop("vault_root", None)
    codegraph = status.get("codegraph")
    if isinstance(codegraph, dict):
        codegraph.pop("executable", None)
    status["vault"] = resolution.logical_name
    status["config"] = CONFIG_REGISTRY.public_status(resolution.resolved)
    archive_status = ArchiveService(resolution.root).status()
    status["archive_index"] = {"enabled": resolution.resolved.settings.archive.archive_index_enabled, **archive_status["archive_index"]}
    status["archive_operations"] = archive_status["operations"]
    if detail == "indexes":
        status = {key: status[key] for key in ("ok", "vault", "vector", "retrieval", "config", "version", "runtime") if key in status}
    elif detail == "generation":
        status = {key: status[key] for key in ("ok", "vault", "queue", "config", "version", "runtime") if key in status}
    elif detail == "archive":
        status = {key: status[key] for key in ("ok", "vault", "archive_index", "archive_operations", "config", "version", "runtime") if key in status}
    return attach_warnings(status, resolution.warnings)


QueryScope = Literal["auto", "knowledge", "history", "all", "archive"]


@_register
def wiki_query(question: str, scope: QueryScope = "auto", project: str | None = None, filters: dict[str, Any] | None = None, top_k: int = DEFAULT_TOP_K, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
    """Query a vault using its immutable retrieval and context profile."""
    if scope not in {"auto", "knowledge", "history", "all", "archive"}:
        return {"ok": False, "code": "invalid_scope", "error": "scope must be auto, knowledge, history, all, or archive"}
    if not question:
        return {"ok": False, "code": "missing_question", "error": "question is required"}
    if top_k < 1 or top_k > 100:
        return {"ok": False, "code": "invalid_top_k", "error": "top_k must be between 1 and 100"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    settings = resolution.resolved.settings.retrieval
    filter_values = filters or {}
    if not isinstance(filter_values, dict) or set(filter_values) - {"type", "tags"}:
        return {"ok": False, "code": "invalid_filters", "error": "filters may only contain type and tags"}
    try:
        typed_filters = QueryFilters.from_mapping(filter_values)
    except ValueError as exc:
        return {"ok": False, "code": "invalid_filters", "error": str(exc)}
    if settings.query_version == "v1":
        legacy = run_wiki_query(
            resolution.root, question, project, top_k,
            include_content=True,
            context_window_tokens=settings.context.hard_budget_tokens,
            include_context_pack=settings.context.response_mode == "context_pack",
            enable_vector=settings.embedding.enabled,
            vector_settings=vector_settings_from_embedding(resolution.root, settings.embedding),
            filter_type=typed_filters.type,
            filter_tags=list(typed_filters.tags),
            scope="archive" if scope == "archive" else "active",
        )
        legacy["scope"] = scope
        legacy.setdefault("warnings", []).append("query_v1_legacy_feature_flag")
        return attach_warnings(legacy, resolution.warnings)
    result = run_query_v2(
        resolution.root,
        question,
        scope=scope,
        project=project,
        filters=typed_filters,
        top_k=top_k,
        hard_budget_tokens=settings.context.hard_budget_tokens,
        embedding=settings.embedding,
        telemetry=resolution.resolved.settings.telemetry,
    )
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_write_note(title: str, content: str, note_type: str | None = None, noteType: str | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, project: str | None = None, domain: str | None = None, tags: list[str] | None = None, filename: str | None = None) -> dict[str, Any]:
    """Create a new manual knowledge page. Existing files are never overwritten."""
    selected_type = note_type or noteType
    if not selected_type:
        return {"ok": False, "code": "missing_note_type", "error": "note_type is required"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    result = wiki_write_note_tool(note_type=selected_type, title=title, content=content, project=project, domain=domain, tags=tags, filename=filename, overwrite=False, auto_index=True, vault_root=str(resolution.root))
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_ingest(source_path: str, source_name: str, project: str = "", source_type: str = "file", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ingest one explicit file; directory/batch orchestration belongs to CLI/workers."""
    del metadata
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    result = run_ingest_file(vault_root=resolution.root, project=project, source_name=source_name, source_path=source_path, source_type=source_type)
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_update(page_path: str, incoming_body: str, action: str = "preview", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, incoming_frontmatter: dict[str, Any] | None = None, plan_id: str | None = None, expected_hash: str | None = None) -> dict[str, Any]:
    """Preview or apply a controlled update to an existing page."""
    if action not in {"preview", "apply"}:
        return {"ok": False, "code": "invalid_action", "error": "action must be preview or apply"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    if action == "preview":
        result = run_preview_update(resolution.root, page_path, incoming_body, incoming_frontmatter)
    else:
        result = run_apply_update(resolution.root, page_path, incoming_body, incoming_frontmatter=incoming_frontmatter, plan_id=plan_id, expected_hash=expected_hash)
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_archive(target: str, reason: str = "manual", cascade: bool = False, action: str = "plan", plan_id: str | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
    """Plan/apply archive lifecycle operations; purge is intentionally not public."""
    if action not in {"plan", "apply"}:
        return {"ok": False, "code": "invalid_action", "error": "action must be plan or apply"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    service = ArchiveService(resolution.root, actor="mcp")
    try:
        result = service.plan_archive(target, reason=reason, cascade=cascade) if action == "plan" else service.apply(plan_id or "")
    except Exception as exc:
        result = {"ok": False, "code": getattr(exc, "code", "archive_apply_failed"), "error": str(exc)}
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_restore(archive_id: str, action: str = "plan", plan_id: str | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
    """Plan/apply restoration of an immutable archive bundle."""
    if action not in {"plan", "apply"}:
        return {"ok": False, "code": "invalid_action", "error": "action must be plan or apply"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    service = ArchiveService(resolution.root, actor="mcp")
    try:
        result = service.plan_restore(archive_id) if action == "plan" else service.apply(plan_id or "")
    except Exception as exc:
        result = {"ok": False, "code": getattr(exc, "code", "restore_apply_failed"), "error": str(exc)}
    return attach_warnings(result, resolution.warnings)


if CONFIG_REGISTRY.config.tool_profile == "worker":
    @_register
    def wiki_generation(action: str = "status", job_id: str | None = None, lease_token: str | None = None, result: dict[str, Any] | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
        """Worker-only generation queue bridge; not registered in the core profile."""
        try:
            resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
        except RuntimeConfigError as exc:
            return _tool_error(exc)
        compiler = KnowledgeCompiler(resolution.root)
        if action == "status":
            payload = compiler.queue.status()
        elif action == "claim":
            payload = compiler.claim("mcp-worker")
        elif action == "apply" and job_id and lease_token and result is not None:
            payload = compiler.apply_capsule(job_id, lease_token, result)
        elif action == "fail" and job_id and lease_token:
            payload = compiler.queue.fail(job_id, lease_token, "worker_failed")
        elif action == "release" and job_id and lease_token:
            payload = compiler.queue.release(job_id, lease_token)
        elif action not in {"apply", "fail", "release"}:
            return {"ok": False, "code": "invalid_action", "error": "invalid generation action"}
        else:
            payload = {"ok": False, "code": "missing_generation_arguments"}
        return attach_warnings(payload, resolution.warnings)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

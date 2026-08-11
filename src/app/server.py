from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import wraps
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, InputRequiredResult, TextContent, ToolAnnotations

from app.public_contracts import PublicError, PublicResult, public_error, public_error_from_exception, project_public_result
from archive.archive_status_reader import ArchiveStatusReader
from wiki.note_writer import save_obsidian_note as run_write_note
from wiki.wiki_update import apply_update as run_apply_update
from wiki.wiki_update import preview_update as run_preview_update
from runtime.runtime_config import (
    ConfigRegistry,
    ResolvedVault,
    RuntimeConfigError,
    VaultSettings,
)
from runtime.runtime_provenance import RUNTIME_PROVENANCE
from wiki.wiki_files import wiki_status as run_wiki_status
from wiki.ingest_service import ingest_file as run_ingest_file
from wiki.wiki_query import DEFAULT_TOP_K
from retrieval.query_cancellation import QueryCancelled, QueryCancellationContext, QueryExecutionRegistry
from retrieval.query_pipeline import QueryFilters, run_query_v2
from retrieval.query_telemetry import QueryTelemetry
from wiki.content_catalog import (
    DEFAULT_BODY_BUDGET,
    DEFAULT_CATALOG_PAGE_SIZE,
    ContentCatalogService,
)
from archive.archive_models import ARCHIVE_REASONS, is_archive_reason
from archive.archive_service import ArchiveService
from codegraph.codegraph_sync import CodeGraphSyncError, sync_codegraph as run_codegraph_sync


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
QUERY_TIMEOUT_SECONDS = 300
_QUERY_REGISTRIES: dict[tuple[str, int, float], QueryExecutionRegistry] = {}
_QUERY_REGISTRY_LOCK = threading.Lock()


class StrictMCPServer(MCPServer):
    """MCPServer with a privacy-safe rejection for unknown input fields."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> CallToolResult | InputRequiredResult:
        tool_manager = getattr(self, "_tool_manager", None)
        tool = tool_manager.get_tool(name) if tool_manager is not None else None
        if tool is not None:
            allowed = set(tool.fn_metadata.arg_model.model_fields)
            allowed.update(
                field.alias
                for field in tool.fn_metadata.arg_model.model_fields.values()
                if field.alias
            )
            unknown = set(arguments) - allowed
            if unknown:
                payload = public_error("validation_error")
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                    structured_content=payload,
                    is_error=True,
                )
        result = await super().call_tool(name, arguments, context)
        return result


mcp = StrictMCPServer(
    "llm-wiki-mcp",
    version=RUNTIME_PROVENANCE.server_version,
)


def attach_warnings(payload: dict[str, Any], warnings: tuple[str, ...] | list[str]) -> dict[str, Any]:
    if not warnings:
        return payload
    result = dict(payload)
    result["warnings"] = [*result.get("warnings", []), *warnings]
    return result


def attach_no_results_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Make an empty successful query explicit without broadening its source boundary."""
    if payload.get("ok") is not True or payload.get("results") != [] or "code" in payload:
        return payload
    pipeline = payload.get("pipeline")
    if isinstance(pipeline, dict):
        discovery = pipeline.get("discovery")
        batch = pipeline.get("batch")
        if (
            isinstance(discovery, dict)
            and bool(discovery.get("candidate_entities"))
        ) or (
            isinstance(batch, dict)
            and batch.get("status") not in {None, "not_triggered"}
        ):
            return payload
    result = dict(payload)
    result["code"] = "no_results"
    result["message"] = "No indexed documentation matched the query."
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
    return public_error_from_exception(exc, fallback_code=exc.code)


def wiki_status_tool(vault_root: str) -> dict[str, Any]:
    """Domain-level status helper; it intentionally accepts an explicit root."""
    return run_wiki_status(vault_root)


def wiki_write_note_tool(*, note_type: str, title: str, content: str, vault_root: str, **kwargs: Any) -> dict[str, Any]:
    return run_write_note(note_type=note_type, title=title, content=content, vault_root=vault_root, **kwargs)


_TOOL_ANNOTATIONS = {
    "wiki_status": ToolAnnotations(title="Read wiki status", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    "wiki_query": ToolAnnotations(title="Query wiki", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    "wiki_list": ToolAnnotations(title="List wiki content", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    "wiki_get": ToolAnnotations(title="Read wiki content", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    "wiki_ingest": ToolAnnotations(title="Ingest a source", read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    "wiki_codegraph_import": ToolAnnotations(title="Import CodeGraph", read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    "wiki_write_note": ToolAnnotations(title="Write a wiki note", read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
    "wiki_update": ToolAnnotations(title="Update a wiki page", read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
    "wiki_archive": ToolAnnotations(title="Archive a wiki page", read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
    "wiki_restore": ToolAnnotations(title="Restore a wiki page", read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
}


def _publicize_tool_result(value: object, kwargs: Mapping[str, object]) -> object:
    if isinstance(value, PublicResult):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        return value
    logical_vault = kwargs.get("vault")
    vault_root_value = kwargs.get("vault_root") or kwargs.get("vaultRoot")
    vault_root = vault_root_value if isinstance(vault_root_value, (str, Path)) else None
    if not isinstance(logical_vault, str):
        try:
            logical_vault = resolve_tool_vault(
                vault_root=str(vault_root) if vault_root is not None else None,
                registry=CONFIG_REGISTRY,
            ).logical_name
        except Exception:  # noqa: BLE001 - projection must never expose resolver details
            logical_vault = None
    projected = project_public_result(
        payload,
        logical_vault=logical_vault if isinstance(logical_vault, str) else None,
        vault_root=vault_root,
    )
    if projected.get("ok") is not False:
        return projected
    public_error = PublicError.from_payload(projected)
    extras = {
        key: item
        for key, item in projected.items()
        if key not in {"ok", "code", "message", "error", "retryable", "correlation_id"}
    }
    return {**public_error.to_payload(), **extras}


def _register(function: Any) -> Any:
    """Register every public tool with one schema/privacy/error boundary."""

    @wraps(function)
    def registered(*args: object, **kwargs: object) -> object:
        try:
            value = function(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - MCP boundary must be stable
            return public_error_from_exception(exc)
        return _publicize_tool_result(value, kwargs)

    registered_tool = mcp.tool(
        annotations=_TOOL_ANNOTATIONS.get(
            function.__name__,
            ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        ),
        structured_output=True,
    )(registered)
    tool_manager = getattr(mcp, "_tool_manager", None)
    tool = tool_manager.get_tool(function.__name__) if tool_manager is not None else None
    if tool is not None:
        # MCP SDK argument models default to silently ignoring unknown keys.
        # The public contract is intentionally strict, so make the generated
        # schema and runtime validator agree at registration time.
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config["extra"] = "forbid"
        argument_model.model_rebuild(force=True)
    return registered_tool


def _query_registry(
    *,
    vault_key: str,
    max_concurrency: int,
    cancel_grace_seconds: float,
) -> QueryExecutionRegistry:
    key = (vault_key, max_concurrency, cancel_grace_seconds)
    with _QUERY_REGISTRY_LOCK:
        registry = _QUERY_REGISTRIES.get(key)
        if registry is None:
            registry = QueryExecutionRegistry(
                max_concurrency=max_concurrency,
                cancel_grace_seconds=cancel_grace_seconds,
            )
            _QUERY_REGISTRIES[key] = registry
        return registry


def _with_timeout(
    function: Any,
    timeout_seconds: float,
    *,
    vault_key: str = "default",
    max_concurrency: int = 4,
    cancel_grace_seconds: float = 0.25,
) -> Any:
    """Run a query in the bounded cooperative execution registry."""

    registry = _query_registry(
        vault_key=vault_key,
        max_concurrency=max_concurrency,
        cancel_grace_seconds=cancel_grace_seconds,
    )
    return registry.run(function, timeout_seconds=timeout_seconds)


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
    archive_status = ArchiveStatusReader(resolution.root).status()
    status["archive_index"] = {"enabled": resolution.resolved.settings.archive.archive_index_enabled, **archive_status["archive_index"]}
    status["archive_operations"] = archive_status["operations"]
    status["archive_state"] = {
        "state": archive_status.get("state", "unknown"),
        "code": archive_status.get("code", "archive_state_unavailable"),
    }
    if archive_status.get("missing_tables"):
        status["archive_state"]["missing_tables"] = archive_status["missing_tables"]
    if archive_status.get("missing_columns"):
        status["archive_state"]["missing_columns"] = archive_status["missing_columns"]
    execution = resolution.resolved.settings.retrieval.execution
    execution_status = _query_registry(
        vault_key=f"{resolution.logical_name}:{resolution.root}",
        max_concurrency=execution.max_concurrency,
        cancel_grace_seconds=execution.cancel_grace_seconds,
    ).status()
    status["query_execution"] = {
        "active": execution_status["active"],
        "pending": execution_status["pending"],
    }
    if detail == "indexes":
        status = {key: status[key] for key in ("ok", "vault", "vector", "retrieval", "query_execution", "config", "version", "runtime") if key in status}
    elif detail == "generation":
        status = {key: status[key] for key in ("ok", "vault", "queue", "query_execution", "config", "version", "runtime") if key in status}
    elif detail == "archive":
        status = {key: status[key] for key in ("ok", "vault", "archive_index", "archive_operations", "archive_state", "config", "version", "runtime") if key in status}
    return attach_warnings(status, resolution.warnings)


@_register
def wiki_list(
    store_scope: Literal["active", "raw", "archive"] = "active",
    page_size: int = DEFAULT_CATALOG_PAGE_SIZE,
    cursor: str | None = None,
    filters: dict[str, Any] | None = None,
    vault: str | None = None,
    vault_root: str | None = None,
    vaultRoot: str | None = None,
    storeScope: Literal["active", "raw", "archive"] | None = None,
    pageSize: int | None = None,
) -> dict[str, Any]:
    """List metadata from one explicitly selected physical content scope."""

    if storeScope is not None:
        store_scope = storeScope
    if pageSize is not None:
        page_size = pageSize
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
        result = ContentCatalogService(resolution.root, logical_vault=resolution.logical_name).list_items(
            scope=store_scope,
            filters=filters,
            page_size=page_size,
            cursor=cursor,
        )
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_get(
    content_ref: str | None = None,
    include_body: bool = False,
    max_bytes: int = DEFAULT_BODY_BUDGET,
    cursor: str | None = None,
    vault: str | None = None,
    vault_root: str | None = None,
    vaultRoot: str | None = None,
    contentRef: str | None = None,
    includeBody: bool | None = None,
    maxBytes: int | None = None,
) -> dict[str, Any]:
    """Read metadata for an opaque content reference, with opt-in bounded body."""

    if contentRef is not None:
        content_ref = contentRef
    if includeBody is not None:
        include_body = includeBody
    if maxBytes is not None:
        max_bytes = maxBytes
    if not content_ref:
        return {"ok": False, "code": "invalid_content_ref", "error": "content_ref is required"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
        result = ContentCatalogService(resolution.root, logical_vault=resolution.logical_name).get_item(
            content_ref,
            include_body=include_body,
            max_bytes=max_bytes,
            cursor=cursor,
        )
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    return attach_warnings(result, resolution.warnings)


QueryScope = Literal["auto", "knowledge", "history", "all", "archive", "raw"]
_SAFE_EXPANSION = re.compile(r"^[a-z0-9_\-/\.\s]+$")


def _validate_expansion_terms(value: object) -> tuple[dict[str, list[str]] | None, str | None]:
    """Normalize and validate caller-supplied query-expansion maps.

    The MCP server itself never talks to a model: the agent driving the tool
    resolves fuzzy terms with its own model and passes the resulting map back
    here.  Keys are the query's original terms and values are the spellings
    that may appear in vault documents (for example ``sl`` ->
    ``["suitelet"]``).  Values are case-folded and restricted to safe FTS
    characters so they can be embedded in MATCH expressions verbatim.
    """

    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "expansion_terms must be an object mapping terms to term lists"
    result: dict[str, list[str]] = {}
    for raw_key, raw_values in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            return None, "expansion_terms keys must be non-empty strings"
        if not isinstance(raw_values, list) or not raw_values or not all(isinstance(item, str) and item.strip() for item in raw_values):
            return None, f"expansion_terms[{raw_key!r}] must be a non-empty list of strings"
        key = raw_key.strip().casefold()
        if not _SAFE_EXPANSION.fullmatch(key):
            return None, f"expansion_terms key {raw_key!r} contains unsupported characters"
        aliases: list[str] = []
        for raw_alias in raw_values:
            alias = raw_alias.strip().casefold()
            if not _SAFE_EXPANSION.fullmatch(alias):
                return None, f"expansion_terms value {raw_alias!r} contains unsupported characters"
            if alias != key and alias not in aliases:
                aliases.append(alias)
        if aliases:
            result[key] = aliases
    return result or None, None


def _run_wiki_query(
    question: str,
    scope: QueryScope,
    project: str | None,
    filters: dict[str, Any] | None,
    top_k: int,
    expansion_terms: dict[str, list[str]] | None,
    vault: str | None,
    vault_root: str | None,
    vaultRoot: str | None,
    confirmation_token: str | None,
    cancellation: QueryCancellationContext | None = None,
) -> dict[str, Any]:
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    settings = resolution.resolved.settings.retrieval
    telemetry_settings = resolution.resolved.settings.telemetry
    telemetry_recorder = QueryTelemetry(resolution.root) if telemetry_settings.enabled else None
    query_started = time.perf_counter()
    if cancellation is not None and telemetry_recorder is not None:
        def record_cancellation(event: object) -> None:
            cancellation_event = event
            telemetry_recorder.finish_once(
                question=question,
                scope=scope,
                project=project,
                passage_ids=(),
                fallback_level="",
                token_count=0,
                latency_ms=(time.perf_counter() - query_started) * 1_000,
                retention_days=telemetry_settings.retention_days,
                outcome="timeout" if getattr(cancellation_event, "code", "") == "query_timeout" else "cancelled",
                cancelled_stage=str(getattr(cancellation_event, "cancelled_stage", "")),
                worker_state="cancellation_pending" if getattr(cancellation_event, "code", "") == "query_timeout" else "cancelled",
            )

        cancellation.set_cancel_handler(record_cancellation)
    filter_values = filters or {}
    if not isinstance(filter_values, dict) or set(filter_values) - {"type", "tags", "path_prefix"}:
        return {"ok": False, "code": "invalid_filters", "error": "filters may only contain type, tags, and path_prefix"}
    try:
        typed_filters = QueryFilters.from_mapping(filter_values)
    except ValueError as exc:
        return {"ok": False, "code": "invalid_filters", "error": str(exc)}
    if typed_filters.type and typed_filters.type.casefold() == "code_fact" and not project:
        return {"ok": False, "code": "project_required_for_codegraph", "error": "project is required to query CodeGraph pages"}
    retrieval_mode = "vector" if not settings.lexical_enabled else "hybrid" if settings.embedding.enabled else "lexical"
    try:
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
            lexical_enabled=settings.lexical_enabled,
            retrieval_mode=retrieval_mode,
            expansion_terms=expansion_terms,
            confirmation_token=confirmation_token,
            cancellation=cancellation,
            telemetry_recorder=telemetry_recorder,
        )
    except QueryCancelled:
        raise
    except Exception:
        if telemetry_recorder is not None:
            telemetry_recorder.finish_once(
                question=question,
                scope=scope,
                project=project,
                passage_ids=(),
                fallback_level="",
                token_count=0,
                latency_ms=(time.perf_counter() - query_started) * 1_000,
                retention_days=telemetry_settings.retention_days,
                outcome="failed",
                worker_state="failed",
            )
        raise
    return attach_warnings(attach_no_results_outcome(result), resolution.warnings)


@_register
def wiki_query(question: str, scope: QueryScope = "auto", project: str | None = None, filters: dict[str, Any] | None = None, top_k: int = DEFAULT_TOP_K, expansion_terms: dict[str, list[str]] | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, confirmation_token: str | None = None) -> dict[str, Any]:
    """Query a vault using its immutable retrieval and context profile.

    ``filters`` accepts ``type`` (string), ``tags`` (list of strings), and
    ``path_prefix`` (string, e.g. ``"wiki/concepts/netsuite-script-types/"``)
    to restrict results to a specific directory.

    When the vault has no indexed answer, the response includes
    ``expansion_suggestions``: the Latin terms in the question that do not
    appear in any page title and have no spelling variant yet.  Resolve those
    terms with your own model (for example ``sl`` -> ``suitelet``) and retry
    with ``expansion_terms`` set to the mapping so the relaxed recovery can
    reach documents that use different vocabulary.

    When the response includes ``pipeline.discovery`` with a
    ``confirmation_token``, pass the same ``question`` and ``confirmation_token``
    back to batch-resolve all discovered entities' API content in one follow-up
    call instead of querying each entity individually.
    """
    if scope not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        return {"ok": False, "code": "invalid_scope", "error": "scope must be auto, knowledge, history, all, archive, or raw"}
    if not question:
        return {"ok": False, "code": "missing_question", "error": "question is required"}
    if top_k < 1 or top_k > 40:
        return {"ok": False, "code": "invalid_top_k", "error": "top_k must be between 1 and 40"}
    normalized_expansion, expansion_error = _validate_expansion_terms(expansion_terms)
    if expansion_error:
        return {"ok": False, "code": "invalid_expansion_terms", "error": expansion_error}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    execution = resolution.resolved.settings.retrieval.execution
    return _with_timeout(
        lambda cancellation: _run_wiki_query(
            question,
            scope,
            project,
            filters,
            top_k,
            normalized_expansion,
            vault,
            vault_root,
            vaultRoot,
            confirmation_token,
            cancellation,
        ),
        QUERY_TIMEOUT_SECONDS,
        vault_key=f"{resolution.logical_name}:{resolution.root}",
        max_concurrency=execution.max_concurrency,
        cancel_grace_seconds=execution.cancel_grace_seconds,
    )


@_register
def wiki_write_note(title: str, content: str, note_type: str | None = None, noteType: str | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, project: str | None = None, domain: str | None = None, tags: list[str] | None = None, filename: str | None = None, chat_metadata: dict[str, Any] | None = None, chat_derived: bool = False, chat_sources: list[dict[str, str]] | None = None, related_pages: list[dict[str, Any]] | None = None, related_pages_heading: str | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    """Create a manual page, optionally linking adopted Wiki pages and raw sources.

    ``related_pages`` accepts existing ``wiki/**`` paths and creates a
    ``## 参考来源`` wikilink section (override with ``related_pages_heading``).
    Raw provenance belongs in ``sources``; chat-derived pages continue to use
    ``chat_sources``.
    """
    selected_type = note_type or noteType
    if not selected_type:
        return {"ok": False, "code": "missing_note_type", "error": "note_type is required"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    result = wiki_write_note_tool(note_type=selected_type, title=title, content=content, project=project, domain=domain, tags=tags, filename=filename, chat_metadata=chat_metadata, chat_derived=chat_derived, chat_sources=chat_sources, related_pages=related_pages, related_pages_heading=related_pages_heading, sources=sources, overwrite=False, auto_index=True, vault_root=str(resolution.root))
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_ingest(source_path: str, source_name: str, project: str = "", source_type: str = "file", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
    """Ingest one explicit file; text is indexed and other files become raw assets."""
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    result = run_ingest_file(vault_root=resolution.root, project=project, source_name=source_name, source_path=source_path, source_type=source_type)
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_codegraph_import(sync: Literal["sync"] = "sync", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, workspace_root: str | None = None) -> dict[str, Any]:
    """Synchronise the current workspace's CodeGraph snapshot into the Wiki.

    workspace_root is required — pass it explicitly or set the
    LLM_WIKI_WORKSPACE_ROOT environment variable (e.g. the client's
    workspace folder); it never falls back to the server process cwd.
    """
    if sync != "sync":
        return {"ok": False, "code": "invalid_codegraph_operation", "error": "only sync is supported"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    try:
        result = run_codegraph_sync(resolution.root, workspace_root=workspace_root)
    except CodeGraphSyncError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - keep the MCP boundary structured
        result = {"ok": False, "code": "codegraph_sync_failed", "error": str(exc)}
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_update(page_path: str, incoming_body: str, action: str = "preview", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, incoming_frontmatter: dict[str, Any] | None = None, plan_id: str | None = None, expected_hash: str | None = None, related_pages: list[dict[str, Any]] | None = None, related_pages_heading: str | None = None) -> dict[str, Any]:
    """Preview or apply a controlled update, optionally appending Wiki links."""
    if action not in {"preview", "apply"}:
        return {"ok": False, "code": "invalid_action", "error": "action must be preview or apply"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    if action == "preview":
        result = run_preview_update(resolution.root, page_path, incoming_body, incoming_frontmatter, related_pages=related_pages, related_pages_heading=related_pages_heading)
    else:
        result = run_apply_update(resolution.root, page_path, incoming_body, incoming_frontmatter=incoming_frontmatter, plan_id=plan_id, expected_hash=expected_hash, related_pages=related_pages, related_pages_heading=related_pages_heading)
    return attach_warnings(result, resolution.warnings)


@_register
def wiki_archive(target: str, reason: str = "manual", cascade: bool = False, action: str = "plan", plan_id: str | None = None, vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None) -> dict[str, Any]:
    """Plan/apply archive lifecycle operations; purge is intentionally not public."""
    if action not in {"plan", "apply"}:
        return {"ok": False, "code": "invalid_action", "error": "action must be plan or apply"}
    if action == "plan" and not is_archive_reason(reason):
        return {"ok": False, "code": "invalid_archive_reason", "error": f"reason must be one of: {', '.join(sorted(ARCHIVE_REASONS))}"}
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    try:
        service = ArchiveService(resolution.root, actor="mcp")
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
    try:
        service = ArchiveService(resolution.root, actor="mcp")
        result = service.plan_restore(archive_id) if action == "plan" else service.apply(plan_id or "")
    except Exception as exc:
        result = {"ok": False, "code": getattr(exc, "code", "restore_apply_failed"), "error": str(exc)}
    return attach_warnings(result, resolution.warnings)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

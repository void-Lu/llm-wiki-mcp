from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import queue
import re
import threading
from typing import Any, Literal

from mcp.server import MCPServer

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
from retrieval.query_pipeline import QueryFilters, run_query_v2
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
mcp = MCPServer(
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
    return {"ok": False, "code": exc.code, "error": str(exc)}


def wiki_status_tool(vault_root: str) -> dict[str, Any]:
    """Domain-level status helper; it intentionally accepts an explicit root."""
    return run_wiki_status(vault_root)


def wiki_write_note_tool(*, note_type: str, title: str, content: str, vault_root: str, **kwargs: Any) -> dict[str, Any]:
    return run_write_note(note_type=note_type, title=title, content=content, vault_root=vault_root, **kwargs)


def _register(function: Any) -> Any:
    return mcp.tool()(function)


def _with_timeout(function: Any, timeout_seconds: float) -> Any:
    """Run a synchronous domain call under a wall-clock deadline.

    MCP tool calls are synchronous, so a hung retrieval (corrupt index, slow
    vector model, graph expansion over a large repository) would otherwise
    block the tool indefinitely.  The worker is a daemon thread, so a timeout
    never prevents interpreter shutdown; the caller receives a structured
    error instead of waiting forever.
    """

    result_box: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_box.put(("ok", function()))
        except BaseException as exc:  # noqa: BLE001 - transport errors back to the caller
            result_box.put(("error", exc))

    thread = threading.Thread(target=runner, daemon=True, name="wiki-query")
    thread.start()
    try:
        status, value = result_box.get(timeout=timeout_seconds)
    except queue.Empty:
        return {
            "ok": False,
            "code": "query_timeout",
            "error": f"query exceeded the {timeout_seconds:.0f}s execution limit",
        }
    if status == "ok":
        return value
    raise value


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
) -> dict[str, Any]:
    try:
        resolution = resolve_tool_vault(vault=vault, vault_root=vault_root, vaultRoot=vaultRoot)
    except RuntimeConfigError as exc:
        return _tool_error(exc)
    settings = resolution.resolved.settings.retrieval
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
    )
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
    return _with_timeout(
        lambda: _run_wiki_query(question, scope, project, filters, top_k, normalized_expansion, vault, vault_root, vaultRoot, confirmation_token),
        QUERY_TIMEOUT_SECONDS,
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
def wiki_ingest(source_path: str, source_name: str, project: str = "", source_type: str = "file", vault: str | None = None, vault_root: str | None = None, vaultRoot: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ingest one explicit file; text is indexed and other files become raw assets."""
    del metadata
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

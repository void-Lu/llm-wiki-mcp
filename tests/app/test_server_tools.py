from __future__ import annotations

import importlib.util
import inspect
import json
import sqlite3
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import Client

from runtime.runtime_config import VAULT_ROOT_ENV, ConfigRegistry, RuntimeConfigError, write_global_config
from runtime.runtime_provenance import RUNTIME_PROVENANCE
import app.server as server_module
from wiki.content_catalog import MAX_BODY_BUDGET, MAX_TOTAL_BODY_BUDGET
from wiki.content_reference import ContentRefV1
from app.server import (
    attach_no_results_outcome,
    attach_warnings,
    mcp,
    resolve_tool_vault,
    _validate_expansion_terms,
    wiki_archive,
    wiki_restore,
    wiki_query,
    wiki_update,
    wiki_write_note,
    wiki_list,
    wiki_get,
)


CORE_TOOLS = {
    "wiki_status",
    "wiki_ingest",
    "wiki_write_note",
    "wiki_update",
    "wiki_query",
    "wiki_list",
    "wiki_get",
    "wiki_archive",
    "wiki_restore",
}


def _registry(tmp_path: Path) -> tuple[ConfigRegistry, Path]:
    root = tmp_path / "vault"
    root.mkdir()
    config_path = tmp_path / "config" / "config.yaml"
    write_global_config(config_path, vault_name="primary", vault_root=root)
    return ConfigRegistry.from_file(config_path), root.resolve()


async def _registered_tool_names(server: object) -> set[str]:
    async with Client(server) as client: # type: ignore
        return {tool.name for tool in (await client.list_tools()).tools}


def _tool_result_payload(result: object) -> dict[str, object]:
    structured_content = getattr(result, "structured_content", None)
    if isinstance(structured_content, dict):
        return structured_content
    for content in getattr(result, "content", []):
        text = getattr(content, "text", None)
        if isinstance(text, str):
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
    raise AssertionError("MCP tool result did not contain an object payload")


def test_mcp_initialization_version_matches_runtime_provenance() -> None:
    async def assert_handshake() -> None:
        async with Client(mcp) as client:
            assert client.server_info is not None
            assert client.server_info.name == "llm-wiki-mcp"
            assert client.server_info.version == RUNTIME_PROVENANCE.server_version

    anyio.run(assert_handshake)


def test_registered_tools_match_core_public_surface() -> None:
    assert anyio.run(_registered_tool_names, mcp) == CORE_TOOLS


def test_validate_expansion_terms_normalizes_and_rejects_bad_maps() -> None:
    normalized, error = _validate_expansion_terms({"sl": ["Suitelet", "suitelet"], "Chatbox": ["chatbot"]})
    assert error is None
    assert normalized == {"sl": ["suitelet"], "chatbox": ["chatbot"]}

    assert _validate_expansion_terms(None) == (None, None)

    for bad in (
        "sl",
        ["suitelet"],
        {"sl": "suitelet"},
        {"sl": []},
        {"sl": [1]},
        {"": ["suitelet"]},
        {"sl; drop": ["suitelet"]},
        {"sl": ['suitelet"; drop']},
    ):
        normalized, error = _validate_expansion_terms(bad)
        assert normalized is None
        assert error is not None


def test_wiki_query_rejects_invalid_expansion_terms() -> None:
    payload = wiki_query(question="sl 页面脚本", expansion_terms={"sl": 1})
    assert payload["ok"] is False
    assert payload["code"] == "invalid_expansion_terms"


def test_worker_profile_does_not_reintroduce_retired_generation_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    config_dir = tmp_path / "config"
    config_path = config_dir / "config.yaml"
    write_global_config(config_path, vault_name="primary", vault_root=root, tool_profile="worker")
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(config_dir))

    module_name = "_worker_profile_test_server"
    spec = importlib.util.spec_from_file_location(module_name, server_module.__file__)
    assert spec is not None and spec.loader is not None
    worker_server = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = worker_server
    try:
        spec.loader.exec_module(worker_server)
        assert anyio.run(_registered_tool_names, worker_server.mcp) == CORE_TOOLS
    finally:
        sys.modules.pop(module_name, None)


def test_mcp_client_protocol_calls_status_and_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    monkeypatch.setattr(
        "app.server.wiki_status_tool",
        lambda _: {
            "ok": True,
            "version": RUNTIME_PROVENANCE.package_version,
            "runtime": RUNTIME_PROVENANCE.to_public_dict(),
        },
    )

    monkeypatch.setattr(
        "app.server.run_query_v2",
        lambda root, question, **_: {"ok": True, "question": question, "results": []},
    )

    async def assert_protocol_calls() -> None:
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
            status_tool = next(tool for tool in tools if tool.name == "wiki_status")
            assert isinstance(status_tool.input_schema, dict)

            status_result = await client.call_tool("wiki_status", {"vault": "primary"})
            query_result = await client.call_tool("wiki_query", {"vault": "primary", "question": "hello"})

            assert status_result.is_error is False
            assert query_result.is_error is False
            assert _tool_result_payload(status_result)["vault"] == "primary"
            assert _tool_result_payload(query_result) == {
                "ok": True,
                "code": "no_results",
                "message": "No indexed documentation matched the query.",
                "question": "hello",
                "results": [],
            }

    anyio.run(assert_protocol_calls)


def test_wiki_status_does_not_materialize_query_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    monkeypatch.setattr(server_module, "CONFIG_REGISTRY", registry)
    monkeypatch.setattr(server_module, "_QUERY_REGISTRIES", {})
    monkeypatch.setattr(
        server_module,
        "wiki_status_tool",
        lambda _: {
            "ok": True,
            "vault": "primary",
            "initialized": True,
            "missing_required_paths": [],
            "version": RUNTIME_PROVENANCE.package_version,
            "runtime": RUNTIME_PROVENANCE.to_public_dict(),
        },
    )

    result = server_module.wiki_status(vault="primary")

    assert result["query_execution"] == {"active": 0, "pending": 0}
    assert server_module._QUERY_REGISTRIES == {}


def test_no_results_adapter_does_not_override_discovery_outcome() -> None:
    payload = {
        "ok": True,
        "question": "列出 N/*",
        "results": [],
        "pipeline": {
            "discovery": {"candidate_entities": [{"canonical_id": "n/auth"}]},
            "batch": {"status": "success"},
        },
    }

    assert attach_no_results_outcome(payload) == payload


def test_mcp_query_schema_accepts_raw_scope_and_forwards_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    calls: dict[str, object] = {}

    def fake_run_query(root: Path, question: str, **kwargs: object) -> dict[str, object]:
        calls.update(kwargs)
        return {
            "ok": True,
            "question": question,
            "scope": kwargs["scope"],
            "results": [{"path": "raw/sources/references/raw.md", "source_kind": "raw"}],
        }

    monkeypatch.setattr("app.server.run_query_v2", fake_run_query)

    async def assert_protocol_contract() -> None:
        async with Client(mcp) as client:
            tool = next(item for item in (await client.list_tools()).tools if item.name == "wiki_query")
            scope_schema = tool.input_schema["properties"]["scope"]
            assert "raw" in scope_schema["enum"]
            query_result = await client.call_tool("wiki_query", {"vault": "primary", "question": "raw", "scope": "raw"})
            assert query_result.is_error is False
            payload = _tool_result_payload(query_result)
            assert payload["scope"] == "raw"
            reference = ContentRefV1.decode(str(payload["results"][0]["content_ref"]))
            assert reference.scope == "raw"
            assert reference.identity == "raw/sources/references/raw.md"

    anyio.run(assert_protocol_contract)
    assert calls["scope"] == "raw"


def test_query_public_projection_adds_refs_to_results_and_batch_hits() -> None:
    payload = server_module._attach_query_content_refs(
        {
            "ok": True,
            "results": [{"path": "wiki/concepts/page.md", "source_kind": "knowledge"}],
            "additional_results": [{"path": "raw/sources/references/raw.md", "source_kind": "raw"}],
            "pipeline": {
                "corpus": "active",
                "batch": {
                    "entities": [
                        {
                            "primary": {"path": "wiki/entities/primary.md", "source_kind": "knowledge"},
                            "alternatives": [{"path": "archives/bundles/a/b/archive/wiki/old.md", "source_kind": "archive"}],
                        }
                    ]
                },
                "discovery": {"candidate_entities": [{"canonical_id": "n/primary"}]},
            },
        },
        logical_vault="primary",
    )

    assert ContentRefV1.decode(str(payload["results"][0]["content_ref"])).scope == "active"
    assert ContentRefV1.decode(str(payload["additional_results"][0]["content_ref"])).scope == "raw"
    batch_entity = payload["pipeline"]["batch"]["entities"][0]
    assert ContentRefV1.decode(str(batch_entity["primary"]["content_ref"])).scope == "active"
    assert ContentRefV1.decode(str(batch_entity["alternatives"][0]["content_ref"])).scope == "archive"
    assert "content_ref" not in payload["pipeline"]["discovery"]["candidate_entities"][0]


def test_wiki_query_rejects_invalid_scope() -> None:
    payload = wiki_query(question="raw", scope="unsupported")  # type: ignore[arg-type]

    assert payload["ok"] is False
    assert payload["code"] == "invalid_scope"
    assert payload["message"] == "the query scope is invalid"
    assert payload["error"] == payload["message"]
    assert payload["retryable"] is False
    assert isinstance(payload["correlation_id"], str)


def test_public_query_schema_has_logical_vault_and_no_runtime_overrides() -> None:
    parameters = inspect.signature(wiki_query).parameters
    assert {"vault", "vault_root", "vaultRoot", "scope", "project", "filters", "top_k"} <= set(parameters)
    assert {"enable_vector", "context_window_tokens", "include_raw_sources", "max_graph_hops"}.isdisjoint(parameters)


def test_resolver_uses_default_logical_vault(tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    result = resolve_tool_vault(registry=registry)
    assert result.root == root
    assert result.logical_name == "primary"
    assert result.warnings == ()


def test_resolver_rejects_unknown_and_ambiguous_selectors(tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    with pytest.raises(RuntimeConfigError, match="unknown vault") as unknown:
        resolve_tool_vault(vault="absent", registry=registry)
    assert unknown.value.code == "unknown_vault"
    with pytest.raises(RuntimeConfigError) as ambiguous:
        resolve_tool_vault(vault="primary", vault_root=str(root), registry=registry)
    assert ambiguous.value.code == "ambiguous_vault_selector"


def test_resolver_accepts_snake_and_camel_legacy_root_with_warning(tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    snake = resolve_tool_vault(vault_root=str(root), registry=registry)
    camel = resolve_tool_vault(vaultRoot=str(root), registry=registry)
    assert snake.root == camel.root == root
    assert snake.warnings == camel.warnings == ("deprecated_vault_root",)


def test_attach_warnings_preserves_existing_warnings() -> None:
    assert attach_warnings({"ok": True, "warnings": ["existing"]}, ("deprecated_vault_root",))["warnings"] == ["existing", "deprecated_vault_root"]


@pytest.mark.parametrize("tool", [wiki_archive, wiki_restore])
def test_archive_tools_use_shared_vault_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: object) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    kwargs = {"vault_root": str(root)}
    if tool is wiki_archive:
        result = tool(target="wiki/concepts/example.md", **kwargs)  # type: ignore[operator]
    else:
        result = tool(archive_id="bundle-1", **kwargs)  # type: ignore[operator]
    assert result["ok"] is False  # type: ignore[index]
    assert result["code"] in {"archive_target_missing", "archive_not_found"}  # type: ignore[index]
    assert result["warnings"] == ["deprecated_vault_root"]  # type: ignore[index]


def test_archive_rejects_invalid_reason_before_resolving_vault() -> None:
    result = wiki_archive(target="wiki/concepts/example.md", reason="mcp_crud_validation_cleanup")

    assert result["ok"] is False
    assert result["code"] == "invalid_archive_reason"


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected_code"),
    [
        (wiki_archive, {"target": "wiki/concepts/example.md"}, "archive_apply_failed"),
        (wiki_restore, {"archive_id": "bundle-1"}, "restore_apply_failed"),
    ],
)
def test_archive_tools_return_structured_error_when_service_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool: object,
    kwargs: dict[str, str],
    expected_code: str,
) -> None:
    registry, _ = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)

    def fail_initialization(*args: object, **kwargs: object) -> object:
        raise OSError("state directory is not writable")

    monkeypatch.setattr("app.server.ArchiveService", fail_initialization)

    result = tool(**kwargs)  # type: ignore[operator]

    assert result["ok"] is False
    assert result["code"] == expected_code
    assert result["message"] == "the operation could not be completed"
    assert result["error"] == result["message"]
    assert "state directory" not in str(result)


def test_query_rejects_runtime_override_filters_before_domain_call() -> None:
    result = wiki_query(
        question="hello",
        filters={"index_path": "bad"},
        vault_root=str(Path.cwd()),
    )
    assert result["ok"] is False
    assert result["code"] == "invalid_filters"
    assert result["message"] == "the query filters are invalid"


def test_query_passes_path_prefix_filter_to_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """path_prefix should pass the server whitelist and reach run_query_v2 as a QueryFilters."""
    from retrieval.query_shared import QueryFilters

    registry, vault_root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)

    captured: dict[str, object] = {}

    def fake_run_query(root: Path, question: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "question": question, "results": []}

    monkeypatch.setattr("app.server.run_query_v2", fake_run_query)

    result = wiki_query(
        question="UserEventType enum values",
        scope="raw",
        filters={"path_prefix": "raw/sources/file/NetSuite Help Docs/"},
        vault_root=str(vault_root),
    )

    assert result["ok"] is True
    passed_filters = captured["filters"]
    assert isinstance(passed_filters, QueryFilters)
    assert passed_filters.path_prefix == "raw/sources/file/NetSuite Help Docs/"


def test_query_normalizes_camel_case_path_prefix_and_rejects_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Nested camelCase filters must be normalized at the MCP boundary."""
    from retrieval.query_shared import QueryFilters

    registry, vault_root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    captured: dict[str, object] = {}

    def fake_run_query(root: Path, question: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "question": question, "results": []}

    monkeypatch.setattr("app.server.run_query_v2", fake_run_query)

    result = wiki_query(
        question="serverWidget",
        scope="raw",
        filters={"pathPrefix": "raw/sources/file/NetSuite Help Docs/"},
        vault_root=str(vault_root),
    )

    assert result["ok"] is True
    passed_filters = captured["filters"]
    assert isinstance(passed_filters, QueryFilters)
    assert passed_filters.path_prefix == "raw/sources/file/NetSuite Help Docs/"

    conflict = wiki_query(
        question="serverWidget",
        scope="raw",
        filters={
            "path_prefix": "raw/sources/file/one/",
            "pathPrefix": "raw/sources/file/two/",
        },
        vault_root=str(vault_root),
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "invalid_filters"


def test_list_normalizes_camel_case_path_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, vault_root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    captured: dict[str, object] = {}

    def fake_list(self: object, *, scope: object, filters: object, page_size: object, cursor: object) -> dict[str, object]:
        captured["filters"] = filters
        return {"ok": True, "scope": scope, "items": [], "page_size": page_size}

    monkeypatch.setattr("app.server.ContentCatalogService.list_items", fake_list)

    result = wiki_list(
        filters={"pathPrefix": "raw/sources/file/NetSuite Help Docs/"},
        vault_root=str(vault_root),
    )

    assert result["ok"] is True
    assert captured["filters"] == {"path_prefix": "raw/sources/file/NetSuite Help Docs/"}


def test_list_filter_alias_conflict_returns_public_error_envelope() -> None:
    result = wiki_list(
        filters={"path_prefix": "raw/one/", "pathPrefix": "raw/two/"},
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_filters"
    assert result["message"] == "the query filters are invalid"
    assert result["error"] == result["message"]
    assert result["retryable"] is False
    assert isinstance(result["correlation_id"], str)


def test_query_rejects_top_k_above_limit() -> None:
    result = wiki_query(question="hello", top_k=41)
    assert result["ok"] is False
    assert result["code"] == "invalid_top_k"
    assert result["message"] == "the query limit is invalid"


def test_query_enforces_wall_clock_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, vault_root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    monkeypatch.setattr("app.server.QUERY_TIMEOUT_SECONDS", 0.1)

    def slow_query(*args: object, **kwargs: object) -> dict[str, object]:
        time.sleep(0.5)
        return {"ok": True, "results": []}

    monkeypatch.setattr("app.server.run_query_v2", slow_query)

    result = wiki_query(question="hello", vault_root=str(vault_root))

    assert result["ok"] is False
    assert result["code"] == "query_timeout"
    with sqlite3.connect(vault_root / ".llm-wiki" / "state.sqlite3") as conn:
        rows = conn.execute("SELECT outcome, worker_state FROM query_telemetry").fetchall()
    assert rows == [("timeout", "running")]


def test_write_note_requires_note_type() -> None:
    assert wiki_write_note(title="Title", content="Body")["code"] == "missing_note_type"


def test_write_and_update_schemas_expose_related_page_arguments() -> None:
    async def assert_schema() -> None:
        async with Client(mcp) as client:
            tools = {item.name: item for item in (await client.list_tools()).tools}
            write_properties = tools["wiki_write_note"].input_schema["properties"]
            update_properties = tools["wiki_update"].input_schema["properties"]
            assert set(write_properties) == {
                "chat_derived",
                "chat_metadata",
                "chat_sources",
                "content",
                "domain",
                "filename",
                "noteType",
                "note_type",
                "project",
                "related_pages",
                "related_pages_heading",
                "sources",
                "tags",
                "title",
                "vault",
                "vaultRoot",
                "vault_root",
            }
            assert tools["wiki_write_note"].input_schema["required"] == ["title", "content"]
            assert {"related_pages", "sources"} <= set(write_properties)
            assert "related_pages" in update_properties

    anyio.run(assert_schema)


def test_registry_exposes_strict_ingest_schema_and_tool_contract_annotations() -> None:
    async def assert_schema() -> None:
        async with Client(mcp) as client:
            tools = {item.name: item for item in (await client.list_tools()).tools}
            ingest = tools["wiki_ingest"]
            assert "metadata" not in ingest.input_schema.get("properties", {})
            assert isinstance(ingest.output_schema, dict)
            status_annotations = tools["wiki_status"].annotations
            query_annotations = tools["wiki_query"].annotations
            list_tool = tools["wiki_list"]
            get_tool = tools["wiki_get"]
            get_properties = get_tool.input_schema.get("properties", {})
            assert {"content_ref", "contentRef", "max_total_bytes", "maxTotalBytes"} <= set(get_properties)
            assert {"store_scope", "storeScope", "page_size", "pageSize"} <= set(list_tool.input_schema.get("properties", {}))
            assert not get_tool.input_schema.get("required", [])
            assert "all" not in str(list_tool.input_schema)
            assert "body" not in list_tool.input_schema.get("properties", {})
            assert getattr(list_tool.annotations, "read_only_hint", None) is True
            assert getattr(list_tool.annotations, "idempotent_hint", None) is True
            assert getattr(list_tool.annotations, "open_world_hint", None) is False
            assert getattr(get_tool.annotations, "read_only_hint", None) is True
            assert getattr(get_tool.annotations, "idempotent_hint", None) is True
            assert getattr(get_tool.annotations, "open_world_hint", None) is False
            assert getattr(status_annotations, "read_only_hint", None) is True
            assert getattr(status_annotations, "idempotent_hint", None) is True
            assert getattr(status_annotations, "open_world_hint", None) is False
            assert getattr(query_annotations, "read_only_hint", None) is True
            assert getattr(query_annotations, "idempotent_hint", None) is True

    anyio.run(assert_schema)


def test_catalog_tools_accept_camel_case_argument_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """camelCase clients (e.g. VS Code MCP) must be normalized to the snake_case domain contract."""
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    captured: dict[str, object] = {}

    def fake_list(self: object, *, scope: object, filters: object, page_size: object, cursor: object) -> dict[str, object]:
        captured.update(scope=scope, page_size=page_size)
        return {"ok": True, "scope": scope, "items": [], "page_size": page_size}

    def fake_get(self: object, content_ref: str, *, include_body: object, max_bytes: object, cursor: object) -> dict[str, object]:
        captured.update(content_ref=content_ref, include_body=include_body, max_bytes=max_bytes)
        return {"ok": True, "content_ref": content_ref}

    monkeypatch.setattr("app.server.ContentCatalogService.list_items", fake_list)
    monkeypatch.setattr("app.server.ContentCatalogService.get_item", fake_get)

    list_result = wiki_list(storeScope="raw", pageSize=7, vault_root=str(root))
    assert list_result["ok"] is True
    assert captured["scope"] == "raw"
    assert captured["page_size"] == 7

    captured.clear()
    get_result = wiki_get(contentRef="cr1_abc", includeBody=True, maxBytes=2048, vault_root=str(root))
    assert get_result["ok"] is True
    assert captured["content_ref"] == "cr1_abc"
    assert captured["include_body"] is True
    assert captured["max_bytes"] == 2048


def test_wiki_get_clamps_oversized_body_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    captured: dict[str, object] = {}

    def fake_get(self: object, content_ref: str, *, include_body: object, max_bytes: object, cursor: object) -> dict[str, object]:
        captured.update(content_ref=content_ref, include_body=include_body, max_bytes=max_bytes)
        return {"ok": True, "content_ref": content_ref}

    monkeypatch.setattr("app.server.ContentCatalogService.get_item", fake_get)

    result = wiki_get(contentRef="cr1_abc", includeBody=True, maxBytes=50000, vault_root=str(root))

    assert result["ok"] is True
    assert captured["max_bytes"] == MAX_BODY_BUDGET
    assert "body_budget_clamped" in result["warnings"]


def test_wiki_get_forwards_and_clamps_total_body_budget_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    captured: dict[str, object] = {}

    def fake_get(
        self: object,
        content_ref: str,
        *,
        include_body: object,
        max_bytes: object,
        cursor: object,
        max_total_bytes: object,
    ) -> dict[str, object]:
        captured.update(
            content_ref=content_ref,
            include_body=include_body,
            max_bytes=max_bytes,
            max_total_bytes=max_total_bytes,
        )
        return {"ok": True, "content_ref": content_ref}

    monkeypatch.setattr("app.server.ContentCatalogService.get_item", fake_get)

    result = wiki_get(
        contentRef="cr1_abc",
        maxTotalBytes=MAX_TOTAL_BODY_BUDGET + 1,
        vault_root=str(root),
    )

    assert result["ok"] is True
    assert captured["max_total_bytes"] == MAX_TOTAL_BODY_BUDGET
    assert "total_body_budget_clamped" in result["warnings"]


def test_wiki_get_infers_configured_vault_from_content_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    config_path = tmp_path / "config" / "config.yaml"
    write_global_config(config_path, vault_name="primary", vault_root=primary, make_default=True)
    write_global_config(config_path, vault_name="secondary", vault_root=secondary, make_default=False)
    registry = ConfigRegistry.from_file(config_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)

    reference = ContentRefV1("secondary", "active", "page", "wiki/concepts/secondary.md").encode()
    captured: dict[str, object] = {}

    def fake_get(self: object, content_ref: str, *, include_body: object, max_bytes: object, cursor: object) -> dict[str, object]:
        captured["logical_vault"] = getattr(self, "logical_vault")
        return {"ok": True, "content_ref": content_ref, "identity": "wiki/concepts/secondary.md"}

    monkeypatch.setattr("app.server.ContentCatalogService.get_item", fake_get)

    result = wiki_get(content_ref=reference)

    assert result["ok"] is True
    assert captured["logical_vault"] == "secondary"
    assert result["vault"] == "secondary"


def test_wiki_get_keeps_environment_default_vault_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "codingwork"
    root.mkdir()
    registry = ConfigRegistry.from_file(tmp_path / "config.yaml")
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    monkeypatch.setenv(VAULT_ROOT_ENV, str(root))

    reference = ContentRefV1("codingwork", "active", "page", "wiki/concepts/serverwidget.md").encode()
    captured: dict[str, object] = {}

    def fake_get(self: object, content_ref: str, *, include_body: object, max_bytes: object, cursor: object) -> dict[str, object]:
        captured["logical_vault"] = getattr(self, "logical_vault")
        return {"ok": True, "content_ref": content_ref, "identity": "wiki/concepts/serverwidget.md"}

    monkeypatch.setattr("app.server.ContentCatalogService.get_item", fake_get)

    result = wiki_get(content_ref=reference)

    assert result["ok"] is True
    assert captured["logical_vault"] == "codingwork"


def test_wiki_get_requires_content_ref_or_camel_alias() -> None:
    result = wiki_get()
    assert result["ok"] is False
    assert result["code"] == "invalid_content_ref"


def test_legacy_ingest_metadata_is_rejected_without_vault_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    before = {path.relative_to(root).as_posix(): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}

    async def assert_rejected() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "wiki_ingest",
                {
                    "vault": "primary",
                    "source_path": str(tmp_path / "source.txt"),
                    "source_name": "source",
                    "metadata": {"secret": "must not be echoed"},
                },
            )
            assert result.is_error is True

    anyio.run(assert_rejected)
    after = {path.relative_to(root).as_posix(): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_write_note_forwards_related_pages_and_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    calls: dict[str, object] = {}

    def fake_writer(**kwargs: object) -> dict[str, object]:
        calls.update(kwargs)
        return {"ok": True, "redacted_count": 2}

    monkeypatch.setattr("app.server.run_write_note", fake_writer)
    related_pages = [{"path": "wiki/concepts/related.md", "title": "Related"}]
    sources = ["raw/sources/reference.txt"]

    result = wiki_write_note(
        title="Title",
        content="Body",
        note_type="knowledge",
        domain="common-errors",
        related_pages=related_pages,
        sources=sources,
        vault="primary",
    )

    assert result == {"ok": True, "redacted_count": 2}
    assert calls["related_pages"] == related_pages
    assert calls["sources"] == sources
    assert calls["vault_root"] == str(root)


def test_write_note_canonical_alias_wins_and_legacy_alias_is_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    calls: dict[str, object] = {}

    def fake_writer(**kwargs: object) -> dict[str, object]:
        calls.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("app.server.run_write_note", fake_writer)

    result = wiki_write_note(
        title="Title",
        content="Body",
        note_type="knowledge",
        noteType="legacy",
        vault="primary",
    )

    assert result == {"ok": True}
    assert calls["note_type"] == "knowledge"
    assert calls["vault_root"] == str(root)


def test_update_forwards_related_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("app.server.CONFIG_REGISTRY", registry)
    calls: dict[str, object] = {}

    def fake_preview(*args: object, **kwargs: object) -> dict[str, object]:
        calls["args"] = args
        calls.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("app.server.run_preview_update", fake_preview)
    related_pages = [{"path": "wiki/concepts/related.md", "title": "Related"}]

    result = wiki_update(
        page_path="wiki/concepts/page.md",
        incoming_body="Body",
        related_pages=related_pages,
        vault="primary",
    )

    assert result == {"ok": True}
    assert calls["related_pages"] == related_pages
    assert calls["args"] == (root, "wiki/concepts/page.md", "Body", None)

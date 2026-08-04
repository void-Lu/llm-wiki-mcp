from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import Client

from netsuite_llm_wiki_mcp.runtime_config import ConfigRegistry, RuntimeConfigError, write_global_config
from netsuite_llm_wiki_mcp.runtime_provenance import RUNTIME_PROVENANCE
from netsuite_llm_wiki_mcp import server as server_module
from netsuite_llm_wiki_mcp.server import (
    attach_warnings,
    mcp,
    resolve_tool_vault,
    _validate_expansion_terms,
    wiki_archive,
    wiki_restore,
    wiki_query,
    wiki_status,
    wiki_write_note,
)


CORE_TOOLS = {
    "wiki_status",
    "wiki_ingest",
    "wiki_codegraph_import",
    "wiki_write_note",
    "wiki_update",
    "wiki_query",
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
            assert client.server_info.name == "netsuite-llm-wiki-mcp"
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
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(config_dir))

    module_name = "netsuite_llm_wiki_mcp._worker_profile_test_server"
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
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.CONFIG_REGISTRY", registry)
    monkeypatch.setattr(
        "netsuite_llm_wiki_mcp.server.wiki_status_tool",
        lambda _: {
            "ok": True,
            "version": RUNTIME_PROVENANCE.package_version,
            "runtime": RUNTIME_PROVENANCE.to_public_dict(),
        },
    )

    class StubArchiveService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def status(self) -> dict[str, object]:
            return {"archive_index": {"state": "ready"}, "operations": {"pending": 0}}

    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.ArchiveService", StubArchiveService)
    monkeypatch.setattr(
        "netsuite_llm_wiki_mcp.server.run_query_v2",
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


def test_public_query_schema_has_logical_vault_and_no_runtime_overrides() -> None:
    parameters = inspect.signature(wiki_query).parameters
    assert {"vault", "vault_root", "vaultRoot", "scope", "project", "filters", "top_k"} <= set(parameters)
    assert {"enable_vector", "context_window_tokens", "include_raw_sources", "max_graph_hops"}.isdisjoint(parameters)


def test_codegraph_import_schema_uses_snake_case_workspace_root() -> None:
    async def assert_schema() -> None:
        async with Client(mcp) as client:
            tool = next(item for item in (await client.list_tools()).tools if item.name == "wiki_codegraph_import")
            properties = tool.input_schema.get("properties", {})
            assert "workspace_root" in properties
            assert "workspaceRoot" not in properties

    anyio.run(assert_schema)


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


def test_status_hides_absolute_vault_and_model_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.CONFIG_REGISTRY", registry)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.wiki_status_tool", lambda _: {"ok": True, "vault_root": str(root), "codegraph": {"available": True, "executable": str(root / "bin" / "codegraph")}, "vector": {"state": "missing"}})
    result = wiki_status()
    assert result["vault"] == "primary"
    assert "vault_root" not in result
    assert "model_path" not in result["config"]["retrieval"]["embedding"]
    assert "executable" not in result["codegraph"]


@pytest.mark.parametrize("tool", [wiki_archive, wiki_restore])
def test_archive_tools_use_shared_vault_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: object) -> None:
    registry, root = _registry(tmp_path)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.CONFIG_REGISTRY", registry)
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
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.CONFIG_REGISTRY", registry)

    def fail_initialization(*args: object, **kwargs: object) -> object:
        raise OSError("state directory is not writable")

    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.ArchiveService", fail_initialization)

    result = tool(**kwargs)  # type: ignore[operator]

    assert result == {"ok": False, "code": expected_code, "error": "state directory is not writable"}  # type: ignore[comparison-overlap]


def test_query_rejects_runtime_override_filters_before_domain_call() -> None:
    result = wiki_query(question="hello", filters={"index_path": "bad"})
    assert result == {"ok": False, "code": "invalid_filters", "error": "filters may only contain type and tags"}


def test_query_rejects_top_k_above_limit() -> None:
    result = wiki_query(question="hello", top_k=41)
    assert result == {"ok": False, "code": "invalid_top_k", "error": "top_k must be between 1 and 40"}


def test_query_enforces_wall_clock_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, vault_root = _registry(tmp_path)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.CONFIG_REGISTRY", registry)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.QUERY_TIMEOUT_SECONDS", 0.1)

    def slow_query(*args: object, **kwargs: object) -> dict[str, object]:
        time.sleep(0.5)
        return {"ok": True, "results": []}

    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_query_v2", slow_query)

    result = wiki_query(question="hello", vault_root=str(vault_root))

    assert result["ok"] is False
    assert result["code"] == "query_timeout"


def test_write_note_requires_note_type() -> None:
    assert wiki_write_note(title="Title", content="Body")["code"] == "missing_note_type"

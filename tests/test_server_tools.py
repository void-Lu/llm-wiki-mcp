from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.runtime_config import ConfigRegistry, RuntimeConfigError, write_global_config
from netsuite_llm_wiki_mcp.runtime_provenance import RUNTIME_PROVENANCE
from netsuite_llm_wiki_mcp import server as server_module
from netsuite_llm_wiki_mcp.server import (
    attach_warnings,
    mcp,
    resolve_tool_vault,
    wiki_archive,
    wiki_restore,
    wiki_query,
    wiki_status,
    wiki_write_note,
)


CORE_TOOLS = {
    "wiki_status",
    "wiki_ingest",
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


def test_mcp_initialization_version_matches_runtime_provenance() -> None:
    options = mcp._mcp_server.create_initialization_options()
    assert options.server_name == "netsuite-llm-wiki-mcp"
    assert options.server_version == RUNTIME_PROVENANCE.server_version


def test_registered_tools_match_core_public_surface() -> None:
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == CORE_TOOLS


def test_worker_profile_adds_only_generation_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
        assert {tool.name for tool in worker_server.mcp._tool_manager.list_tools()} == CORE_TOOLS | {"wiki_generation"}
    finally:
        sys.modules.pop(module_name, None)


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


def test_write_note_requires_note_type() -> None:
    assert wiki_write_note(title="Title", content="Body")["code"] == "missing_note_type"

from pathlib import Path

import pytest

from netsuite_rag_mcp.server import (
    _build_filters,
    generate_suitecloud_wiki_tool,
    get_index_status_tool,
    index_sources_tool,
    index_vault_tool,
)
from netsuite_rag_mcp.vector_store import FakeEmbedder


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_v2_config(vault: Path) -> None:
    """Write a v2 multi-source sources.yaml."""
    (vault / "rag").mkdir(parents=True, exist_ok=True)
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  chroma_path: .rag-index/chroma",
                "  embedding_model: fake",
                "  embedding_cache_path: .models",
                "  collections:",
                "    default: netsuite_notes",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian, .rag-index]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_notes",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian, .rag-index, node_modules]",
                "    file_types: [js]",
                "    parser: suitescript_code_and_config",
                "    collection: netsuite_notes",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


def _write_note(vault: Path, relative_path: str, content: str) -> Path:
    path = vault / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


NOTE_CONTENT = """---
type: script
project: project-a
script_type: restlet
script_id: customscript_order_sync_restlet
related_objects: [salesorder]
related_scripts: []
status: active
---

# RESTlet - 订单同步接口

## 用途
同步订单到外部系统。
"""

JS_CONTENT = """/**
 * @NScriptType Restlet
 * @NApiVersion 2.1
 */
define(['N/record'], function(record) {
    function doGet(context) {
        return record.load({type: context.type, id: context.id});
    }
    return {get: doGet};
});
"""


# ── Existing tests ───────────────────────────────────────────────────────────

def test_index_status_before_index(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_v2_config(vault)

    status = get_index_status_tool(str(vault))

    assert status["indexed"] is False
    assert status["collection_count"] == 0
    assert status["manifest_exists"] is False


def test_server_status_does_not_use_cwd_as_vault(monkeypatch, tmp_path: Path):
    cwd_vault = tmp_path / "cwd-vault"
    cwd_vault.mkdir()
    _write_v2_config(cwd_vault)
    monkeypatch.chdir(cwd_vault)
    monkeypatch.delenv("NETSUITE_RAG_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_RAG_CONFIG_DIR", str(tmp_path / "empty-config"))
    monkeypatch.setenv("NETSUITE_RAG_USER_DATA_DIR", str(tmp_path / "user-data"))

    status = get_index_status_tool()

    assert status["ok"] is False
    assert status["code"] == "missing_vault_root"
    assert "netsuite-rag-mcp init --vault" in status["error"]


def test_server_status_reports_runtime_paths_from_global_config(monkeypatch, tmp_path: Path):
    from netsuite_rag_mcp.runtime_config import write_global_config

    vault = tmp_path / "vault"
    vault.mkdir()
    _write_v2_config(vault)
    config_dir = tmp_path / "config"
    monkeypatch.delenv("NETSUITE_RAG_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_RAG_CONFIG_DIR", str(config_dir))
    write_global_config(config_dir / "config.yaml", vault_name="homework", vault_root=vault, make_default=True)

    status = get_index_status_tool()

    assert status["ok"] is True
    assert status["vault_root"] == str(vault.resolve())
    assert status["resolution_source"] == "global_config"
    assert status["config_path"] == str((config_dir / "config.yaml").resolve())
    # vault-local: data lives inside vault
    assert status["vault_data_root"] == str((vault / ".rag-index").resolve())
    assert status["manifest_path"].endswith("index-manifest.json")
    assert status["chroma_path"].endswith("chroma")
    assert status["embedding_cache_path"] == str((vault / ".models").resolve())
    assert status["sources_config_exists"] is True


def test_server_status_reports_runtime_paths_from_env(monkeypatch, tmp_path: Path):
    vault = tmp_path / "env-vault"
    vault.mkdir()
    _write_v2_config(vault)
    config_dir = tmp_path / "empty-config"
    data_root = tmp_path / "user-data"
    monkeypatch.setenv("NETSUITE_RAG_VAULT_ROOT", str(vault))
    monkeypatch.setenv("NETSUITE_RAG_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("NETSUITE_RAG_USER_DATA_DIR", str(data_root))

    status = get_index_status_tool()

    assert status["ok"] is True
    assert status["vault_root"] == str(vault.resolve())
    assert status["resolution_source"] == "env"
    assert status["global_config_path"] == str((config_dir / "config.yaml").resolve())
    assert status["data_root"] == str(data_root.resolve())
    assert status["sources_config_exists"] is True


def test_index_vault_tool_uses_core_indexer(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "vault_root: .",
                "include:",
                "  - projects",
                "exclude:",
                "  - .rag-index",
                "chroma_path: .rag-index/chroma",
                "collection_name: netsuite_notes",
                "embedding_model: fake",
            ]
        ),
        encoding="utf-8",
    )
    note_dir = vault / "projects" / "project-a" / "scripts" / "restlet"
    note_dir.mkdir(parents=True)
    (note_dir / "order-sync.md").write_text(
        """---
type: script
project: project-a
script_type: restlet
script_id: customscript_order_sync_restlet
related_objects: [salesorder]
related_scripts: []
status: active
---

# RESTlet - 订单同步接口

## 用途
同步订单到外部系统。
""",
        encoding="utf-8",
    )

    report = index_vault_tool(str(vault), mode="full", embedder=FakeEmbedder())

    assert report["indexed_files"] == 1


# ── T13: New tests ───────────────────────────────────────────────────────────


class TestBuildFiltersWithSourceParams:
    """Test that _build_filters includes source_kind and source_name."""

    def test_source_kind_included(self):
        filters = _build_filters(
            project=None, script_type=None, related_objects=None,
            related_scripts=None, object_type=None, status=None,
            source_kind="note",
        )
        assert filters["source_kind"] == "note"

    def test_source_name_included(self):
        filters = _build_filters(
            project=None, script_type=None, related_objects=None,
            related_scripts=None, object_type=None, status=None,
            source_kind=None, source_name="obsidian",
        )
        assert filters["source_name"] == "obsidian"

    def test_both_source_params_with_other_filters(self):
        filters = _build_filters(
            project="project-a", script_type="restlet",
            related_objects=None, related_scripts=None,
            object_type=None, status=None,
            source_kind="code", source_name="netsuite_repo",
        )
        assert filters["project"] == "project-a"
        assert filters["script_type"] == "restlet"
        assert filters["source_kind"] == "code"
        assert filters["source_name"] == "netsuite_repo"

    def test_source_params_omitted_when_none(self):
        filters = _build_filters(
            project="project-a", script_type=None, related_objects=None,
            related_scripts=None, object_type=None, status=None,
            source_kind=None, source_name=None,
        )
        assert "source_kind" not in filters
        assert "source_name" not in filters
        assert filters["project"] == "project-a"

    def test_content_type_filter_is_mapped_to_type(self):
        filters = _build_filters(
            project=None,
            script_type=None,
            related_objects=None,
            related_scripts=None,
            object_type=None,
            status=None,
            source_kind=None,
            source_name=None,
            content_type="generated_wiki",
        )

        assert filters["type"] == "generated_wiki"


class TestIndexSourcesTool:
    """Test the index_sources_tool function."""

    def test_index_by_source_name(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_note(vault, "projects/other-note.md", NOTE_CONTENT)

        result = index_sources_tool(
            str(vault), source_names=["obsidian"], mode="full", embedder=FakeEmbedder()
        )

        assert "sources" in result
        assert "obsidian" in result["sources"]
        assert result["total_indexed"] >= 1

    def test_index_by_source_kind(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        result = index_sources_tool(
            str(vault), source_kind="note", mode="full", embedder=FakeEmbedder()
        )

        assert "sources" in result
        assert "obsidian" in result["sources"]

    def test_index_by_source_name_and_kind(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_note(vault, "projects/test-script.js", JS_CONTENT)  # wait, js not md

        # Write a JS file too
        js_path = vault / "projects" / "test-script.js"
        js_path.parent.mkdir(parents=True, exist_ok=True)
        js_path.write_text(JS_CONTENT, encoding="utf-8")

        # Index only code sources
        result = index_sources_tool(
            str(vault), source_kind="code", mode="full", embedder=FakeEmbedder()
        )

        assert "sources" in result
        # Only netsuite_repo (code source) should appear
        assert "netsuite_repo" in result["sources"]
        # obsidian (note source) should NOT appear
        assert "obsidian" not in result["sources"]

    def test_invalid_mode_returns_error(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)

        result = index_sources_tool(str(vault), mode="bad_mode", embedder=FakeEmbedder())

        assert result["ok"] is False
        assert result["code"] == "invalid_mode"
        assert result["mode"] == "bad_mode"
        assert "full" in result["error"]
        assert "incremental" in result["error"]
    def test_unknown_exception_is_not_swallowed(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)

        def raise_unexpected_error(*_args, **_kwargs):
            raise RuntimeError("unexpected index failure")

        monkeypatch.setattr("netsuite_rag_mcp.server.run_index_sources", raise_unexpected_error)

        with pytest.raises(RuntimeError, match="unexpected index failure"):
            index_sources_tool(str(vault), source_names=["obsidian"], mode="full", embedder=FakeEmbedder())

    def test_incremental_mode(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        # First, full index
        result = index_sources_tool(
            str(vault), source_names=["obsidian"], mode="full", embedder=FakeEmbedder()
        )
        assert result["total_indexed"] >= 1

        # Then, incremental (should skip already indexed)
        result2 = index_sources_tool(
            str(vault), source_names=["obsidian"], mode="incremental", embedder=FakeEmbedder()
        )
        assert "total_skipped" in result2


class TestGenerateSuitecloudWikiTool:
    def test_generate_suitecloud_wiki_tool_delegates_to_core(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        payload = {"ok": True, "written": 3, "archived": 1}
        calls: list[dict[str, object]] = []

        def fake_generate_suitecloud_wiki(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_rag_mcp.server.run_generate_suitecloud_wiki", fake_generate_suitecloud_wiki)

        result = generate_suitecloud_wiki_tool(
            vault_root=str(vault),
            project="huideng",
            source_name="huideng",
            auto_index=False,
        )

        assert result == payload
        assert calls == [
            {
                "vault_root": str(vault),
                "project": "huideng",
                "source_name": "huideng",
                "auto_index": False,
                "llm_summary": False,
            }
        ]


class TestGetIndexStatusPerSource:
    """Test that get_index_status_tool returns per-source statistics."""

    def test_status_with_per_source_stats(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        # Index first
        index_sources_tool(str(vault), mode="full", embedder=FakeEmbedder())

        # Check status
        status = get_index_status_tool(str(vault))

        assert status["indexed"] is True
        assert status["collection_count"] > 0
        assert "sources" in status
        assert "obsidian" in status["sources"]

        source_stats = status["sources"]["obsidian"]
        assert "file_count" in source_stats
        assert "last_indexed" in source_stats

    def test_status_shows_per_source_file_counts(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/note1.md", NOTE_CONTENT)
        _write_note(vault, "projects/note2.md", NOTE_CONTENT)

        index_sources_tool(str(vault), mode="full", embedder=FakeEmbedder())

        status = get_index_status_tool(str(vault))
        assert status["sources"]["obsidian"]["file_count"] == 2

    def test_status_with_code_source(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        js_path = vault / "projects" / "test-script.js"
        js_path.parent.mkdir(parents=True, exist_ok=True)
        js_path.write_text(JS_CONTENT, encoding="utf-8")

        index_sources_tool(str(vault), mode="full", embedder=FakeEmbedder())

        status = get_index_status_tool(str(vault))
        assert "obsidian" in status["sources"]
        assert "netsuite_repo" in status["sources"]
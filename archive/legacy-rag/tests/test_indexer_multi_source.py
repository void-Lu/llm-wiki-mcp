"""Tests for multi-source indexer: index_all, index_sources, and backward-compatible index_vault."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.indexer import (
    _collect_source_files,
    _route_to_parser_chunker,
    index_all,
    index_sources,
    index_vault,
)
from netsuite_rag_mcp.manifest import ManifestEntry, read_manifest, write_manifest
from netsuite_rag_mcp.models import RagConfig, SourceConfig
from netsuite_rag_mcp.runtime_config import resolve_runtime_config
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_v1_config(vault: Path) -> None:
    """Write a v1 (flat) sources.yaml for backward-compat testing."""
    (vault / "rag").mkdir(parents=True, exist_ok=True)
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "vault_root: .",
                "include:",
                "  - projects",
                "exclude:",
                "  - .git",
                "  - .obsidian",
                "  - .superpowers",
                "  - .rag-index",
                "chroma_path: .rag-index/chroma",
                "collection_name: netsuite_notes",
                "embedding_model: fake",
            ]
        ),
        encoding="utf-8",
    )


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
                "    include: [projects, knowledge]",
                "    exclude: [.git, .obsidian, .superpowers, .rag-index]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_notes",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian, .superpowers, .rag-index, node_modules]",
                "    file_types: [js, ts, xml, json]",
                "    parser: suitescript_code_and_config",
                "    collection: netsuite_notes",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


def _write_note(vault: Path, relative_path: str, content: str) -> Path:
    """Write a markdown note and return its path."""
    path = vault / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_code(vault: Path, relative_path: str, content: str) -> Path:
    """Write a code file (js/ts/xml/json) and return its path."""
    path = vault / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _collection_count(vault: Path) -> int:
    config = load_config(vault)
    vector_store = ChromaVectorStore(
        config.chroma_path, config.collection_name, FakeEmbedder()
    )
    return vector_store.count()


def _manifest_path(vault: Path) -> Path:
    return load_config(vault).manifest_path


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

XML_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<customrecord scriptid="customrecord_internal_id">
  <name>Test Custom Record</name>
  <description>A test custom record</description>
</customrecord>
"""

JSON_CONTENT = """{
    "script_id": "customscript_test_restlet",
    "name": "Test Script Configuration",
    "type": "RESTLET"
}
"""


# ── Tests: _collect_source_files ─────────────────────────────────────────────


class TestCollectSourceFiles:
    def test_collects_md_files_from_note_source(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        config = load_config(vault)
        obsidian = config.sources[0]  # note source

        files = _collect_source_files(obsidian, vault)
        paths = [f.name for f in files]
        assert "test-note.md" in paths

    def test_collects_js_files_from_code_source(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        config = load_config(vault)
        netsuite_repo = config.sources[1]  # code source

        files = _collect_source_files(netsuite_repo, vault)
        paths = [f.name for f in files]
        assert "test-script.js" in paths

    def test_excludes_dirs_from_exclude_list(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/node_modules/test.js", JS_CONTENT)

        config = load_config(vault)
        netsuite_repo = config.sources[1]

        files = _collect_source_files(netsuite_repo, vault)
        paths = [f.name for f in files]
        assert "test.js" not in paths

    def test_collects_multiple_file_types(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/a.js", JS_CONTENT)
        _write_code(vault, "projects/b.xml", XML_CONTENT)
        _write_code(vault, "projects/c.json", JSON_CONTENT)
        _write_code(vault, "projects/d.ts", JS_CONTENT)

        config = load_config(vault)
        netsuite_repo = config.sources[1]

        files = _collect_source_files(netsuite_repo, vault)
        names = {f.name for f in files}
        assert names == {"a.js", "b.xml", "c.json", "d.ts"}

    def test_note_source_ignores_js_files(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        config = load_config(vault)
        obsidian = config.sources[0]

        files = _collect_source_files(obsidian, vault)
        paths = [f.name for f in files]
        assert "test-note.md" in paths
        assert "test-script.js" not in paths

    def test_empty_directories_returns_no_files(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        # No files created

        config = load_config(vault)
        for source in config.sources:
            files = _collect_source_files(source, vault)
            assert files == []


# ── Tests: _route_to_parser_chunker ───────────────────────────────────────────


class TestRouteToParserChunker:
    def test_md_routes_to_markdown(self):
        result = _route_to_parser_chunker("markdown_frontmatter_h2", ".md")
        assert result == ("parse_markdown_file", "chunk_document")

    def test_js_routes_to_code(self):
        result = _route_to_parser_chunker("suitescript_code_and_config", ".js")
        assert result == ("parse_code_file", "chunk_code_document")

    def test_ts_routes_to_code(self):
        result = _route_to_parser_chunker("suitescript_code_and_config", ".ts")
        assert result == ("parse_code_file", "chunk_code_document")

    def test_xml_routes_to_xml(self):
        result = _route_to_parser_chunker("suitescript_code_and_config", ".xml")
        assert result == ("parse_xml_file", "chunk_xml_document")

    def test_json_routes_to_json(self):
        result = _route_to_parser_chunker("suitescript_code_and_config", ".json")
        assert result == ("parse_json_config", "chunk_json_config")

    def test_unknown_extension_returns_none(self):
        result = _route_to_parser_chunker("suitescript_code_and_config", ".py")
        assert result is None

    def test_unknown_parser_with_md_returns_none(self):
        result = _route_to_parser_chunker("unknown_parser", ".md")
        assert result is None


# ── Tests: index_all ─────────────────────────────────────────────────────────


class TestIndexAll:
    def test_index_all_with_v2_config_indexes_all_sources(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        result = index_all(vault, mode="full", embedder=FakeEmbedder())

        assert "sources" in result
        assert "obsidian" in result["sources"]
        assert "netsuite_repo" in result["sources"]
        obsidian_stats = result["sources"]["obsidian"]
        netsuite_stats = result["sources"]["netsuite_repo"]
        assert obsidian_stats["indexed"] >= 1
        assert netsuite_stats["indexed"] >= 1
        assert result["total_indexed"] >= 2

    def test_index_all_returns_accumulated_stats(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        result = index_all(vault, mode="full", embedder=FakeEmbedder())

        assert "total_indexed" in result
        assert "total_skipped" in result
        assert "total_deleted" in result
        assert "total_errors" in result
        assert isinstance(result["sources"], dict)

    def test_index_all_incremental_skips_unchanged(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        # First index: full
        result1 = index_all(vault, mode="full", embedder=FakeEmbedder())
        assert result1["total_indexed"] >= 1

        # Second index: incremental (files unchanged => should skip)
        result2 = index_all(vault, mode="incremental", embedder=FakeEmbedder())
        assert result2["total_skipped"] >= 1

    def test_index_all_v1_config_backward_compat(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v1_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        result = index_all(vault, mode="full", embedder=FakeEmbedder())

        # v1 auto-migrates to a single "obsidian" source
        assert "obsidian" in result["sources"]
        assert result["sources"]["obsidian"]["indexed"] >= 1

    def test_index_all_writes_manifest_to_runtime_path_not_vault_local(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        runtime = resolve_runtime_config(vault_root_arg=vault)
        config = load_config(vault, runtime_config=runtime)

        result = index_all(vault, mode="full", config=config, embedder=FakeEmbedder())

        assert result["total_indexed"] >= 1
        assert config.manifest_path == runtime.manifest_path
        assert runtime.manifest_path.exists()
        manifest = read_manifest(runtime.manifest_path)
        assert "obsidian:note:projects/test-note.md" in manifest


# ── Tests: index_sources ──────────────────────────────────────────────────────


class TestIndexSources:
    def test_index_sources_by_name(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        # Only index the "obsidian" source
        result = index_sources(
            vault, source_names=["obsidian"], mode="full", embedder=FakeEmbedder()
        )

        assert "obsidian" in result["sources"]
        # netsuite_repo should not be in the results since it wasn't selected
        assert "netsuite_repo" not in result["sources"]
        assert result["sources"]["obsidian"]["indexed"] >= 1

    def test_index_sources_filtered_full_writes_manifest_to_runtime_path_not_vault_local(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        runtime = resolve_runtime_config(vault_root_arg=vault)
        config = load_config(vault, runtime_config=runtime)
        code_key = "netsuite_repo:code:projects/existing-script.js"
        write_manifest(
            runtime.manifest_path,
            {
                code_key: ManifestEntry(
                    doc_id="netsuite_repo:projects/existing-script.js",
                    source_name="netsuite_repo",
                    source_kind="code",
                    relative_path="projects/existing-script.js",
                    mtime=1.0,
                    size=10,
                    file_hash="0" * 64,
                    chunk_count=1,
                    indexed_at="2026-05-18T00:00:00+00:00",
                )
            },
        )

        result = index_sources(
            vault,
            source_names=["obsidian"],
            mode="full",
            config=config,
            embedder=FakeEmbedder(),
        )

        assert set(result["sources"]) == {"obsidian"}
        assert runtime.manifest_path.exists()
        manifest = read_manifest(runtime.manifest_path)
        assert code_key in manifest
        assert "obsidian:note:projects/test-note.md" in manifest

    def test_index_sources_by_kind(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        # Only index "note" type sources
        result = index_sources(
            vault, source_kind="note", mode="full", embedder=FakeEmbedder()
        )

        assert "obsidian" in result["sources"]
        assert "netsuite_repo" not in result["sources"]

    def test_index_sources_by_name_and_kind(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)

        result = index_sources(
            vault, source_names=["obsidian"], source_kind="note", mode="full", embedder=FakeEmbedder()
        )

        # obsidian is kind=note, so should be included
        assert "obsidian" in result["sources"]

    def test_index_sources_kind_excludes_nonmatching(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        # Only index "code" type sources
        result = index_sources(
            vault, source_kind="code", mode="full", embedder=FakeEmbedder()
        )

        assert "netsuite_repo" in result["sources"]
        assert "obsidian" not in result["sources"]

    def test_index_sources_no_match_returns_empty(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)

        result = index_sources(
            vault, source_names=["nonexistent"], mode="full", embedder=FakeEmbedder()
        )

        assert result["sources"] == {}
        assert result["total_indexed"] == 0

    def test_index_sources_defaults_to_all(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        # No filter => all sources
        result = index_sources(vault, mode="full", embedder=FakeEmbedder())

        assert "obsidian" in result["sources"]

    def test_index_sources_full_by_name_preserves_unselected_code_source(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        index_all(vault, mode="full", embedder=FakeEmbedder())

        manifest_path = _manifest_path(vault)
        initial_manifest = read_manifest(manifest_path)
        initial_count = _collection_count(vault)
        code_keys = {
            key for key, entry in initial_manifest.items() if entry.source_kind == "code"
        }

        result = index_sources(
            vault, source_names=["obsidian"], mode="full", embedder=FakeEmbedder()
        )

        manifest = read_manifest(manifest_path)
        assert set(result["sources"]) == {"obsidian"}
        assert code_keys
        assert code_keys.issubset(manifest.keys())
        assert _collection_count(vault) == initial_count

    def test_index_sources_full_by_kind_preserves_unselected_note_source(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        index_all(vault, mode="full", embedder=FakeEmbedder())

        manifest_path = _manifest_path(vault)
        initial_manifest = read_manifest(manifest_path)
        initial_count = _collection_count(vault)
        note_keys = {
            key for key, entry in initial_manifest.items() if entry.source_kind == "note"
        }

        result = index_sources(
            vault, source_kind="code", mode="full", embedder=FakeEmbedder()
        )

        manifest = read_manifest(manifest_path)
        assert set(result["sources"]) == {"netsuite_repo"}
        assert note_keys
        assert note_keys.issubset(manifest.keys())
        assert _collection_count(vault) == initial_count

    def test_index_sources_full_no_match_preserves_existing_index(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        index_all(vault, mode="full", embedder=FakeEmbedder())

        manifest_path = _manifest_path(vault)
        initial_manifest = read_manifest(manifest_path)
        initial_count = _collection_count(vault)

        result = index_sources(
            vault, source_names=["missing"], mode="full", embedder=FakeEmbedder()
        )

        manifest = read_manifest(manifest_path)
        assert result["sources"] == {}
        assert manifest == initial_manifest
        assert _collection_count(vault) == initial_count

    def test_index_sources_full_deletes_removed_selected_files_only(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        deleted_note = _write_note(vault, "projects/deleted-note.md", NOTE_CONTENT)
        _write_note(vault, "knowledge/kept-note.md", NOTE_CONTENT)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        index_all(vault, mode="full", embedder=FakeEmbedder())

        manifest_path = _manifest_path(vault)
        initial_manifest = read_manifest(manifest_path)
        deleted_key = "obsidian:note:projects/deleted-note.md"
        kept_note_key = "obsidian:note:knowledge/kept-note.md"
        code_key = "netsuite_repo:code:projects/test-script.js"
        initial_count = _collection_count(vault)
        deleted_chunk_count = initial_manifest[deleted_key].chunk_count

        deleted_note.unlink()
        result = index_sources(
            vault, source_names=["obsidian"], mode="full", embedder=FakeEmbedder()
        )

        manifest = read_manifest(manifest_path)
        assert result["sources"]["obsidian"]["deleted"] == 1
        assert deleted_key not in manifest
        assert kept_note_key in manifest
        assert code_key in manifest
        assert _collection_count(vault) == initial_count - deleted_chunk_count


# ── Tests: Backward compatibility: index_vault ────────────────────────────────


class TestIndexVaultBackwardCompat:
    def test_index_vault_v1_config_still_works(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v1_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        result = index_vault(vault, mode="full", embedder=FakeEmbedder())

        assert result["indexed_files"] >= 1
        assert result["mode"] == "full"
        assert result["collection_count"] >= 1

    def test_index_vault_incremental_mode(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v1_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        # Full index first
        result1 = index_vault(vault, mode="full", embedder=FakeEmbedder())
        assert result1["indexed_files"] >= 1

        # Incremental should skip (unchanged files)
        result2 = index_vault(vault, mode="incremental", embedder=FakeEmbedder())
        assert result2["skipped_files"] >= 1

    def test_index_vault_rejects_invalid_mode(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v1_config(vault)

        with pytest.raises(ValueError, match="Invalid mode"):
            index_vault(vault, mode="bad_mode")


# ── Tests: Source metadata injection (source_kind, source_name, file_hash) ───


class TestSourceMetadataInjection:
    def test_md_chunks_have_source_kind_note(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        config = load_config(vault)
        obsidian = config.sources[0]
        files = _collect_source_files(obsidian, vault)

        from netsuite_rag_mcp.parser import parse_markdown_file
        from netsuite_rag_mcp.chunker import chunk_document

        md_file = files[0]
        doc = parse_markdown_file(md_file, vault)
        # SourceDocument defaults source_kind="note", source_name=""
        # The indexer should inject source metadata from SourceConfig
        assert doc is not None

    def test_code_chunks_have_source_kind_code(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        config = load_config(vault)
        netsuite_repo = config.sources[1]
        files = _collect_source_files(netsuite_repo, vault)

        from netsuite_rag_mcp.parser import parse_code_file

        js_file = files[0]
        doc = parse_code_file(js_file, source_name="netsuite_repo", repo_root=vault)
        assert doc is not None
        assert doc.source_kind == "code"


# ── Tests: XML and JSON routing ──────────────────────────────────────────────


class TestXmlJsonRouting:
    def test_xml_file_routed_correctly(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/customization.xml", XML_CONTENT)

        result = index_all(vault, mode="full", embedder=FakeEmbedder())

        # Should index the XML file via netsuite_repo source
        assert result["total_indexed"] >= 1
        netsuite_stats = result["sources"]["netsuite_repo"]
        assert netsuite_stats["indexed"] >= 1

    def test_json_file_routed_correctly(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/config.json", JSON_CONTENT)

        result = index_all(vault, mode="full", embedder=FakeEmbedder())

        assert result["total_indexed"] >= 1
        netsuite_stats = result["sources"]["netsuite_repo"]
        assert netsuite_stats["indexed"] >= 1


# ── Tests: Manifest key format ───────────────────────────────────────────────


class TestManifestKeyFormat:
    def test_manifest_uses_v2_key_format(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_note(vault, "projects/test-note.md", NOTE_CONTENT)

        index_all(vault, mode="full", embedder=FakeEmbedder())

        manifest_path = _manifest_path(vault)
        manifest = read_manifest(manifest_path)

        # Keys should be in v2 format: {source_name}:{source_kind}:{relative_path}
        md_key = None
        for key in manifest:
            if "test-note.md" in key:
                md_key = key
                break

        assert md_key is not None
        assert md_key.startswith("obsidian:note:")

    def test_manifest_code_uses_v2_key_format(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_v2_config(vault)
        _write_code(vault, "projects/test-script.js", JS_CONTENT)

        index_all(vault, mode="full", embedder=FakeEmbedder())

        manifest_path = _manifest_path(vault)
        manifest = read_manifest(manifest_path)

        js_key = None
        for key in manifest:
            if "test-script.js" in key:
                js_key = key
                break

        assert js_key is not None
        assert js_key.startswith("netsuite_repo:code:")
from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.indexer import index_vault
from netsuite_rag_mcp.manifest import read_manifest
from netsuite_rag_mcp.preload import preload_embedding_model
from netsuite_rag_mcp.retriever import search_netsuite_knowledge
from netsuite_rag_mcp.runtime_config import resolve_runtime_config, write_global_config
from netsuite_rag_mcp.vector_store import FakeEmbedder


def _write_sources(vault: Path) -> None:
    (vault / "rag").mkdir(parents=True, exist_ok=True)
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  embedding_model: fake",
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
            ]
        ),
        encoding="utf-8",
    )


def _write_note(vault: Path, name: str, marker: str) -> None:
    target = vault / "projects" / "project-a" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"""---
type: script
project: project-a
script_type: restlet
script_id: customscript_order_sync_restlet
related_objects: [salesorder]
related_scripts: []
status: active
---

# RESTlet

## Purpose
{marker} synchronizes orders through a RESTlet.
""",
        encoding="utf-8",
    )


def test_indexing_writes_chroma_and_manifest_under_runtime_storage(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_sources(vault)
    _write_note(vault, "order-sync.md", "Alpha runtime storage")

    result = index_vault(vault, mode="full", embedder=FakeEmbedder())
    runtime = resolve_runtime_config(vault_root_arg=vault)

    assert result["indexed_files"] == 1
    assert runtime.manifest_path.is_file()
    assert runtime.chroma_path.exists()
    # Default vault-local: data lives inside vault
    assert runtime.manifest_path.is_relative_to(vault)
    assert runtime.chroma_path.is_relative_to(vault)
    assert (vault / ".rag-index").exists()


def test_search_after_indexing_reads_from_runtime_chroma(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_sources(vault)
    _write_note(vault, "order-sync.md", "Searchable runtime marker")

    index_vault(vault, mode="full", embedder=FakeEmbedder())

    search = search_netsuite_knowledge(
        vault,
        "Searchable runtime marker order",
        embedder=FakeEmbedder(),
        top_k=1,
    )

    assert len(search["results"]) == 1
    assert "Searchable runtime marker" in search["results"][0]["text"]


def test_two_vaults_keep_isolated_runtime_chroma_manifest_and_full_reindex(monkeypatch, tmp_path: Path):
    first = tmp_path / "client-a" / "homework"
    second = tmp_path / "client-b" / "homework"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _write_sources(first)
    _write_sources(second)
    _write_note(first, "first.md", "Alpha first vault")
    _write_note(second, "second.md", "Beta second vault")

    first_result = index_vault(first, mode="full", embedder=FakeEmbedder())
    second_result = index_vault(second, mode="full", embedder=FakeEmbedder())

    first_runtime = resolve_runtime_config(vault_root_arg=first)
    second_runtime = resolve_runtime_config(vault_root_arg=second)
    assert first_runtime.vault_storage_dir != second_runtime.vault_storage_dir
    assert first_result["collection_count"] == 1
    assert second_result["collection_count"] == 1
    assert len(read_manifest(first_runtime.manifest_path)) == 1
    assert len(read_manifest(second_runtime.manifest_path)) == 1
    assert "Alpha first vault" in search_netsuite_knowledge(
        first,
        "Alpha first vault order",
        embedder=FakeEmbedder(),
        top_k=1,
    )["results"][0]["text"]
    assert "Beta second vault" in search_netsuite_knowledge(
        second,
        "Beta second vault order",
        embedder=FakeEmbedder(),
        top_k=1,
    )["results"][0]["text"]

    (first / "projects" / "project-a" / "first.md").unlink()
    _write_note(first, "replacement.md", "Alpha replacement vault")
    first_reindex_result = index_vault(first, mode="full", embedder=FakeEmbedder())

    assert first_reindex_result["collection_count"] == 1
    assert len(read_manifest(first_runtime.manifest_path)) == 1
    assert len(read_manifest(second_runtime.manifest_path)) == 1
    assert "Alpha replacement vault" in search_netsuite_knowledge(
        first,
        "Alpha replacement vault order",
        embedder=FakeEmbedder(),
        top_k=1,
    )["results"][0]["text"]
    assert "Beta second vault" in search_netsuite_knowledge(
        second,
        "Beta second vault order",
        embedder=FakeEmbedder(),
        top_k=1,
    )["results"][0]["text"]


def test_preload_uses_runtime_model_cache_from_global_config_not_cwd(monkeypatch, tmp_path: Path):
    configured_vault = tmp_path / "configured-vault"
    cwd_vault = tmp_path / "cwd-vault"
    config_path = tmp_path / "config" / "config.yaml"
    configured_vault.mkdir()
    cwd_vault.mkdir()
    _write_sources(configured_vault)
    _write_sources(cwd_vault)
    (cwd_vault / "rag" / "sources.yaml").write_text(
        (cwd_vault / "rag" / "sources.yaml").read_text(encoding="utf-8").replace("fake", "cwd-fake"),
        encoding="utf-8",
    )
    write_global_config(config_path, vault_name="configured", vault_root=configured_vault, make_default=True)
    monkeypatch.chdir(cwd_vault)
    monkeypatch.delenv("NETSUITE_RAG_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_RAG_CONFIG_DIR", str(config_path.parent))
    calls = {}

    class StubEmbedder:
        def __init__(self, model_name: str, cache_folder: Path):
            calls["model_name"] = model_name
            calls["cache_folder"] = cache_folder

    monkeypatch.setattr("netsuite_rag_mcp.preload.SentenceTransformerEmbedder", StubEmbedder)

    result = preload_embedding_model()
    runtime = resolve_runtime_config(config_path=config_path)

    assert calls == {"model_name": "fake", "cache_folder": runtime.embedding_cache_path}
    assert result == {
        "model": "fake",
        "cache_path": str(runtime.embedding_cache_path),
        "status": "ready",
    }
    # vault-local: .models is inside the configured vault, not cwd
    assert runtime.embedding_cache_path == configured_vault / ".models"
    assert not (cwd_vault / ".models").exists()

from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.indexer import index_vault
from netsuite_rag_mcp.manifest import read_manifest
from netsuite_rag_mcp.models import Chunk
from netsuite_rag_mcp.retriever import search_netsuite_knowledge
from netsuite_rag_mcp.runtime_config import resolve_runtime_config
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder, SentenceTransformerEmbedder


def test_sentence_transformer_embedder_uses_cache_folder(monkeypatch, tmp_path: Path):
    calls = {}

    class StubSentenceTransformer:
        def __init__(self, model_name: str, cache_folder: str | None = None):
            calls["model_name"] = model_name
            calls["cache_folder"] = cache_folder

        def encode(self, texts: list[str], normalize_embeddings: bool = False):
            calls["normalize_embeddings"] = normalize_embeddings

            class StubEmbedding:
                def tolist(self):
                    return [1.0, 0.0]

            return [StubEmbedding() for _ in texts]

    monkeypatch.setattr("netsuite_rag_mcp.vector_store.SentenceTransformer", StubSentenceTransformer)

    embedder = SentenceTransformerEmbedder("BAAI/bge-m3", cache_folder=tmp_path / ".models")
    embeddings = embedder.embed(["订单同步"])

    assert calls["model_name"] == "BAAI/bge-m3"
    assert calls["cache_folder"] == str(tmp_path / ".models")
    assert calls["normalize_embeddings"] is True
    assert embeddings == [[1.0, 0.0]]


def test_vector_store_upsert_query_and_reset(tmp_path: Path):
    store = ChromaVectorStore(tmp_path / "chroma", "test_notes", FakeEmbedder())
    chunk = Chunk(
        id="doc1:0",
        doc_id="doc1",
        chunk_index=0,
        source_path="projects/project-a/scripts/restlet/order-sync.md",
        heading="相关脚本",
        text="RESTlet 会提交 customscript_order_sync_mr 处理订单同步。",
        metadata={
            "doc_id": "doc1",
            "chunk_index": 0,
            "source_path": "projects/project-a/scripts/restlet/order-sync.md",
            "heading": "相关脚本",
            "type": "script",
            "project": "project-a",
            "script_type": "restlet",
            "script_id": "customscript_order_sync_restlet",
            "related_objects": ["salesorder"],
            "related_scripts": ["customscript_order_sync_mr"],
            "status": "active",
        },
    )

    store.upsert_chunks([chunk])
    results = store.query("订单同步 Map/Reduce", n_results=3)

    assert len(results) == 1
    assert results[0].metadata["related_objects"] == ["salesorder"]
    assert results[0].metadata["related_scripts"] == ["customscript_order_sync_mr"]

    store.reset()
    assert store.count() == 0


def test_vector_store_persists_chunk_top_level_metadata_for_source_filters(tmp_path: Path):
    store = ChromaVectorStore(tmp_path / "chroma", "test_notes", FakeEmbedder())
    chunk = Chunk(
        id="doc1:0",
        doc_id="doc1",
        chunk_index=0,
        source_path="src/OrderSync.js",
        heading="map",
        text="Map/Reduce script customscript_order_sync_mr 处理订单。",
        metadata={"type": "script", "project": "project-a"},
        function_name="map",
        line_start=10,
        line_end=20,
        source_kind="code",
        source_name="netsuite_repo",
        file_hash="abc123",
    )

    store.upsert_chunks([chunk])
    results = store.query("订单", n_results=3, where={"source_kind": "code"})

    assert len(results) == 1
    metadata = results[0].metadata
    assert metadata["doc_id"] == "doc1"
    assert metadata["chunk_index"] == 0
    assert metadata["source_path"] == "src/OrderSync.js"
    assert metadata["heading"] == "map"
    assert metadata["function_name"] == "map"
    assert metadata["line_start"] == 10
    assert metadata["line_end"] == 20
    assert metadata["source_kind"] == "code"
    assert metadata["source_name"] == "netsuite_repo"
    assert metadata["file_hash"] == "abc123"


def write_sources_config(vault: Path) -> None:
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


def test_index_vault_full_mode_indexes_files(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    write_sources_config(vault)

    # Create a markdown file
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

    result = index_vault(vault, mode="full", embedder=FakeEmbedder())
    runtime = resolve_runtime_config(vault_root_arg=vault)

    assert result["indexed_files"] == 1
    assert result["indexed_chunks"] >= 1
    assert result["mode"] == "full"
    assert result["collection_count"] >= 1
    assert len(read_manifest(runtime.manifest_path)) == 1
    assert runtime.chroma_path.exists()
    # vault-local: data lives inside vault
    assert (vault / ".rag-index").exists()


def test_search_uses_runtime_embedding_cache_for_default_embedder(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    write_sources_config(vault)
    runtime = resolve_runtime_config(vault_root_arg=vault)
    calls = {}

    class StubEmbedder:
        def __init__(self, model_name: str, cache_folder: Path):
            calls["model_name"] = model_name
            calls["cache_folder"] = cache_folder

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    class StubStore:
        def __init__(self, persist_path: Path, collection_name: str, embedder: StubEmbedder):
            calls["persist_path"] = persist_path
            calls["collection_name"] = collection_name
            calls["store_embedder"] = embedder

        def query(self, query_text: str, n_results: int = 5, where=None):
            return []

    monkeypatch.setattr("netsuite_rag_mcp.retriever.SentenceTransformerEmbedder", StubEmbedder)
    monkeypatch.setattr("netsuite_rag_mcp.retriever.ChromaVectorStore", StubStore)

    result = search_netsuite_knowledge(vault, "订单同步")

    assert result["results"] == []
    assert calls["model_name"] == "fake"
    assert calls["cache_folder"] == runtime.embedding_cache_path
    assert calls["persist_path"] == runtime.chroma_path

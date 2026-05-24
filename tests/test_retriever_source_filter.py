"""Tests for T11: source_kind/source_name filtering in retriever."""
from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.models import Chunk, SearchResult
from netsuite_rag_mcp.retriever import _build_where_clause, ask_netsuite_rag, search_netsuite_knowledge
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder


def _write_sources_config(vault: Path) -> None:
    (vault / "rag").mkdir(parents=True, exist_ok=True)
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  embedding_model: fake",
                "  collections:",
                "    default: test_source_filter",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: test_source_filter",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [src]",
                "    exclude: [.git, node_modules]",
                "    file_types: [js]",
                "    parser: suitescript_code_and_config",
                "    collection: test_source_filter",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


def _setup_store_with_mixed_sources(tmp_path: Path) -> ChromaVectorStore:
    """Create a vector store with chunks from different source kinds/names."""
    _write_sources_config(tmp_path)
    store = ChromaVectorStore(tmp_path / "chroma", "test_source_filter", FakeEmbedder())

    note_chunk = Chunk(
        id="note_doc1:0",
        doc_id="note_doc1",
        chunk_index=0,
        source_path="projects/project-a/scripts/restlet/order-sync.md",
        heading="RESTlet 订单同步",
        text="RESTlet 会提交 customscript_order_sync_mr 处理订单同步。",
        metadata={
            "doc_id": "note_doc1",
            "chunk_index": 0,
            "source_path": "projects/project-a/scripts/restlet/order-sync.md",
            "heading": "RESTlet 订单同步",
            "type": "script",
            "project": "project-a",
            "script_type": "restlet",
            "script_id": "customscript_order_sync_restlet",
            "related_objects": ["salesorder"],
            "related_scripts": ["customscript_order_sync_mr"],
            "status": "active",
            "source_kind": "note",
            "source_name": "obsidian",
        },
    )

    code_chunk = Chunk(
        id="code_doc1:0",
        doc_id="code_doc1",
        chunk_index=0,
        source_path="src/OrderSync.js",
        heading="afterSubmit",
        text="Map/Reduce script customscript_order_sync_mr 处理订单。",
        metadata={
            "doc_id": "code_doc1",
            "chunk_index": 0,
            "source_path": "src/OrderSync.js",
            "heading": "afterSubmit",
            "type": "script",
            "project": "project-a",
            "script_type": "mapreduce",
            "script_id": "customscript_order_sync_mr",
            "related_objects": ["salesorder"],
            "related_scripts": [],
            "status": "active",
            "source_kind": "code",
            "source_name": "netsuite_repo",
        },
    )

    code_chunk_2 = Chunk(
        id="code_doc2:0",
        doc_id="code_doc2",
        chunk_index=0,
        source_path="lib/Validator.js",
        heading="validate",
        text="验证函数 Validator 不会处理订单。",
        metadata={
            "doc_id": "code_doc2",
            "chunk_index": 0,
            "source_path": "lib/Validator.js",
            "heading": "validate",
            "type": "script",
            "project": "project-b",
            "script_type": "clientscript",
            "script_id": "customscript_validator_cs",
            "related_objects": [],
            "related_scripts": [],
            "status": "active",
            "source_kind": "code",
            "source_name": "suitecommerce-extension",
        },
    )

    store.upsert_chunks([note_chunk, code_chunk, code_chunk_2])
    return store


def test_search_filters_by_source_kind_note(tmp_path: Path):
    """search_netsuite_knowledge returns only note chunks when source_kind='note'."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["results"]) == 1
    assert result["results"][0]["metadata"]["source_kind"] == "note"
    assert result["results"][0]["metadata"]["source_name"] == "obsidian"


def test_search_filters_by_source_kind_code(tmp_path: Path):
    """search_netsuite_knowledge returns only code chunks when source_kind='code'."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="code",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["results"]) == 2
    for r in result["results"]:
        assert r["metadata"]["source_kind"] == "code"


def test_search_filters_by_source_name(tmp_path: Path):
    """search_netsuite_knowledge returns only chunks matching source_name."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单同步",
        vault_root=tmp_path,
        source_name="netsuite_repo",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["results"]) == 1
    assert result["results"][0]["metadata"]["source_name"] == "netsuite_repo"


def test_search_filters_by_source_kind_and_source_name(tmp_path: Path):
    """search_netsuite_knowledge applies both source_kind and source_name as AND filters."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单",
        vault_root=tmp_path,
        source_kind="code",
        source_name="suitecommerce-extension",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["results"]) == 1
    assert result["results"][0]["metadata"]["source_kind"] == "code"
    assert result["results"][0]["metadata"]["source_name"] == "suitecommerce-extension"


def test_search_no_filter_returns_all(tmp_path: Path):
    """search_netsuite_knowledge without source filters returns all chunks (backward compat)."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单",
        vault_root=tmp_path,
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["results"]) == 3


def test_search_existing_filters_combined_with_source_kind(tmp_path: Path):
    """source_kind filter works alongside existing project/script_type filters."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单",
        vault_root=tmp_path,
        filters={"project": "project-a"},
        source_kind="code",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["results"]) == 1
    assert result["results"][0]["metadata"]["project"] == "project-a"
    assert result["results"][0]["metadata"]["source_kind"] == "code"


def test_ask_netsuite_rag_passes_source_filters(tmp_path: Path):
    """ask_netsuite_rag correctly passes source_kind and source_name through."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    # ask_netsuite_rag should return context_blocks filtered to note only
    assert len(result["context_blocks"]) == 1
    assert result["context_blocks"][0]["metadata"]["source_kind"] == "note"
    # filters should include source_kind
    assert result["filters"]["source_kind"] == "note"


def test_ask_netsuite_rag_passes_source_name(tmp_path: Path):
    """ask_netsuite_rag correctly passes source_name through."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = ask_netsuite_rag(
        question="订单",
        vault_root=tmp_path,
        source_name="obsidian",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert len(result["context_blocks"]) == 1
    assert result["context_blocks"][0]["metadata"]["source_name"] == "obsidian"


def test_search_returns_source_kind_in_filters(tmp_path: Path):
    """When source_kind is set, it appears in the returned filters dict."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert "source_kind" in result["filters"]
    assert result["filters"]["source_kind"] == "note"


def test_search_returns_source_name_in_filters(tmp_path: Path):
    """When source_name is set, it appears in the returned filters dict."""
    store = _setup_store_with_mixed_sources(tmp_path)

    result = search_netsuite_knowledge(
        question="订单",
        vault_root=tmp_path,
        source_name="netsuite_repo",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )

    assert "source_name" in result["filters"]
    assert result["filters"]["source_name"] == "netsuite_repo"


# --- Tests for _build_where_clause ---


def test_build_where_clause_returns_none_when_no_filters():
    """_build_where_clause returns None when no source filters are provided."""
    assert _build_where_clause(source_kind=None, source_name=None) is None


def test_build_where_clause_single_source_kind():
    """_build_where_clause returns a simple dict for a single source_kind filter."""
    result = _build_where_clause(source_kind="note", source_name=None)
    assert result == {"source_kind": "note"}


def test_build_where_clause_single_source_name():
    """_build_where_clause returns a simple dict for a single source_name filter."""
    result = _build_where_clause(source_kind=None, source_name="obsidian")
    assert result == {"source_name": "obsidian"}


def test_build_where_clause_both_filters_uses_and():
    """_build_where_clause returns $and clause when both source_kind and source_name are set."""
    result = _build_where_clause(source_kind="code", source_name="netsuite_repo")
    assert result == {"$and": [{"source_kind": "code"}, {"source_name": "netsuite_repo"}]}


class StubStore:
    def __init__(self, results: list[SearchResult]):
        self.results = results
        self.where = None

    def query(self, query_text: str, n_results: int = 5, where: dict | None = None):
        self.where = where
        return self.results


def test_search_excludes_archived_results_by_default(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "schema_version: 2\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )
    active = SearchResult(
        citation_id="S1",
        chunk_id="active",
        text="active wiki",
        metadata={"type": "generated_wiki", "archived": False},
        distance=0.1,
    )
    archived = SearchResult(
        citation_id="S2",
        chunk_id="archived",
        text="archived wiki",
        metadata={"type": "generated_wiki", "archived": True},
        distance=0.2,
    )

    result = search_netsuite_knowledge(
        vault,
        "wiki",
        top_k=5,
        embedder=FakeEmbedder(),
        store=StubStore([archived, active]),
        include_archived=False,
    )

    assert [row["chunk_id"] for row in result["results"]] == ["active"]


def test_search_can_include_archived_results(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "schema_version: 2\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )
    archived = SearchResult(
        citation_id="S1",
        chunk_id="archived",
        text="archived wiki",
        metadata={"type": "generated_wiki", "archived": True},
        distance=0.1,
    )

    result = search_netsuite_knowledge(
        vault,
        "wiki",
        top_k=5,
        embedder=FakeEmbedder(),
        store=StubStore([archived]),
        include_archived=True,
    )

    assert [row["chunk_id"] for row in result["results"]] == ["archived"]
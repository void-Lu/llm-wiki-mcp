"""Tests for T12: Enhanced citation format with source_kind, function, line, git_commit."""
from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.models import Chunk
from netsuite_rag_mcp.retriever import _format_citation, ask_netsuite_rag
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder


# ── Unit tests for _format_citation ──────────────────────────────────────────


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
                "    default: test_citation",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: test_citation",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [src]",
                "    exclude: [.git, node_modules]",
                "    file_types: [js]",
                "    parser: suitescript_code_and_config",
                "    collection: test_citation",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


def test_format_note_citation_all_fields():
    """Note citation includes source_kind, path, heading, chunk_index, updated_at."""
    citation = {
        "source_kind": "note",
        "path": "projects/project-a/scripts/restlet/order-sync.md",
        "heading": "相关脚本",
        "chunk_index": 2,
        "updated_at": "2026-05-14T10:20:00Z",
    }
    result = _format_citation(citation)
    assert result.startswith("source_kind=note")
    assert "path=projects/project-a/scripts/restlet/order-sync.md" in result
    assert "heading=相关脚本" in result
    assert "chunk_index=2" in result
    assert "updated_at=2026-05-14T10:20:00Z" in result


def test_format_note_citation_missing_optional_fields():
    """Note citation omits missing optional fields."""
    citation = {
        "source_kind": "note",
        "path": "projects/project-a/scripts/restlet/order-sync.md",
    }
    result = _format_citation(citation)
    assert "source_kind=note" in result
    assert "path=projects/project-a/scripts/restlet/order-sync.md" in result
    assert "heading=" not in result
    assert "chunk_index=" not in result
    assert "updated_at=" not in result


def test_format_code_citation_all_fields():
    """Code citation includes source_kind, path, function, line, git_commit."""
    citation = {
        "source_kind": "code",
        "path": "project-a/src/restlets/order-sync-restlet.js",
        "function": "post",
        "line": "42-55",
        "git_commit": "abc1234",
    }
    result = _format_citation(citation)
    assert "source_kind=code" in result
    assert "path=project-a/src/restlets/order-sync-restlet.js" in result
    assert "function=post" in result
    assert "line=42-55" in result
    assert "git_commit=abc1234" in result


def test_format_code_citation_dirty():
    """Dirty code citation includes git_commit with +dirty suffix and file_hash."""
    citation = {
        "source_kind": "code",
        "path": "project-a/src/ue/salesorder-after-submit.js",
        "function": "afterSubmit",
        "line": "88-130",
        "git_commit": "abc1234+dirty",
        "file_hash": "sha256:deadbeef",
    }
    result = _format_citation(citation)
    assert "source_kind=code" in result
    assert "git_commit=abc1234+dirty" in result
    assert "file_hash=sha256:deadbeef" in result


def test_format_code_citation_missing_optional_fields():
    """Code citation omits missing optional fields."""
    citation = {
        "source_kind": "code",
        "path": "project-a/src/restlets/foo.js",
    }
    result = _format_citation(citation)
    assert "source_kind=code" in result
    assert "path=project-a/src/restlets/foo.js" in result
    assert "function=" not in result
    assert "line=" not in result
    assert "git_commit=" not in result


# ── Integration tests for ask_netsuite_rag enhanced sources ──────────────────


def _make_store_with_note_and_code(tmp_path: Path) -> ChromaVectorStore:
    """Create a store with both note and code chunks for integration testing."""
    _write_sources_config(tmp_path)
    store = ChromaVectorStore(tmp_path / "chroma", "test_citation", FakeEmbedder())

    note_chunk = Chunk(
        id="note_doc1:0",
        doc_id="note_doc1",
        chunk_index=2,
        source_path="projects/project-a/scripts/restlet/order-sync.md",
        heading="相关脚本",
        text="RESTlet 会提交 customscript_order_sync_mr 处理订单同步。",
        metadata={
            "doc_id": "note_doc1",
            "chunk_index": 2,
            "source_path": "projects/project-a/scripts/restlet/order-sync.md",
            "heading": "相关脚本",
            "type": "script",
            "project": "project-a",
            "script_type": "restlet",
            "script_id": "customscript_order_sync_restlet",
            "related_objects": ["salesorder"],
            "related_scripts": ["customscript_order_sync_mr"],
            "status": "active",
            "source_kind": "note",
            "source_name": "obsidian",
            "updated_at": "2026-05-14T10:20:00Z",
        },
        source_kind="note",
        source_name="obsidian",
    )

    code_chunk = Chunk(
        id="code_doc1:0",
        doc_id="code_doc1",
        chunk_index=0,
        source_path="project-a/src/restlets/order-sync-restlet.js",
        heading="post function",
        text="Map/Reduce script customscript_order_sync_mr 处理订单。",
        metadata={
            "doc_id": "code_doc1",
            "chunk_index": 0,
            "source_path": "project-a/src/restlets/order-sync-restlet.js",
            "heading": "post function",
            "type": "script",
            "project": "project-a",
            "script_type": "restlet",
            "script_id": "customscript_order_sync_restlet",
            "related_objects": ["salesorder"],
            "related_scripts": [],
            "status": "active",
            "source_kind": "code",
            "source_name": "netsuite_repo",
            "function_name": "post",
            "line_start": 42,
            "line_end": 55,
            "git_commit": "abc1234",
        },
        function_name="post",
        line_start=42,
        line_end=55,
        source_kind="code",
        source_name="netsuite_repo",
    )

    dirty_code_chunk = Chunk(
        id="code_doc2:0",
        doc_id="code_doc2",
        chunk_index=0,
        source_path="project-a/src/ue/salesorder-after-submit.js",
        heading="afterSubmit function",
        text="AfterSubmit script 处理提交后逻辑。",
        metadata={
            "doc_id": "code_doc2",
            "chunk_index": 0,
            "source_path": "project-a/src/ue/salesorder-after-submit.js",
            "heading": "afterSubmit function",
            "type": "script",
            "project": "project-a",
            "script_type": "userevent",
            "script_id": "customscript_salesorder_ue",
            "related_objects": ["salesorder"],
            "related_scripts": [],
            "status": "active",
            "source_kind": "code",
            "source_name": "netsuite_repo",
            "function_name": "afterSubmit",
            "line_start": 88,
            "line_end": 130,
            "git_commit": "abc1234",
            "file_hash": "sha256:deadbeef",
        },
        function_name="afterSubmit",
        line_start=88,
        line_end=130,
        source_kind="code",
        source_name="netsuite_repo",
        file_hash="sha256:deadbeef",
    )

    store.upsert_chunks([note_chunk, code_chunk, dirty_code_chunk])
    return store


def test_ask_netsuite_rag_note_source_has_source_kind_and_path(tmp_path: Path):
    """ask_netsuite_rag returns enhanced citation with source_kind and path for notes."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    assert len(result["sources"]) >= 1
    note_source = result["sources"][0]
    assert note_source["source_kind"] == "note"
    assert note_source["path"] == "projects/project-a/scripts/restlet/order-sync.md"


def test_ask_netsuite_rag_note_source_includes_heading_chunk_index_updated_at(tmp_path: Path):
    """ask_netsuite_rag note source includes heading, chunk_index, updated_at."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    note_source = result["sources"][0]
    assert note_source["source_kind"] == "note"
    assert note_source["heading"] == "相关脚本"
    assert note_source["chunk_index"] == 2
    assert note_source["updated_at"] == "2026-05-14T10:20:00Z"


def test_ask_netsuite_rag_code_source_has_function_line_git_commit(tmp_path: Path):
    """ask_netsuite_rag returns code citation with function, line, git_commit."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="code",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    assert len(result["sources"]) >= 1
    code_source = result["sources"][0]
    assert code_source["source_kind"] == "code"
    assert code_source["path"] == "project-a/src/restlets/order-sync-restlet.js"
    assert code_source["function"] == "post"
    assert code_source["line"] == "42-55"
    assert code_source["git_commit"] == "abc1234"


def test_ask_netsuite_rag_dirty_code_has_dirty_git_commit_and_file_hash(tmp_path: Path):
    """ask_netsuite_rag dirty code citation includes git_commit+dirty and file_hash."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="提交后逻辑",
        vault_root=tmp_path,
        source_kind="code",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    assert len(result["sources"]) >= 1
    # Find the dirty code source
    dirty_sources = [s for s in result["sources"] if "after-submit" in s["path"]]
    assert len(dirty_sources) >= 1
    dirty_source = dirty_sources[0]
    assert dirty_source["git_commit"] == "abc1234+dirty"
    assert dirty_source["file_hash"] == "sha256:deadbeef"


def test_ask_netsuite_rag_source_has_formatted_citation_string(tmp_path: Path):
    """ask_netsuite_rag returns sources with a 'formatted' citation string."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    note_source = result["sources"][0]
    assert "formatted" in note_source
    formatted = note_source["formatted"]
    assert formatted.startswith("source_kind=note")
    assert "path=projects/project-a/scripts/restlet/order-sync.md" in formatted


def test_ask_netsuite_rag_code_source_formatted_citation(tmp_path: Path):
    """ask_netsuite_rag code source has correctly formatted citation string."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="code",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    code_source = result["sources"][0]
    formatted = code_source["formatted"]
    assert "source_kind=code" in formatted
    assert "function=post" in formatted
    assert "line=42-55" in formatted
    assert "git_commit=abc1234" in formatted


def test_ask_netsuite_rag_backward_compat_source_path_exists(tmp_path: Path):
    """ask_netsuite_rag still returns source_path for backward compatibility."""
    store = _make_store_with_note_and_code(tmp_path)
    result = ask_netsuite_rag(
        question="订单同步",
        vault_root=tmp_path,
        source_kind="note",
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    note_source = result["sources"][0]
    # source_path should still be present for backward compat
    assert "source_path" in note_source
    assert note_source["source_path"] == "projects/project-a/scripts/restlet/order-sync.md"


def test_ask_netsuite_rag_defaults_source_kind_to_note(tmp_path: Path):
    """When source_kind is empty string (from ChromaDB default), default to 'note'."""
    _write_sources_config(tmp_path)
    store = ChromaVectorStore(tmp_path / "chroma2", "test_citation_default", FakeEmbedder())
    old_style_chunk = Chunk(
        id="old_doc:0",
        doc_id="old_doc",
        chunk_index=0,
        source_path="old-note.md",
        heading="Legacy note",
        text="Old data without source_kind.",
        metadata={
            "doc_id": "old_doc",
            "chunk_index": 0,
            "source_path": "old-note.md",
            "heading": "Legacy note",
            "type": "script",
            "project": "project-a",
            "source_name": "obsidian",
            # source_kind will default to "" via from_chroma_metadata
        },
        source_kind="note",
        source_name="obsidian",
    )
    store.upsert_chunks([old_style_chunk])

    result = ask_netsuite_rag(
        question="old data",
        vault_root=tmp_path,
        top_k=10,
        embedder=FakeEmbedder(),
        store=store,
    )
    assert len(result["sources"]) >= 1
    source = result["sources"][0]
    # from_chroma_metadata defaults source_kind to "" when absent;
    # our _build_citation_dict should treat "" as "note"
    assert source["source_kind"] == "note"


def test_ask_netsuite_rag_dirty_code_git_commit_formatting(tmp_path: Path):
    """Test that git_commit is formatted with +dirty suffix when file_hash is present."""
    citation = {
        "source_kind": "code",
        "path": "test.js",
        "function": "main",
        "line": "1-10",
        "git_commit": "abc1234+dirty",
        "file_hash": "sha256:deadbeef",
    }
    result = _format_citation(citation)
    assert "git_commit=abc1234+dirty" in result
    assert "file_hash=sha256:deadbeef" in result
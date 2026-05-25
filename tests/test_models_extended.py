"""Tests for new SourceConfig model and extended data model fields."""

from pathlib import Path

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.models import (
    ARRAY_METADATA_FIELDS,
    Chunk,
    RagConfig,
    SearchResult,
    SourceConfig,
    SourceDocument,
)


# --- SourceConfig ---


def test_source_config_is_frozen():
    """SourceConfig must be immutable (frozen dataclass)."""
    cfg = SourceConfig(
        source_name="obsidian",
        source_kind="note",
        root=Path("/tmp/vault"),
        include=["projects"],
        exclude=[".git"],
        file_types=["md"],
        parser="markdown_frontmatter_h2",
        collection="netsuite_notes",
        authority="curated_note_source",
    )
    import dataclasses

    assert dataclasses.is_dataclass(cfg)
    assert cfg.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_source_config_stores_all_fields():
    """All SourceConfig fields round-trip correctly."""
    root = Path("/tmp/vault")
    cfg = SourceConfig(
        source_name="netsuite_repo",
        source_kind="code",
        root=root,
        include=["SuiteScripts"],
        exclude=[".git", "node_modules"],
        file_types=["js", "ts", "xml", "json"],
        parser="suitescript_code_and_config",
        collection="netsuite_code",
        authority="implementation_source_of_truth",
        file_exclude_patterns=["src/deploy.xml"],
        library_exclude_patterns=["src/FileCabinet/SuiteScripts/tools/moment.js"],
        utility_allowlist=["src/FileCabinet/SuiteScripts/tools/common_api.js"],
    )

    assert cfg.source_name == "netsuite_repo"
    assert cfg.source_kind == "code"
    assert cfg.root == root
    assert cfg.include == ["SuiteScripts"]
    assert cfg.exclude == [".git", "node_modules"]
    assert cfg.file_types == ["js", "ts", "xml", "json"]
    assert cfg.parser == "suitescript_code_and_config"
    assert cfg.collection == "netsuite_code"
    assert cfg.authority == "implementation_source_of_truth"
    assert cfg.file_exclude_patterns == ["src/deploy.xml"]
    assert cfg.library_exclude_patterns == ["src/FileCabinet/SuiteScripts/tools/moment.js"]
    assert cfg.utility_allowlist == ["src/FileCabinet/SuiteScripts/tools/common_api.js"]


# --- Extended SourceDocument ---


def test_source_document_new_fields_have_defaults():
    """New SourceDocument fields default correctly for backward compat."""
    doc = SourceDocument(
        doc_id="a" * 40,
        source_path="projects/test.md",
        absolute_path=Path("/tmp/vault/projects/test.md"),
        frontmatter={"type": "script"},
        body="test body",
        updated_at="2026-05-15",
    )

    assert doc.source_kind == "note"
    assert doc.source_name == ""
    assert doc.file_hash == ""
    assert doc.repo_root == ""
    assert doc.repo_relative_path == ""
    assert doc.language == ""


def test_source_document_new_fields_can_be_set():
    """New SourceDocument fields accept explicit values."""
    doc = SourceDocument(
        doc_id="b" * 40,
        source_path="SuiteScripts/order-sync.ts",
        absolute_path=Path("/src/SuiteScripts/order-sync.ts"),
        frontmatter={},
        body="// code",
        updated_at="2026-05-15",
        source_kind="code",
        source_name="netsuite_repo",
        file_hash="abc123def456",
        repo_root="/src",
        repo_relative_path="SuiteScripts/order-sync.ts",
        language="typescript",
    )

    assert doc.source_kind == "code"
    assert doc.source_name == "netsuite_repo"
    assert doc.file_hash == "abc123def456"
    assert doc.repo_root == "/src"
    assert doc.repo_relative_path == "SuiteScripts/order-sync.ts"
    assert doc.language == "typescript"


# --- Extended Chunk ---


def test_chunk_new_fields_have_defaults():
    """New Chunk fields default correctly for backward compat."""
    c = Chunk(
        id="doc1:0",
        doc_id="doc1",
        chunk_index=0,
        source_path="projects/test.md",
        heading="Heading",
        text="body text",
        metadata={},
    )

    assert c.function_name == ""
    assert c.line_start == 0
    assert c.line_end == 0
    assert c.source_kind == "note"
    assert c.source_name == ""
    assert c.file_hash == ""


def test_chunk_new_fields_can_be_set():
    """New Chunk fields accept explicit values."""
    c = Chunk(
        id="doc1:0",
        doc_id="doc1",
        chunk_index=0,
        source_path="SuiteScripts/order-sync.ts",
        heading="function orderSync",
        text="// impl",
        metadata={},
        function_name="orderSync",
        line_start=10,
        line_end=45,
        source_kind="code",
        source_name="netsuite_repo",
        file_hash="deadbeef",
    )

    assert c.function_name == "orderSync"
    assert c.line_start == 10
    assert c.line_end == 45
    assert c.source_kind == "code"
    assert c.source_name == "netsuite_repo"
    assert c.file_hash == "deadbeef"


# --- Extended SearchResult ---


def test_search_result_new_fields_have_defaults():
    """New SearchResult fields default correctly for backward compat."""
    sr = SearchResult(
        citation_id="cit1",
        chunk_id="doc1:0",
        text="body",
        metadata={},
        distance=0.42,
    )

    assert sr.source_kind == "note"
    assert sr.git_commit == ""


def test_search_result_new_fields_can_be_set():
    """New SearchResult fields accept explicit values."""
    sr = SearchResult(
        citation_id="cit1",
        chunk_id="doc1:0",
        text="impl",
        metadata={"source_kind": "code"},
        distance=0.1,
        source_kind="code",
        git_commit="a1b2c3d4",
    )

    assert sr.source_kind == "code"
    assert sr.git_commit == "a1b2c3d4"


# --- ARRAY_METADATA_FIELDS ---


def test_array_metadata_fields_includes_expected_fields():
    """ARRAY_METADATA_FIELDS must include existing array fields."""
    assert "related_objects" in ARRAY_METADATA_FIELDS
    assert "related_scripts" in ARRAY_METADATA_FIELDS
    assert "related_deployments" in ARRAY_METADATA_FIELDS
    assert "deployment_ids" in ARRAY_METADATA_FIELDS
    assert "script_parameters" in ARRAY_METADATA_FIELDS
    assert "field_ids" in ARRAY_METADATA_FIELDS
    assert "config_paths" in ARRAY_METADATA_FIELDS
    assert "tags" in ARRAY_METADATA_FIELDS
    assert "zentao_urls" in ARRAY_METADATA_FIELDS


def test_array_metadata_fields_does_not_include_source_kind_or_source_name():
    """source_kind and source_name are scalar fields, not array."""
    assert "source_kind" not in ARRAY_METADATA_FIELDS
    assert "source_name" not in ARRAY_METADATA_FIELDS


# --- RagConfig with sources field ---


def test_rag_config_has_sources_field_with_default():
    """RagConfig must have a sources field defaulting to empty list."""
    cfg = RagConfig(
        vault_root=Path("/tmp/vault"),
        include_paths=[Path("/tmp/vault/projects")],
        exclude_names={".git"},
        chroma_path=Path("/tmp/vault/.rag-index/chroma"),
        collection_name="netsuite_notes",
        embedding_model="BAAI/bge-m3",
        embedding_cache_path=Path("/tmp/vault/.models"),
        manifest_path=Path("/tmp/vault/.rag-index/index-manifest.json"),
    )

    assert cfg.sources == []


def test_rag_config_sources_can_be_populated():
    """RagConfig.sources can hold SourceConfig entries."""
    src = SourceConfig(
        source_name="obsidian",
        source_kind="note",
        root=Path("/tmp/vault"),
        include=["projects"],
        exclude=[".git"],
        file_types=["md"],
        parser="markdown_frontmatter_h2",
        collection="netsuite_notes",
        authority="curated_note_source",
    )
    cfg = RagConfig(
        vault_root=Path("/tmp/vault"),
        include_paths=[Path("/tmp/vault/projects")],
        exclude_names={".git"},
        chroma_path=Path("/tmp/vault/.rag-index/chroma"),
        collection_name="netsuite_notes",
        embedding_model="BAAI/bge-m3",
        embedding_cache_path=Path("/tmp/vault/.models"),
        manifest_path=Path("/tmp/vault/.rag-index/index-manifest.json"),
        sources=[src],
    )

    assert len(cfg.sources) == 1
    assert cfg.sources[0].source_name == "obsidian"
"""Tests for manifest module: ManifestEntry, compute_file_hash, read/write, v1→v2 migration."""

import json
from pathlib import Path

from netsuite_rag_mcp.manifest import (
    ManifestEntry,
    compute_file_hash,
    manifest_key,
    migrate_manifest,
    read_manifest,
    write_manifest,
)


# --- ManifestEntry dataclass ---


def test_manifest_entry_required_fields():
    """ManifestEntry must store all required fields."""
    entry = ManifestEntry(
        doc_id="a" * 40,
        source_name="obsidian",
        source_kind="note",
        relative_path="projects/test.md",
        mtime=1700000000.0,
        size=1024,
        file_hash="sha256hexdigest123",
        chunk_count=3,
        indexed_at="2026-05-15T00:00:00+00:00",
    )
    assert entry.doc_id == "a" * 40
    assert entry.source_name == "obsidian"
    assert entry.source_kind == "note"
    assert entry.relative_path == "projects/test.md"
    assert entry.mtime == 1700000000.0
    assert entry.size == 1024
    assert entry.file_hash == "sha256hexdigest123"
    assert entry.chunk_count == 3
    assert entry.indexed_at == "2026-05-15T00:00:00+00:00"


def test_manifest_entry_code_fields_default_empty():
    """Git fields default to empty/False for note entries."""
    entry = ManifestEntry(
        doc_id="a" * 40,
        source_name="obsidian",
        source_kind="note",
        relative_path="projects/test.md",
        mtime=1700000000.0,
        size=1024,
        file_hash="sha256hex",
        chunk_count=1,
        indexed_at="2026-05-15T00:00:00+00:00",
    )
    assert entry.git_commit == ""
    assert entry.git_branch == ""
    assert entry.dirty is False


def test_manifest_entry_code_fields_can_be_set():
    """Git fields can be set for code entries."""
    entry = ManifestEntry(
        doc_id="b" * 40,
        source_name="netsuite_repo",
        source_kind="code",
        relative_path="SuiteScripts/order-sync.ts",
        mtime=1700000000.0,
        size=2048,
        file_hash="abc456def",
        chunk_count=5,
        indexed_at="2026-05-15T00:00:00+00:00",
        git_commit="a1b2c3d4",
        git_branch="main",
        dirty=True,
    )
    assert entry.git_commit == "a1b2c3d4"
    assert entry.git_branch == "main"
    assert entry.dirty is True


def test_manifest_entry_is_mutable():
    """ManifestEntry should NOT be frozen — indexer needs to update fields."""
    entry = ManifestEntry(
        doc_id="a" * 40,
        source_name="obsidian",
        source_kind="note",
        relative_path="projects/test.md",
        mtime=1700000000.0,
        size=1024,
        file_hash="sha256hex",
        chunk_count=1,
        indexed_at="2026-05-15T00:00:00+00:00",
    )
    entry.chunk_count = 5
    entry.mtime = 1700000001.0
    assert entry.chunk_count == 5
    assert entry.mtime == 1700000001.0


# --- manifest_key ---


def test_manifest_key_format():
    """manifest_key generates {source_name}:{source_kind}:{relative_path}."""
    key = manifest_key("obsidian", "note", "projects/test.md")
    assert key == "obsidian:note:projects/test.md"


def test_manifest_key_code_source():
    """manifest_key works for code sources too."""
    key = manifest_key("netsuite_repo", "code", "SuiteScripts/order-sync.ts")
    assert key == "netsuite_repo:code:SuiteScripts/order-sync.ts"


# --- compute_file_hash ---


def test_compute_file_hash_sha256(tmp_path: Path):
    """compute_file_hash returns SHA-256 hex digest of file content."""
    test_file = tmp_path / "test.md"
    test_file.write_text("hello world", encoding="utf-8")

    result = compute_file_hash(test_file)
    # SHA-256 of "hello world" is a known 64-char hex string
    assert isinstance(result, str)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_compute_file_hash_deterministic(tmp_path: Path):
    """Same content produces same hash."""
    test_file = tmp_path / "test.md"
    test_file.write_text("deterministic content", encoding="utf-8")

    hash1 = compute_file_hash(test_file)
    hash2 = compute_file_hash(test_file)
    assert hash1 == hash2


def test_compute_file_hash_different_content(tmp_path: Path):
    """Different content produces different hash."""
    file_a = tmp_path / "a.md"
    file_b = tmp_path / "b.md"
    file_a.write_text("content A", encoding="utf-8")
    file_b.write_text("content B", encoding="utf-8")

    assert compute_file_hash(file_a) != compute_file_hash(file_b)


# --- read_manifest / write_manifest ---


def test_write_and_read_manifest_round_trip(tmp_path: Path):
    """Writing then reading a manifest preserves all entries."""
    manifest_path = tmp_path / "index-manifest.json"

    original = {
        "obsidian:note:projects/test.md": ManifestEntry(
            doc_id="a" * 40,
            source_name="obsidian",
            source_kind="note",
            relative_path="projects/test.md",
            mtime=1700000000.0,
            size=1024,
            file_hash="sha256hex123",
            chunk_count=3,
            indexed_at="2026-05-15T00:00:00+00:00",
        ),
        "netsuite_repo:code:SuiteScripts/order-sync.ts": ManifestEntry(
            doc_id="b" * 40,
            source_name="netsuite_repo",
            source_kind="code",
            relative_path="SuiteScripts/order-sync.ts",
            mtime=1700000001.0,
            size=2048,
            file_hash="sha256hex456",
            chunk_count=5,
            indexed_at="2026-05-15T01:00:00+00:00",
            git_commit="a1b2c3d4",
            git_branch="main",
            dirty=True,
        ),
    }

    write_manifest(manifest_path, original)
    loaded = read_manifest(manifest_path)

    assert len(loaded) == 2

    note_entry = loaded["obsidian:note:projects/test.md"]
    assert note_entry.doc_id == "a" * 40
    assert note_entry.source_name == "obsidian"
    assert note_entry.source_kind == "note"
    assert note_entry.relative_path == "projects/test.md"
    assert note_entry.mtime == 1700000000.0
    assert note_entry.size == 1024
    assert note_entry.file_hash == "sha256hex123"
    assert note_entry.chunk_count == 3
    assert note_entry.indexed_at == "2026-05-15T00:00:00+00:00"
    assert note_entry.git_commit == ""
    assert note_entry.git_branch == ""
    assert note_entry.dirty is False

    code_entry = loaded["netsuite_repo:code:SuiteScripts/order-sync.ts"]
    assert code_entry.doc_id == "b" * 40
    assert code_entry.git_commit == "a1b2c3d4"
    assert code_entry.git_branch == "main"
    assert code_entry.dirty is True


def test_read_manifest_missing_file_returns_empty(tmp_path: Path):
    """Reading a non-existent manifest returns empty dict."""
    manifest_path = tmp_path / "nonexistent.json"
    result = read_manifest(manifest_path)
    assert result == {}


def test_read_manifest_invalid_json_returns_empty(tmp_path: Path):
    """Reading an invalid JSON manifest returns empty dict."""
    manifest_path = tmp_path / "bad.json"
    manifest_path.write_text("not valid json {{{", encoding="utf-8")
    result = read_manifest(manifest_path)
    assert result == {}


# --- v1 → v2 migration ---


def test_migrate_manifest_converts_v1_keys():
    """v1 manifest (relative_path keys) → v2 ({source_name}:{source_kind}:{relative_path})."""
    v1_manifest = {
        "projects/test.md": {
            "doc_id": "a" * 40,
            "mtime": 1700000000.0,
            "chunk_count": 3,
            "indexed_at": "2026-05-15T00:00:00+00:00",
        },
        "knowledge/netsuite-basics.md": {
            "doc_id": "b" * 40,
            "mtime": 1700000001.0,
            "chunk_count": 2,
            "indexed_at": "2026-05-15T01:00:00+00:00",
        },
    }

    v2 = migrate_manifest(v1_manifest)

    assert len(v2) == 2
    # v1 entries default to source_name='obsidian', source_kind='note'
    key1 = "obsidian:note:projects/test.md"
    key2 = "obsidian:note:knowledge/netsuite-basics.md"
    assert key1 in v2
    assert key2 in v2

    entry1 = v2[key1]
    assert entry1.source_name == "obsidian"
    assert entry1.source_kind == "note"
    assert entry1.relative_path == "projects/test.md"
    assert entry1.doc_id == "a" * 40
    assert entry1.mtime == 1700000000.0
    assert entry1.chunk_count == 3
    assert entry1.indexed_at == "2026-05-15T00:00:00+00:00"
    # Migrated entries have no file_hash/size → defaults
    assert entry1.file_hash == ""
    assert entry1.size == 0


def test_migrate_manifest_preserves_v2_keys():
    """v2 manifest entries (with ':' in key) pass through unchanged."""
    v2_manifest = {
        "obsidian:note:projects/test.md": {
            "doc_id": "a" * 40,
            "source_name": "obsidian",
            "source_kind": "note",
            "relative_path": "projects/test.md",
            "mtime": 1700000000.0,
            "size": 1024,
            "file_hash": "sha256hex",
            "chunk_count": 3,
            "indexed_at": "2026-05-15T00:00:00+00:00",
        },
    }

    result = migrate_manifest(v2_manifest)
    # v2 entries pass through unchanged (already have proper keys)
    key = "obsidian:note:projects/test.md"
    assert key in result
    assert result[key].file_hash == "sha256hex"
    assert result[key].size == 1024


def test_migrate_manifest_empty_input():
    """Migrating empty manifest returns empty dict."""
    result = migrate_manifest({})
    assert result == {}


# --- Integration: read_manifest auto-migrates v1 ---


def test_read_manifest_auto_migrates_v1(tmp_path: Path):
    """read_manifest auto-detects and migrates v1 manifests."""
    manifest_path = tmp_path / "index-manifest.json"
    v1_content = {
        "projects/test.md": {
            "doc_id": "a" * 40,
            "mtime": 1700000000.0,
            "chunk_count": 3,
            "indexed_at": "2026-05-15T00:00:00+00:00",
        },
    }
    manifest_path.write_text(json.dumps(v1_content, ensure_ascii=False), encoding="utf-8")

    result = read_manifest(manifest_path)

    assert len(result) == 1
    key = "obsidian:note:projects/test.md"
    assert key in result
    assert result[key].source_name == "obsidian"
    assert result[key].source_kind == "note"
    assert result[key].relative_path == "projects/test.md"


# --- Integration: indexer writes v2 manifest ---


def test_index_vault_writes_v2_manifest(tmp_path: Path):
    """index_vault should write a v2 manifest with file_hash, size, source_name, source_kind."""
    from netsuite_rag_mcp.config import load_config
    from netsuite_rag_mcp.manifest import read_manifest

    vault = tmp_path / "vault"
    vault.mkdir()
    # Create sources.yaml config
    (vault / "rag").mkdir(parents=True)
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

    # Create a markdown file
    note_dir = vault / "projects" / "test-notes"
    note_dir.mkdir(parents=True)
    (note_dir / "sample.md").write_text(
        "---\ntype: note\n---\n\n# Sample\n\nHello world.\n",
        encoding="utf-8",
    )

    from netsuite_rag_mcp.indexer import index_vault
    from netsuite_rag_mcp.vector_store import FakeEmbedder

    result = index_vault(vault, mode="full", embedder=FakeEmbedder())
    assert result["indexed_files"] == 1

    # Read the manifest file directly
    manifest_path = load_config(vault).manifest_path
    entries = read_manifest(manifest_path)

    # Should have a v2 key
    assert len(entries) == 1
    key = list(entries.keys())[0]
    assert key == "obsidian:note:projects/test-notes/sample.md"

    entry = entries[key]
    assert entry.source_name == "obsidian"
    assert entry.source_kind == "note"
    assert entry.relative_path == "projects/test-notes/sample.md"
    assert entry.size > 0
    assert len(entry.file_hash) == 64  # SHA-256 hex digest
    assert entry.chunk_count >= 1
    assert entry.git_commit == ""
    assert entry.git_branch == ""
    assert entry.dirty is False


def test_incremental_mode_skips_unchanged_file_via_hash(tmp_path: Path):
    """Incremental mode should skip files whose mtime and file_hash are unchanged."""
    from netsuite_rag_mcp.manifest import read_manifest

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir(parents=True)
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

    # Create a markdown file
    note_dir = vault / "projects" / "test-notes"
    note_dir.mkdir(parents=True)
    (note_dir / "sample.md").write_text(
        "---\ntype: note\n---\n\n# Sample\n\nHello world.\n",
        encoding="utf-8",
    )

    from netsuite_rag_mcp.indexer import index_vault
    from netsuite_rag_mcp.vector_store import FakeEmbedder

    # First full index
    result1 = index_vault(vault, mode="full", embedder=FakeEmbedder())
    assert result1["indexed_files"] == 1
    assert result1["skipped_files"] == 0

    # Incremental index on same content — should be skipped
    result2 = index_vault(vault, mode="incremental", embedder=FakeEmbedder())
    assert result2["skipped_files"] == 1
    assert result2["indexed_files"] == 0
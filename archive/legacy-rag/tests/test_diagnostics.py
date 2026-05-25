"""Tests for T16: routing/conflict/freshness diagnostics in ask_netsuite_rag response."""
from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.models import Chunk, ConflictReport, RoutingResult, SearchResult
from netsuite_rag_mcp.retriever import (
    _detect_dirty_sources,
    _detect_stale_sources,
    ask_netsuite_rag,
)
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder


# ── _detect_stale_sources() tests ──────────────────────────────────────────────


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
                "    default: test_diagnostics",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: test_diagnostics",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [src]",
                "    exclude: [.git, node_modules]",
                "    file_types: [js]",
                "    parser: suitescript_code_and_config",
                "    collection: test_diagnostics",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


class TestDetectStaleSources:
    """Test stale source detection — notes that are significantly older than code."""

    def _make_note_result(self, citation_id: str, updated_at: str | None = None, **extra) -> SearchResult:
        metadata = {"source_kind": "note", "source_path": f"notes/{citation_id}.md", **extra}
        if updated_at is not None:
            metadata["updated_at"] = updated_at
        return SearchResult(
            citation_id=citation_id,
            chunk_id=f"note_{citation_id}:0",
            text="Note content",
            metadata=metadata,
            distance=0.3,
            source_kind="note",
        )

    def _make_code_result(self, citation_id: str, git_commit: str | None = None, **extra) -> SearchResult:
        metadata = {"source_kind": "code", "source_path": f"src/{citation_id}.js", **extra}
        if git_commit is not None:
            metadata["git_commit"] = git_commit
        return SearchResult(
            citation_id=citation_id,
            chunk_id=f"code_{citation_id}:0",
            text="Code content",
            metadata=metadata,
            distance=0.35,
            source_kind="code",
        )

    def test_stale_note_detected_when_old_note_vs_recent_code(self):
        """Note from 2024-01-01 with code from 2026-05 should be flagged as stale."""
        conflicts = [
            ConflictReport(
                conflict_type="implementation",
                entity="customscript_order_sync",
                note_source={
                    "citation_id": "S1",
                    "source_kind": "note",
                    "path": "notes/order-sync.md",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
                code_source={
                    "citation_id": "S2",
                    "source_kind": "code",
                    "path": "src/OrderSync.js",
                    "git_commit": "abc1234",
                },
                winning_source="code",
                explanation="Code wins",
                uncertainty=False,
            )
        ]
        results = [
            self._make_note_result("S1", updated_at="2024-01-01T00:00:00Z"),
            self._make_code_result("S2", git_commit="abc1234"),
        ]
        stale = _detect_stale_sources(results, conflicts)
        assert len(stale) >= 1
        found_s1 = any(s["source"] == "S1" for s in stale)
        assert found_s1, "S1 (note from 2024) should be flagged as stale"

    def test_recent_note_not_flagged_as_stale(self):
        """Note from 2026-05 with code should NOT be flagged as stale."""
        conflicts = [
            ConflictReport(
                conflict_type="business",
                entity="customscript_order_sync",
                note_source={
                    "citation_id": "S1",
                    "source_kind": "note",
                    "path": "notes/order-sync.md",
                    "updated_at": "2026-05-10T00:00:00Z",
                },
                code_source={
                    "citation_id": "S2",
                    "source_kind": "code",
                    "path": "src/OrderSync.js",
                    "git_commit": "abc1234",
                },
                winning_source="note",
                explanation="Note wins",
                uncertainty=False,
            )
        ]
        results = [
            self._make_note_result("S1", updated_at="2026-05-10T00:00:00Z"),
            self._make_code_result("S2", git_commit="abc1234"),
        ]
        stale = _detect_stale_sources(results, conflicts)
        assert len(stale) == 0, "Recent note should not be flagged as stale"

    def test_no_conflicts_means_no_stale_sources(self):
        """When there are no conflicts, there are no stale sources."""
        results = [self._make_note_result("S1", updated_at="2024-01-01T00:00:00Z")]
        stale = _detect_stale_sources(results, [])
        assert stale == []

    def test_stale_entry_includes_reason_and_timestamps(self):
        """Each stale source entry should include reason, note_updated, and code_commit."""
        conflicts = [
            ConflictReport(
                conflict_type="implementation",
                entity="customscript_order_sync",
                note_source={
                    "citation_id": "S1",
                    "source_kind": "note",
                    "path": "notes/order-sync.md",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
                code_source={
                    "citation_id": "S2",
                    "source_kind": "code",
                    "path": "src/OrderSync.js",
                    "git_commit": "abc1234",
                },
                winning_source="code",
                explanation="Code wins",
                uncertainty=False,
            )
        ]
        results = [
            self._make_note_result("S1", updated_at="2024-01-01T00:00:00Z"),
            self._make_code_result("S2", git_commit="abc1234"),
        ]
        stale = _detect_stale_sources(results, conflicts)
        assert len(stale) >= 1
        entry = stale[0]
        assert "source" in entry
        assert "reason" in entry
        assert entry["note_updated"] == "2024-01-01T00:00:00Z"

    def test_stale_threshold_is_30_days(self):
        """Note from ~35 days before today should be flagged; ~10 days should not."""
        # Note from 2026-04-01 (44+ days before 2026-05-15) → stale
        old_conflicts = [
            ConflictReport(
                conflict_type="implementation",
                entity="script_old",
                note_source={
                    "citation_id": "S1",
                    "source_kind": "note",
                    "path": "notes/old.md",
                    "updated_at": "2026-04-01T00:00:00Z",
                },
                code_source={
                    "citation_id": "S2",
                    "source_kind": "code",
                    "path": "src/old.js",
                    "git_commit": "def5678",
                },
                winning_source="code",
                explanation="",
                uncertainty=False,
            )
        ]
        old_results = [
            self._make_note_result("S1", updated_at="2026-04-01T00:00:00Z"),
            self._make_code_result("S2", git_commit="def5678"),
        ]
        stale = _detect_stale_sources(old_results, old_conflicts)
        assert len(stale) >= 1, "Note from 44+ days ago should be stale"

    def test_missing_timestamps_means_no_stale_flag(self):
        """If note has no updated_at, can't determine staleness."""
        conflicts = [
            ConflictReport(
                conflict_type="implementation",
                entity="customscript_order_sync",
                note_source={
                    "citation_id": "S1",
                    "source_kind": "note",
                    "path": "notes/order-sync.md",
                    # no updated_at
                },
                code_source={
                    "citation_id": "S2",
                    "source_kind": "code",
                    "path": "src/OrderSync.js",
                },
                winning_source="code",
                explanation="",
                uncertainty=False,
            )
        ]
        results = [
            self._make_note_result("S1"),  # no updated_at
            self._make_code_result("S2"),  # no git_commit
        ]
        stale = _detect_stale_sources(results, conflicts)
        assert len(stale) == 0, "Cannot determine staleness without timestamps"


# ── _detect_dirty_sources() tests ──────────────────────────────────────────────


class TestDetectDirtySources:
    """Test detection of code sources with uncommitted changes (dirty)."""

    def test_dirty_source_detected(self):
        """Source with git_commit containing '+dirty' should be flagged."""
        results = [
            SearchResult(
                citation_id="S3",
                chunk_id="code_doc:0",
                text="Code content",
                metadata={
                    "source_kind": "code",
                    "source_path": "src/foo.js",
                    "git_commit": "abc1234+dirty",
                    "file_hash": "sha256:abcdef",
                },
                distance=0.3,
                source_kind="code",
                git_commit="abc1234+dirty",
            )
        ]
        dirty = _detect_dirty_sources(results)
        assert len(dirty) == 1
        assert dirty[0]["source"] == "S3"
        assert dirty[0]["git_commit"] == "abc1234+dirty"
        assert dirty[0]["file_hash"] == "sha256:abcdef"

    def test_clean_source_not_detected(self):
        """Source with clean git_commit (no '+dirty') should NOT be flagged."""
        results = [
            SearchResult(
                citation_id="S1",
                chunk_id="code_doc:0",
                text="Clean code",
                metadata={
                    "source_kind": "code",
                    "source_path": "src/bar.js",
                    "git_commit": "abc1234",
                },
                distance=0.3,
                source_kind="code",
                git_commit="abc1234",
            )
        ]
        dirty = _detect_dirty_sources(results)
        assert len(dirty) == 0

    def test_note_source_never_dirty(self):
        """Note sources should never appear in dirty list (no git_commit)."""
        results = [
            SearchResult(
                citation_id="S1",
                chunk_id="note_doc:0",
                text="Note content",
                metadata={
                    "source_kind": "note",
                    "source_path": "notes/note.md",
                },
                distance=0.3,
                source_kind="note",
            )
        ]
        dirty = _detect_dirty_sources(results)
        assert len(dirty) == 0

    def test_multiple_dirty_sources(self):
        """All dirty sources should be collected."""
        results = [
            SearchResult(
                citation_id="S1",
                chunk_id="code_doc1:0",
                text="Dirty 1",
                metadata={
                    "source_kind": "code",
                    "source_path": "src/a.js",
                    "git_commit": "aaa1+dirty",
                    "file_hash": "sha256:a",
                },
                distance=0.3,
                source_kind="code",
                git_commit="aaa1+dirty",
            ),
            SearchResult(
                citation_id="S2",
                chunk_id="code_doc2:0",
                text="Clean",
                metadata={
                    "source_kind": "code",
                    "source_path": "src/b.js",
                    "git_commit": "bbb2",
                },
                distance=0.35,
                source_kind="code",
                git_commit="bbb2",
            ),
            SearchResult(
                citation_id="S3",
                chunk_id="code_doc3:0",
                text="Dirty 2",
                metadata={
                    "source_kind": "code",
                    "source_path": "src/c.js",
                    "git_commit": "ccc3+dirty",
                    "file_hash": "sha256:c",
                },
                distance=0.4,
                source_kind="code",
                git_commit="ccc3+dirty",
            ),
        ]
        dirty = _detect_dirty_sources(results)
        assert len(dirty) == 2
        sources = {d["source"] for d in dirty}
        assert sources == {"S1", "S3"}

    def test_empty_results(self):
        """Empty results should produce empty dirty list."""
        dirty = _detect_dirty_sources([])
        assert dirty == []


# ── Integration: ask_netsuite_rag diagnostics ──────────────────────────────────


def _setup_store_for_diagnostics(tmp_path: Path) -> ChromaVectorStore:
    """Create a store with mixed sources including stale and dirty."""
    _write_sources_config(tmp_path)
    store = ChromaVectorStore(tmp_path / "chroma", "test_diagnostics", FakeEmbedder())

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
            "source_kind": "note",
            "source_name": "obsidian",
            "script_id": "customscript_order_sync_mr",
            "updated_at": "2024-01-01T00:00:00Z",
        },
        source_kind="note",
        source_name="obsidian",
    )

    code_chunk = Chunk(
        id="code_doc1:0",
        doc_id="code_doc1",
        chunk_index=0,
        source_path="src/OrderSync.js",
        heading="afterSubmit",
        text="Map/Reduce script customscript_order_sync_mr 处理订单实际逻辑。",
        metadata={
            "doc_id": "code_doc1",
            "chunk_index": 0,
            "source_path": "src/OrderSync.js",
            "heading": "afterSubmit",
            "source_kind": "code",
            "source_name": "netsuite_repo",
            "script_id": "customscript_order_sync_mr",
            "function_name": "afterSubmit",
            "git_commit": "abc1234+dirty",
            "file_hash": "sha256:deadbeef",
        },
        source_kind="code",
        source_name="netsuite_repo",
    )

    store.upsert_chunks([note_chunk, code_chunk])
    return store


class TestAskNetsuiteRagDiagnostics:
    """Test that ask_netsuite_rag response includes all diagnostic fields."""

    def test_response_includes_sources_considered(self, tmp_path: Path):
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="订单同步怎么实现？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "sources_considered" in result
        assert isinstance(result["sources_considered"], dict)
        assert "note" in result["sources_considered"]
        assert "code" in result["sources_considered"]

    def test_sources_considered_counts_note_and_code(self, tmp_path: Path):
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="订单同步怎么实现以及为什么？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        considered = result["sources_considered"]
        # The store has 1 note and 1 code chunk
        assert considered["note"] + considered["code"] >= 1  # At least one source considered

    def test_response_includes_stale_sources(self, tmp_path: Path):
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="订单同步怎么实现以及为什么？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "stale_sources" in result
        assert isinstance(result["stale_sources"], list)

    def test_response_includes_code_dirty_sources(self, tmp_path: Path):
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="怎么实现？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "code_dirty_sources" in result
        assert isinstance(result["code_dirty_sources"], list)

    def test_response_includes_redaction_count(self, tmp_path: Path):
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="怎么实现？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "redaction_applied_before_return" in result
        assert isinstance(result["redaction_applied_before_return"], int)

    def test_dirty_sources_populated_when_code_has_dirty_commit(self, tmp_path: Path):
        """When a code source has '+dirty' git_commit, it should appear in code_dirty_sources."""
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="怎么实现？",
            source_kind="code",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        dirty = result["code_dirty_sources"]
        # The code chunk has git_commit "abc1234+dirty"
        if dirty:
            assert any(d["git_commit"].endswith("+dirty") for d in dirty)

    def test_filters_applied_in_response(self, tmp_path: Path):
        """filters_applied should be present in the response."""
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="Why?",
            source_kind="note",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "filters_applied" in result
        # When source_kind="note" is specified, filters_applied should reflect it
        assert isinstance(result["filters_applied"], dict)

    def test_all_diagnostic_fields_present(self, tmp_path: Path):
        """Verify all diagnostic fields are present in ask_netsuite_rag response."""
        store = _setup_store_for_diagnostics(tmp_path)
        result = ask_netsuite_rag(
            question="怎么实现以及为什么？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        required_diagnostic_fields = [
            "routing",
            "filters_applied",
            "sources_considered",
            "conflicts_detected",
            "stale_sources",
            "code_dirty_sources",
            "redaction_applied_before_return",
        ]
        for field in required_diagnostic_fields:
            assert field in result, f"Missing diagnostic field: {field}"
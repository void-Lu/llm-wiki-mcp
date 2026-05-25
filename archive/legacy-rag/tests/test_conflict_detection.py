"""Tests for T15: conflict detection and resolution logic."""
from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.models import ConflictReport, RoutingResult, SearchResult
from netsuite_rag_mcp.retriever import ask_netsuite_rag, detect_conflicts, resolve_conflicts
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder


# ── ConflictReport dataclass tests ─────────────────────────────────────────────


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
                "    default: test_conflict",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: test_conflict",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [src]",
                "    exclude: [.git, node_modules]",
                "    file_types: [js]",
                "    parser: suitescript_code_and_config",
                "    collection: test_conflict",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


class TestConflictReportDataclass:
    """Test that ConflictReport is a properly configured frozen dataclass."""

    def test_conflict_report_fields(self):
        report = ConflictReport(
            conflict_type="implementation",
            entity="customscript_order_sync",
            note_source={"citation_id": "S1", "path": "notes/order-sync.md"},
            code_source={"citation_id": "S3", "path": "src/OrderSync.js"},
            winning_source="code",
            explanation="Implementation fact: code is the authoritative source",
            uncertainty=False,
        )
        assert report.conflict_type == "implementation"
        assert report.entity == "customscript_order_sync"
        assert report.note_source["citation_id"] == "S1"
        assert report.code_source["citation_id"] == "S3"
        assert report.winning_source == "code"
        assert report.explanation == "Implementation fact: code is the authoritative source"
        assert report.uncertainty is False

    def test_conflict_report_is_frozen(self):
        report = ConflictReport(
            conflict_type="unclassified",
            entity="test_entity",
            note_source={},
            code_source={},
            winning_source="both",
            explanation="Cannot determine authoritative source",
            uncertainty=True,
        )
        try:
            report.conflict_type = "implementation"  # type: ignore[misc]
            assert False, "Should raise FrozenInstanceError"
        except AttributeError:
            pass  # Expected

    def test_conflict_report_uncertainty_default(self):
        """ConflictReport requires uncertainty field (no default)."""
        # Should not raise — uncertainty is required but explicitly set
        report = ConflictReport(
            conflict_type="business",
            entity="test",
            note_source={},
            code_source={},
            winning_source="note",
            explanation="Business rationale from note",
            uncertainty=False,
        )
        assert report.uncertainty is False


# ── detect_conflicts() tests ──────────────────────────────────────────────────


class TestDetectConflicts:
    """Test detect_conflicts() for entity overlap between note and code sources."""

    def _make_note_result(self, script_id: str, text: str, **extra_meta) -> SearchResult:
        metadata = {
            "source_kind": "note",
            "source_path": f"notes/{script_id}.md",
            "script_id": script_id,
            "updated_at": "2026-05-01T00:00:00Z",
            **extra_meta,
        }
        return SearchResult(
            citation_id="S1",
            chunk_id=f"note_{script_id}:0",
            text=text,
            metadata=metadata,
            distance=0.3,
            source_kind="note",
        )

    def _make_code_result(self, script_id: str, text: str, **extra_meta) -> SearchResult:
        metadata = {
            "source_kind": "code",
            "source_path": f"src/{script_id}.js",
            "script_id": script_id,
            "git_commit": "abc1234",
            **extra_meta,
        }
        return SearchResult(
            citation_id="S2",
            chunk_id=f"code_{script_id}:0",
            text=text,
            metadata=metadata,
            distance=0.35,
            source_kind="code",
        )

    def test_no_conflict_when_single_source(self):
        """No conflicts when results come from only one source kind."""
        results = [
            self._make_note_result("customscript_foo", "Note about foo"),
            self._make_note_result("customscript_bar", "Note about bar"),
        ]
        conflicts = detect_conflicts(results)
        assert conflicts == []

    def test_no_conflict_when_different_entities(self):
        """No conflicts when note and code describe different entities."""
        results = [
            self._make_note_result("customscript_foo", "Note about foo"),
            self._make_code_result("customscript_bar", "Code about bar"),
        ]
        conflicts = detect_conflicts(results)
        assert conflicts == []

    def test_conflict_detected_when_same_script_id(self):
        """Conflict detected when same script_id appears in both note and code with different text."""
        note = self._make_note_result(
            "customscript_order_sync",
            "RESTlet 会提交 Map/Reduce 处理订单同步。",
            source_name="obsidian",
        )
        code = self._make_code_result(
            "customscript_order_sync",
            "Map/Reduce script 处理订单同步的实际逻辑。",
            source_name="netsuite_repo",
        )
        conflicts = detect_conflicts([note, code])
        assert len(conflicts) == 1
        assert conflicts[0].entity == "customscript_order_sync"
        assert conflicts[0].note_source["source_kind"] == "note"
        assert conflicts[0].code_source["source_kind"] == "code"

    def test_conflict_detected_when_same_source_path(self):
        """Conflict detected when same source_path (not script_id) appears in both sources."""
        shared_path = "project-a/scripts/restlet/order-sync.md"
        note = SearchResult(
            citation_id="S1",
            chunk_id="note_doc1:0",
            text="Note version of content",
            metadata={"source_kind": "note", "source_path": shared_path},
            distance=0.3,
            source_kind="note",
        )
        code = SearchResult(
            citation_id="S2",
            chunk_id="code_doc1:0",
            text="Code version of content",
            metadata={"source_kind": "code", "source_path": shared_path},
            distance=0.35,
            source_kind="code",
        )
        # source_path overlap but different source_kind — entity should be the path
        conflicts = detect_conflicts([note, code])
        assert len(conflicts) >= 1
        assert conflicts[0].entity == shared_path

    def test_no_conflict_when_same_text_content(self):
        """No conflict when note and code have identical text (just duplicated info)."""
        shared_text = "This entity processes order sync."
        results = [
            self._make_note_result("customscript_order_sync", shared_text),
            self._make_code_result("customscript_order_sync", shared_text),
        ]
        conflicts = detect_conflicts(results)
        assert conflicts == []

    def test_multiple_conflicts_detected(self):
        """Multiple conflicts detected when multiple entities overlap."""
        results = [
            self._make_note_result("customscript_foo", "Note says foo does X"),
            self._make_code_result("customscript_foo", "Code says foo does Y"),
            self._make_note_result("customscript_bar", "Note says bar does A"),
            self._make_code_result("customscript_bar", "Code says bar does B"),
        ]
        conflicts = detect_conflicts(results)
        assert len(conflicts) == 2
        entities = {c.entity for c in conflicts}
        assert "customscript_foo" in entities
        assert "customscript_bar" in entities

    def test_conflict_entity_from_function_name(self):
        """Conflict detected when same function_name appears in both sources."""
        note = SearchResult(
            citation_id="S1",
            chunk_id="note_doc1:0",
            text="Note says afterSubmit handles validation",
            metadata={
                "source_kind": "note",
                "source_path": "notes/script.md",
                "function_name": "afterSubmit",
            },
            distance=0.3,
            source_kind="note",
        )
        code = SearchResult(
            citation_id="S2",
            chunk_id="code_doc1:0",
            text="Code shows afterSubmit performs calculation",
            metadata={
                "source_kind": "code",
                "source_path": "src/script.js",
                "function_name": "afterSubmit",
            },
            distance=0.35,
            source_kind="code",
        )
        conflicts = detect_conflicts([note, code])
        assert len(conflicts) >= 1
        assert conflicts[0].entity == "afterSubmit"

    def test_empty_results_no_conflict(self):
        """Empty result list produces no conflicts."""
        conflicts = detect_conflicts([])
        assert conflicts == []


# ── resolve_conflicts() tests ──────────────────────────────────────────────────


class TestResolveConflicts:
    """Test resolve_conflicts() applies resolution rules correctly."""

    def _make_conflict(self, conflict_type: str, entity: str = "customscript_test") -> ConflictReport:
        """Helper to create ConflictReport for resolution testing."""
        return ConflictReport(
            conflict_type=conflict_type,
            entity=entity,
            note_source={"citation_id": "S1", "source_kind": "note", "path": "notes/test.md"},
            code_source={"citation_id": "S2", "source_kind": "code", "path": "src/test.js"},
            winning_source="",  # Will be determined by resolution
            explanation="",
            uncertainty=False,
        )

    def _make_routing(self, kind: str = "mixed") -> RoutingResult:
        return RoutingResult(
            kind=kind,
            source_filter={} if kind == "mixed" else {"source_kind": kind.split("_")[0]},
            boost_factor=1.0,
            explanation=f"Routing: {kind}",
        )

    def test_implementation_conflict_code_wins(self):
        """Implementation conflicts resolved with code as authoritative source."""
        conflict = self._make_conflict("implementation")
        routing = self._make_routing("code_led")
        resolved = resolve_conflicts([conflict], routing)
        assert len(resolved) == 1
        assert resolved[0].winning_source == "code"
        assert "authoritative" in resolved[0].explanation.lower() or "code" in resolved[0].explanation.lower()
        assert resolved[0].uncertainty is False

    def test_business_conflict_note_wins(self):
        """Business conflicts resolved with note as authoritative source."""
        conflict = self._make_conflict("business")
        routing = self._make_routing("note_led")
        resolved = resolve_conflicts([conflict], routing)
        assert len(resolved) == 1
        assert resolved[0].winning_source == "note"
        assert "business" in resolved[0].explanation.lower() or "note" in resolved[0].explanation.lower()
        assert resolved[0].uncertainty is False

    def test_unclassified_conflict_both_shown_with_uncertainty(self):
        """Unclassified conflicts show both sources with uncertainty=True."""
        conflict = self._make_conflict("unclassified")
        routing = self._make_routing("mixed")
        resolved = resolve_conflicts([conflict], routing)
        assert len(resolved) == 1
        assert resolved[0].winning_source == "both"
        assert resolved[0].uncertainty is True

    def test_staleness_newer_code_wins(self):
        """Staleness conflict: code is newer (has recent git_commit) → code wins."""
        conflict = ConflictReport(
            conflict_type="staleness",
            entity="customscript_order_sync",
            note_source={
                "citation_id": "S1",
                "source_kind": "note",
                "path": "notes/order-sync.md",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            code_source={
                "citation_id": "S2",
                "source_kind": "code",
                "path": "src/OrderSync.js",
                "git_commit": "abc1234",
            },
            winning_source="",
            explanation="",
            uncertainty=False,
        )
        routing = self._make_routing("mixed")
        resolved = resolve_conflicts([conflict], routing)
        assert len(resolved) == 1
        # Code is newer (git_commit present and note is from Jan 2026), code should win
        assert resolved[0].winning_source in ("code", "both")

    def test_staleness_newer_note_wins(self):
        """Staleness conflict: note is newer → note wins."""
        conflict = ConflictReport(
            conflict_type="staleness",
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
            winning_source="",
            explanation="",
            uncertainty=False,
        )
        routing = self._make_routing("mixed")
        resolved = resolve_conflicts([conflict], routing)
        assert len(resolved) == 1
        # Note is from May 2026 (newer), note should win
        assert resolved[0].winning_source in ("note", "both")

    def test_code_led_routing_biases_implementation_to_code(self):
        """With code-led routing, implementation conflicts always favor code."""
        conflict = self._make_conflict("implementation")
        routing = self._make_routing("code_led")
        resolved = resolve_conflicts([conflict], routing)
        assert resolved[0].winning_source == "code"

    def test_note_led_routing_biases_business_to_note(self):
        """With note-led routing, business conflicts always favor note."""
        conflict = self._make_conflict("business")
        routing = self._make_routing("note_led")
        resolved = resolve_conflicts([conflict], routing)
        assert resolved[0].winning_source == "note"

    def test_mixed_routing_shows_both_for_unclassified(self):
        """With mixed routing, unclassified conflicts show both sources."""
        conflict = self._make_conflict("unclassified")
        routing = self._make_routing("mixed")
        resolved = resolve_conflicts([conflict], routing)
        assert resolved[0].winning_source == "both"
        assert resolved[0].uncertainty is True

    def test_resolved_conflicts_never_silently_merge(self):
        """Resolved conflicts always have explanation — never silently merged."""
        conflicts = [
            self._make_conflict("implementation"),
            self._make_conflict("business"),
            self._make_conflict("unclassified"),
        ]
        routing = self._make_routing("mixed")
        resolved = resolve_conflicts(conflicts, routing)
        for r in resolved:
            assert r.explanation, "Every resolved conflict must have an explanation"
            assert r.winning_source, "Every resolved conflict must have a winning_source"

    def test_empty_conflict_list_returns_empty(self):
        """Empty conflict list produces empty resolved list."""
        routing = self._make_routing("mixed")
        resolved = resolve_conflicts([], routing)
        assert resolved == []


# ── Integration: ask_netsuite_rag includes conflicts_detected ────────────────────


class TestAskNetsuiteRagConflictIntegration:
    """Test that ask_netsuite_rag includes conflict detection in response."""

    def _make_store_with_conflicting_sources(self, tmp_path: Path) -> ChromaVectorStore:
        """Create a store where the same script_id appears in both note and code."""
        from netsuite_rag_mcp.models import Chunk

        _write_sources_config(tmp_path)
        store = ChromaVectorStore(tmp_path / "chroma", "test_conflict", FakeEmbedder())

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
                "script_id": "customscript_order_sync_mr",
                "related_objects": ["salesorder"],
                "related_scripts": [],
                "status": "active",
                "source_kind": "note",
                "source_name": "obsidian",
                "updated_at": "2026-05-01T00:00:00Z",
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
            text="Map/Reduce script customscript_order_sync_mr 处理订单实际逻辑不同。",
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
                "function_name": "afterSubmit",
                "git_commit": "abc1234",
            },
            source_kind="code",
            source_name="netsuite_repo",
        )

        store.upsert_chunks([note_chunk, code_chunk])
        return store

    def test_ask_netsuite_rag_includes_conflicts_detected(self, tmp_path: Path):
        """ask_netsuite_rag response includes 'conflicts_detected' key."""
        store = self._make_store_with_conflicting_sources(tmp_path)
        result = ask_netsuite_rag(
            question="订单同步怎么实现以及为什么？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "conflicts_detected" in result
        assert isinstance(result["conflicts_detected"], list)

    def test_ask_netsuite_rag_conflict_has_required_fields(self, tmp_path: Path):
        """Each conflict in ask_netsuite_rag response has required fields."""
        store = self._make_store_with_conflicting_sources(tmp_path)
        result = ask_netsuite_rag(
            question="订单同步怎么实现以及为什么？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        conflicts = result["conflicts_detected"]
        if conflicts:
            conflict = conflicts[0]
            required_fields = [
                "conflict_type", "entity", "note_source", "code_source",
                "winning_source", "explanation", "uncertainty",
            ]
            for field in required_fields:
                assert field in conflict, f"Missing required field: {field}"

    def test_ask_netsuite_rag_no_conflicts_when_single_source(self, tmp_path: Path):
        """ask_netsuite_rag returns empty conflicts when results are from one source only."""
        store = self._make_store_with_conflicting_sources(tmp_path)
        result = ask_netsuite_rag(
            question="为什么使用RESTlet进行订单同步？",
            vault_root=tmp_path,
            source_kind="note",
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        # When filtered to note only, no conflicts (only one source kind)
        assert result["conflicts_detected"] == []

    def test_ask_netsuite_rag_no_silent_merge(self, tmp_path: Path):
        """Conflicts are surfaced in response, never silently merged."""
        store = self._make_store_with_conflicting_sources(tmp_path)
        result = ask_netsuite_rag(
            question="怎么实现以及为什么？",
            vault_root=tmp_path,
            top_k=10,
            embedder=FakeEmbedder(),
            store=store,
        )
        for conflict in result["conflicts_detected"]:
            assert conflict["winning_source"] in ("code", "note", "both"), \
                f"Conflict must have explicit winning_source, got: {conflict['winning_source']}"
            assert len(conflict["explanation"]) > 0, \
                "Conflict must have a non-empty explanation"
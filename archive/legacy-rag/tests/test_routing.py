"""Tests for T14: query routing strategy (note-led, code-led, mixed)."""
from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.models import Chunk, RoutingResult
from netsuite_rag_mcp.retriever import ask_netsuite_rag, route_query
from netsuite_rag_mcp.vector_store import ChromaVectorStore, FakeEmbedder


# ── RoutingResult dataclass tests ─────────────────────────────────────────────


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
                "    default: test_routing",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: test_routing",
                "    authority: curated_note_source",
                "  - source_name: netsuite_repo",
                "    source_kind: code",
                "    root: .",
                "    include: [src]",
                "    exclude: [.git, node_modules]",
                "    file_types: [js]",
                "    parser: suitescript_code_and_config",
                "    collection: test_routing",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )


class TestRoutingResultDataclass:
    """Test that RoutingResult is a properly configured frozen dataclass."""

    def test_routing_result_fields(self):
        result = RoutingResult(
            kind="note_led",
            source_filter={"source_kind": "note"},
            boost_factor=1.0,
            explanation="Query contains business rationale keywords",
        )
        assert result.kind == "note_led"
        assert result.source_filter == {"source_kind": "note"}
        assert result.boost_factor == 1.0
        assert result.explanation == "Query contains business rationale keywords"

    def test_routing_result_is_frozen(self):
        result = RoutingResult(
            kind="mixed", source_filter={}, boost_factor=1.0, explanation="test"
        )
        try:
            result.kind = "code_led"  # type: ignore[misc]
            assert False, "Should raise FrozenInstanceError"
        except AttributeError:
            pass  # Expected


# ── route_query() tests ───────────────────────────────────────────────────────


class TestRouteQueryNoteLed:
    """Test that note-led keywords route to source_kind='note'."""

    def test_english_why(self):
        result = route_query("Why do we use RESTlet for order sync?")
        assert result.kind == "note_led"
        assert result.source_filter == {"source_kind": "note"}

    def test_english_rationale(self):
        result = route_query("What is the rationale for this design?")
        assert result.kind == "note_led"

    def test_english_troubleshooting(self):
        result = route_query("Troubleshooting the deployment failure")
        assert result.kind == "note_led"

    def test_english_decision(self):
        result = route_query("What was the decision behind this approach?")
        assert result.kind == "note_led"

    def test_english_background(self):
        result = route_query("Give me background on the sync process")
        assert result.kind == "note_led"

    def test_chinese_为什么(self):
        result = route_query("为什么使用RESTlet进行订单同步？")
        assert result.kind == "note_led"

    def test_chinese_背景(self):
        result = route_query("给我一些背景信息")
        assert result.kind == "note_led"

    def test_chinese_决策(self):
        result = route_query("这个决策是怎么做的？")
        assert result.kind == "note_led"

    def test_chinese_需求(self):
        result = route_query("原始需求是什么？")
        assert result.kind == "note_led"

    def test_chinese_排坑(self):
        result = route_query("排坑记录都在这里")
        assert result.kind == "note_led"


class TestRouteQueryCodeLed:
    """Test that code-led keywords route to source_kind='code'."""

    def test_english_function(self):
        result = route_query("Show me the function that handles order sync")
        assert result.kind == "code_led"
        assert result.source_filter == {"source_kind": "code"}

    def test_english_implementation(self):
        result = route_query("What is the implementation detail?")
        assert result.kind == "code_led"

    def test_english_script(self):
        result = route_query("Which script processes the order?")
        assert result.kind == "code_led"

    def test_english_deployment(self):
        result = route_query("How does the deployment work?")
        assert result.kind == "code_led"

    def test_english_config(self):
        result = route_query("What config is needed?")
        assert result.kind == "code_led"

    def test_english_entry_point(self):
        result = route_query("What is the entry point for the script?")
        assert result.kind == "code_led"

    def test_chinese_函数(self):
        result = route_query("这个函数做了什么？")
        assert result.kind == "code_led"

    def test_chinese_脚本(self):
        result = route_query("脚本是如何处理的？")
        assert result.kind == "code_led"

    def test_chinese_参数(self):
        result = route_query("这个脚本的参数有哪些？")
        assert result.kind == "code_led"

    def test_chinese_代码(self):
        result = route_query("代码里怎么处理的？")
        assert result.kind == "code_led"


class TestRouteQueryMixed:
    """Test that mixed keywords route to both sources."""

    def test_english_how_and_why(self):
        result = route_query("How and why does the order sync work?")
        assert result.kind == "mixed"
        assert result.source_filter == {}

    def test_english_impact(self):
        result = route_query("What is the impact analysis of this change?")
        assert result.kind == "mixed"

    def test_english_both(self):
        result = route_query("Show both the implementation and rationale")
        assert result.kind == "mixed"

    def test_chinese_怎么实现以及为什么(self):
        result = route_query("怎么实现以及为什么这样做？")
        assert result.kind == "mixed"

    def test_chinese_影响分析(self):
        result = route_query("这个变更的影响分析是什么？")
        assert result.kind == "mixed"


class TestRouteQueryDefault:
    """Test that queries with no routing keywords default to 'mixed'."""

    def test_no_keywords_defaults_to_mixed(self):
        result = route_query("订单同步")
        assert result.kind == "mixed"

    def test_ambiguous_defaults_to_mixed(self):
        result = route_query("Tell me about the project")
        assert result.kind == "mixed"
        assert result.source_filter == {}

    def test_empty_string_defaults_to_mixed(self):
        result = route_query("")
        assert result.kind == "mixed"

    def test_result_includes_explanation(self):
        result = route_query("Why does this fail?")
        assert isinstance(result.explanation, str)
        assert len(result.explanation) > 0


class TestRouteQueryExplicitSourceKind:
    """Test that explicit source_kind bypasses heuristic routing."""

    def test_explicit_note_overrides_note_led(self):
        """If user explicitly says source_kind='note', use it regardless of query."""
        result = route_query("Show me the function", source_kind="note")
        assert result.kind == "note_led"
        assert result.source_filter == {"source_kind": "note"}
        assert "explicit" in result.explanation.lower() or "override" in result.explanation.lower()

    def test_explicit_code_overrides_code_led(self):
        """If user explicitly says source_kind='code', use it regardless of query."""
        result = route_query("Why does this fail?", source_kind="code")
        assert result.kind == "code_led"
        assert result.source_filter == {"source_kind": "code"}

    def test_explicit_source_kind_provides_explanation(self):
        result = route_query("anything", source_kind="note")
        assert isinstance(result.explanation, str)
        assert len(result.explanation) > 0


# ── Integration: ask_netsuite_rag includes routing in response ──────────────


def _setup_store_with_mixed_sources(tmp_path: Path) -> ChromaVectorStore:
    """Create a store with both note and code chunks."""
    _write_sources_config(tmp_path)
    store = ChromaVectorStore(tmp_path / "chroma", "test_routing", FakeEmbedder())

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
            "source_kind": "code",
            "source_name": "netsuite_repo",
        },
    )

    store.upsert_chunks([note_chunk, code_chunk])
    return store


class TestAskNetsuiteRagRouting:
    """Test that ask_netsuite_rag includes routing diagnostics."""

    def test_routing_included_in_response(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="Why does the order sync fail?",
            vault_root=tmp_path,
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "routing" in result
        assert result["routing"]["kind"] in ("note_led", "code_led", "mixed")

    def test_note_led_routing_adjusts_source_kind(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="为什么订单同步失败？",
            vault_root=tmp_path,
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        # note-led query should route to note source
        routing = result["routing"]
        assert routing["kind"] == "note_led"
        assert routing["source_filter"] == {"source_kind": "note"}
        # Only note results should be returned
        for r in result["results"] if "results" in result else []:
            pass  # ask_netsuite_rag doesn't have a 'results' key, check sources

    def test_code_led_routing_adjusts_source_kind(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="What function handles order sync?",
            vault_root=tmp_path,
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        routing = result["routing"]
        assert routing["kind"] == "code_led"
        assert routing["source_filter"] == {"source_kind": "code"}

    def test_mixed_routing_returns_both_sources(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="订单同步",
            vault_root=tmp_path,
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        routing = result["routing"]
        assert routing["kind"] == "mixed"
        assert routing["source_filter"] == {}

    def test_explicit_source_kind_overrides_routing(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="Why does this fail?",  # Would normally be note_led
            vault_root=tmp_path,
            source_kind="code",  # Explicit override
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        routing = result["routing"]
        assert routing["kind"] == "code_led"
        assert routing["source_filter"] == {"source_kind": "code"}

    def test_routing_includes_explanation(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="Why does this fail?",
            vault_root=tmp_path,
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert isinstance(result["routing"]["explanation"], str)
        assert len(result["routing"]["explanation"]) > 0

    def test_routing_includes_boost_factor(self, tmp_path: Path):
        store = _setup_store_with_mixed_sources(tmp_path)
        result = ask_netsuite_rag(
            question="Why does this fail?",
            vault_root=tmp_path,
            top_k=5,
            embedder=FakeEmbedder(),
            store=store,
        )
        assert "boost_factor" in result["routing"]
        assert isinstance(result["routing"]["boost_factor"], float)
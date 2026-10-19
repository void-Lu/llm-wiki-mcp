"""Tests for citation / retrieval trace (no passage bodies in telemetry)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from retrieval.candidate_items import candidate_item
from retrieval.citation_trace import (
    BODY_KEYS,
    build_citation_trace,
    write_citation_trace_dump,
)
from retrieval.query_execution_context import QueryExecutionView, QueryRequestView
from retrieval.query_pipeline import (
    QuerySeedStats,
    assemble_public_projection,
)
from retrieval.query_shared import heading
from retrieval.query_recovery import RecoveryAssembly
from retrieval.query_shared import QueryFilters
from retrieval.query_telemetry import QueryTelemetry, redact_query
from retrieval.query_recall_policy import RRF_K
from retrieval.retrieval_index import PassageHit


def _hit(
    passage_id: str = "hit-id",
    *,
    path: str = "wiki/concepts/a.md",
    ordinal: int = 1,
    text: str = "Hit sentence about invoice approval.",
    title: str = "Invoice",
) -> PassageHit:
    return PassageHit(
        passage_id,
        path,
        title,
        ("Steps",),
        text,
        0.8,
        "active",
        "formal",
        "wiki",
        ordinal,
    )


def _assert_no_body(obj: object) -> None:
    if isinstance(obj, dict):
        assert BODY_KEYS.isdisjoint(obj.keys())
        for value in obj.values():
            _assert_no_body(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_body(item)


def test_build_citation_trace_shape_and_no_body() -> None:
    hit = _hit()
    item = candidate_item(hit, score=1.2, fts_rank=1, rrf=0.7, vector_score=0.1, graph_score=0.0)
    expansion = {
        hit.passage_id: {
            "expanded": True,
            "neighbor_ordinals": (0, 2),
            "expansion_chars": 40,
        }
    }
    trace = build_citation_trace(
        question="invoice approval api_key=sk-secret",
        public_scope="knowledge",
        intent="concept",
        retrieval_mode="hybrid",
        lexical_mode="strict",
        public_selected=[item],
        expansion_by_passage_id=expansion,
        heading_of=heading,
    )
    assert trace["query_redacted"] == redact_query("invoice approval api_key=sk-secret")
    assert "sk-secret" not in trace["query_redacted"]
    assert trace["scope"] == "knowledge"
    assert trace["intent"] == "concept"
    assert trace["retrieval_mode"] == "hybrid"
    assert trace["lexical_mode"] == "strict"
    assert len(trace["hits"]) == 1
    row = trace["hits"][0]
    assert row["citation"] == "[1]"
    assert row["passage_id"] == "hit-id"
    assert row["path"] == "wiki/concepts/a.md"
    assert row["heading"] == heading(hit)
    assert row["score"] == 1.2
    assert row["scores"]["rrf"] == 0.7
    assert row["expanded"] is True
    assert row["neighbor_ordinals"] == [0, 2]
    assert row["expansion_chars"] == 40
    _assert_no_body(trace)
    assert "Hit sentence" not in json.dumps(trace)


def test_write_citation_trace_dump_creates_side_file(tmp_path: Path) -> None:
    trace = {
        "query_redacted": "invoice approval",
        "scope": "knowledge",
        "intent": "concept",
        "retrieval_mode": "hybrid",
        "lexical_mode": "strict",
        "hits": [{"passage_id": "hit-id", "path": "wiki/a.md", "expanded": False}],
    }
    relative = write_citation_trace_dump(tmp_path, trace)
    digest = hashlib.sha256(b"invoice approval").hexdigest()
    assert relative == f".llm-wiki/citation-traces/{digest}.json"
    path = tmp_path / relative
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["query_redacted"] == "invoice approval"
    assert loaded["hits"][0]["passage_id"] == "hit-id"
    _assert_no_body(loaded)


def _minimal_view(item) -> QueryExecutionView:
    recovery = RecoveryAssembly(
        selected=[item],
        hit_stats={},
        pool_by_page={},
        context_items=[item],
        fallback={"level": "none", "reasons": [], "allowed_source_paths": []},
    )
    return QueryExecutionView(
        selected=(item,),
        context_items=(item,),
        recovery=recovery,
        status={"ok": True},
        raw_availability="missing",
        raw_fts_hits=0,
        relaxed_fts_hits=0,
        raw_index_warning="",
        coverage_fallback=False,
        lexical_mode="strict",
        expansion_suggestions=(),
        uncovered_latin_terms=(),
        discovery={},
        discovery_entities=(),
        discovery_source_items=(),
        batch_payload={"status": "not_triggered", "entities": []},
        discovery_requested=False,
    )


def test_assemble_public_projection_includes_citation_trace_and_result_fields() -> None:
    prev = _hit("prev-id", ordinal=0, text="Neighbor previous sentence about invoice limits.")
    hit = _hit("hit-id", ordinal=1, text="Hit sentence about invoice approval steps.")
    item = candidate_item(hit, score=1.2, fts_rank=1, rrf=0.7)
    request_view = QueryRequestView(
        question="invoice approval",
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(),
        top_k=1,
        metadata={"wiki/concepts/a.md": {"type": "concept", "tags": []}},
        intent="concept",
        effective_rrf_k=RRF_K,
        public_scope="knowledge",
        lexical_enabled=True,
        include_context_pack=True,
        expand_parents=True,
        parent_window=1,
        same_heading_only=False,
        dump_citation_trace=False,
    )

    def load(page_path: str, ordinals: list[int]) -> list[PassageHit]:
        pool = {0: prev, 1: hit}
        return [pool[o] for o in sorted(set(ordinals)) if o in pool]

    projection = assemble_public_projection(
        _minimal_view(item),
        request_view=request_view,
        provenance={},
        seed_stats=QuerySeedStats(
            fts_hits=1,
            qualified_fts_hits=0,
            vector_hits=0,
            vector_warnings=(),
            index_warnings=(),
            scope_rules=(),
        ),
        quality_gate_evaluation=None,
        load_passages_at_ordinals=load,
    )
    response = projection.response
    assert "citation_trace" in response
    trace = response["citation_trace"]
    assert trace["query_redacted"] == "invoice approval"
    assert trace["hits"][0]["passage_id"] == "hit-id"
    assert trace["hits"][0]["expanded"] is True
    assert 0 in trace["hits"][0]["neighbor_ordinals"]
    _assert_no_body(trace)
    result = response["results"][0]
    assert result["passage_id"] == "hit-id"
    assert result["expanded"] is True
    assert result["expansion_chars"] > 0
    assert "citation_trace_path" not in response
    assert projection.telemetry.passage_ids == ("hit-id",)


def test_dump_citation_trace_writes_file_when_enabled(tmp_path: Path) -> None:
    hit = _hit()
    item = candidate_item(hit, score=1.0, fts_rank=1, rrf=0.5)
    request_view = QueryRequestView(
        question="invoice approval",
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(),
        top_k=1,
        metadata={"wiki/concepts/a.md": {"type": "concept", "tags": []}},
        intent="concept",
        effective_rrf_k=RRF_K,
        public_scope="knowledge",
        lexical_enabled=True,
        include_context_pack=False,
        expand_parents=False,
        dump_citation_trace=True,
    )
    projection = assemble_public_projection(
        _minimal_view(item),
        request_view=request_view,
        provenance={},
        seed_stats=QuerySeedStats(
            fts_hits=1,
            qualified_fts_hits=0,
            vector_hits=0,
            vector_warnings=(),
            index_warnings=(),
            scope_rules=(),
        ),
        quality_gate_evaluation=None,
        vault_root=tmp_path,
    )
    relative = projection.response["citation_trace_path"]
    path = tmp_path / relative
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["hits"][0]["passage_id"] == "hit-id"
    assert loaded["hits"][0]["expanded"] is False
    _assert_no_body(loaded)


def test_query_telemetry_still_stores_no_content(tmp_path: Path) -> None:
    telemetry = QueryTelemetry(tmp_path)
    telemetry.record(
        question="invoice approval with body SECRET",
        scope="knowledge",
        project=None,
        passage_ids=["hit-id"],
        fallback_level="none",
        token_count=12,
        latency_ms=1.5,
    )
    status = telemetry.status()
    assert status["stores_content"] is False
    conn = sqlite3.connect(telemetry.path)
    try:
        row = conn.execute(
            "SELECT normalized_query_redacted, passage_ids FROM query_telemetry"
        ).fetchone()
        columns = {str(info[1]) for info in conn.execute("PRAGMA table_info(query_telemetry)")}
    finally:
        conn.close()
    assert row is not None
    assert "Hit sentence" not in row[0]
    assert row[1] == "hit-id"
    assert BODY_KEYS.isdisjoint(columns)


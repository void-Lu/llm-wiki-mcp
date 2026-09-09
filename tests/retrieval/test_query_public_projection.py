from __future__ import annotations

from retrieval.candidate_items import candidate_item
from retrieval.query_execution_context import QueryExecutionView, QueryRequestView
from retrieval.query_pipeline import (
    QuerySeedStats,
    _QualityGateEvaluation,
    assemble_public_projection,
)
from retrieval.query_recovery import RecoveryAssembly
from retrieval.query_shared import QueryFilters
from retrieval.retrieval_index import PassageHit
from retrieval.query_recall_policy import RRF_K


def test_assemble_public_projection_projects_seed_gate_discovery_and_context() -> None:
    hit = PassageHit(
        "passage-1",
        "wiki/concepts/invoice.md",
        "Invoice Approval",
        ("Steps",),
        "Invoice approval content",
        0.8,
        "active",
        "formal",
        "wiki",
    )
    item = candidate_item(hit, score=1.2, fts_rank=1, rrf=0.7)
    recovery = RecoveryAssembly(
        selected=[item],
        hit_stats={},
        pool_by_page={},
        context_items=[item],
        fallback={"level": "none", "reasons": [], "allowed_source_paths": []},
    )
    view = QueryExecutionView(
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
        discovery={
            "candidate_entities": [{"identifier": "N/auth"}],
            "total_count": 1,
            "returned_count": 1,
            "truncated": False,
        },
        discovery_entities=({"identifier": "N/auth"},),
        discovery_source_items=(),
        batch_payload={"status": "not_triggered", "entities": []},
        discovery_requested=True,
    )
    request_view = QueryRequestView(
        question="invoice approval",
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(),
        top_k=1,
        metadata={"wiki/concepts/invoice.md": {"type": "concept", "tags": []}},
        intent="concept",
        effective_rrf_k=RRF_K,
        public_scope="knowledge",
        lexical_enabled=True,
        include_context_pack=True,
    )
    gate_summary = {
        "policy_version": "query-quality-policy-v0",
        "mode": "shadow",
        "status": "gate_shadow",
        "candidate_count": 1,
        "accepted_count": 1,
        "rejected_count": 0,
        "score_family_counts": {"main_rrf": 1},
        "reason_counts": {"gate_keep_default": 1},
        "low_sample_buckets": [],
        "fail_open": False,
    }
    projection = assemble_public_projection(
        view,
        request_view=request_view,
        provenance={},
        seed_stats=QuerySeedStats(
            fts_hits=3,
            qualified_fts_hits=1,
            vector_hits=0,
            vector_warnings=("model_missing",),
            index_warnings=(),
            scope_rules=("history_intent",),
        ),
        quality_gate_evaluation=_QualityGateEvaluation(gate_summary, None),
    )

    assert projection.response["scope"] == "knowledge"
    assert projection.response["results"][0]["content"] == "Invoice approval content"
    assert projection.response["pipeline"]["counters"] == {
        "fts_hits": 3,
        "qualified_fts_hits": 1,
        "relaxed_fts_hits": 0,
        "raw_fts_hits": 0,
        "vector_hits": 0,
        "graph_hits": 0,
        "selected": 1,
        "returned": 1,
        "additional": 0,
    }
    assert projection.response["pipeline"]["warnings"] == [
        "history_intent",
        "model_missing",
    ]
    assert projection.response["pipeline"]["quality_gate"] == gate_summary
    assert projection.response["pipeline"]["discovery"] == view.discovery

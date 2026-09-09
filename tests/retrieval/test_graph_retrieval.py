from __future__ import annotations

from pathlib import Path

from retrieval.graph_retrieval import (
    Graph,
    QueryCandidate,
    apply_graph_expansion,
    relationship_evidence_for,
    relationship_reasons_for,
    relationship_score_for,
)


def test_relationship_evidence_has_one_score_owner_for_all_relationships() -> None:
    graph = Graph(
        neighbors={"left": {"right", "common"}, "right": {"common"}, "common": {"left", "right"}},
        sources={"left": {"source-a"}, "right": {"source-a"}},
        types={"left": "concept", "right": "concept", "common": "concept"},
    )

    evidence = relationship_evidence_for("left", "right", graph)
    reasons = relationship_reasons_for("left", "right", graph)

    assert [reason["kind"] for reason in reasons] == ["direct_wikilink", "shared_source", "common_neighbor", "same_type"]
    assert relationship_score_for("left", "right", graph) == evidence.score
    assert evidence.score == round(sum(float(reason["score"]) for reason in reasons), 12)


def test_graph_debug_only_controls_reason_projection_not_contribution() -> None:
    graph = Graph(
        neighbors={"wiki/left.md": {"wiki/right.md"}, "wiki/right.md": {"wiki/left.md"}},
        sources={"wiki/left.md": set(), "wiki/right.md": set()},
        types={"wiki/left.md": "concept", "wiki/right.md": "concept"},
    )

    def run(collect_reasons: bool) -> QueryCandidate:
        seed = QueryCandidate(Path("left.md"), "wiki/left.md", "left", "", fusion_score=10.0)
        target = QueryCandidate(Path("right.md"), "wiki/right.md", "right", "", fusion_score=10.0)
        apply_graph_expansion({"wiki/left.md": seed}, [seed, target], graph, 1, collect_reasons=collect_reasons)
        return target

    without_debug = run(False)
    with_debug = run(True)

    assert with_debug.graph_score == without_debug.graph_score
    assert without_debug.rank_breakdown.graph_reasons == []
    assert with_debug.rank_breakdown.graph_reasons

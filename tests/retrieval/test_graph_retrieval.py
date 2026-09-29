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


def _candidate(rel: str, fusion_score: float = 0.0) -> QueryCandidate:
    return QueryCandidate(Path(rel), rel, rel, "", fusion_score=fusion_score, keyword_score=fusion_score)


def test_graph_only_scores_follow_raw_evidence_below_the_pure_cap() -> None:
    seed, strong, weak, far = "wiki/seed.md", "wiki/strong.md", "wiki/weak.md", "wiki/far.md"
    graph = Graph(
        neighbors={seed: {strong, weak}, strong: {seed, far}, weak: {seed}, far: {strong}},
        sources={seed: {"raw/a.md"}, strong: {"raw/a.md"}, weak: set(), far: set()},
        types={seed: "concept", strong: "concept", weak: "entity", far: "entity"},
    )
    candidates = {rel: _candidate(rel) for rel in (strong, weak, far)}
    seed_candidate = _candidate(seed, fusion_score=10.0)
    scored = {seed: seed_candidate}

    apply_graph_expansion(scored, [seed_candidate, *candidates.values()], graph, 2, collect_reasons=False)

    strong_score, weak_score, far_score = (candidates[rel].graph_score for rel in (strong, weak, far))
    # direct + shared source + same type > direct only > two-hop only
    assert 0.75 > strong_score > weak_score > far_score > 0
    # one direct wikilink (3.0) from one seed sits at half of the cap
    assert weak_score == 0.375
    assert candidates[strong].graph_evidence == 8.0


def test_lexical_candidates_keep_the_proportional_graph_cap() -> None:
    seed, other = "wiki/seed.md", "wiki/other.md"
    graph = Graph(
        neighbors={seed: {other}, other: {seed}},
        sources={seed: set(), other: set()},
        types={seed: "concept", other: "concept"},
    )
    seed_candidate = _candidate(seed, fusion_score=10.0)
    other_candidate = _candidate(other, fusion_score=2.0)

    apply_graph_expansion({seed: seed_candidate}, [seed_candidate, other_candidate], graph, 1, collect_reasons=False)

    assert other_candidate.graph_score == 0.3  # 15% of its own lexical score
    assert other_candidate.graph_evidence == 0.0

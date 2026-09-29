from __future__ import annotations

from pathlib import Path

import pytest

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


def _ranked_graph_only(pure_seed_ratio: float | None) -> dict[str, QueryCandidate]:
    seed, strong, weak, far = "wiki/seed.md", "wiki/strong.md", "wiki/weak.md", "wiki/far.md"
    graph = Graph(
        neighbors={seed: {strong, weak}, strong: {seed, far}, weak: {seed}, far: {strong}},
        sources={seed: {"raw/a.md"}, strong: {"raw/a.md"}, weak: set(), far: set()},
        types={seed: "concept", strong: "concept", weak: "entity", far: "entity"},
    )
    candidates = {rel: _candidate(rel) for rel in (strong, weak, far)}
    seed_candidate = _candidate(seed, fusion_score=10.0)
    apply_graph_expansion(
        {seed: seed_candidate}, [seed_candidate, *candidates.values()], graph, 2,
        collect_reasons=False, pure_seed_ratio=pure_seed_ratio,
    )
    return {rel.removeprefix("wiki/").removesuffix(".md"): candidate for rel, candidate in candidates.items()}


def test_graph_only_scores_follow_raw_evidence_below_the_seed_relative_cap() -> None:
    ranked = _ranked_graph_only(0.7)
    strong_score, weak_score, far_score = (ranked[name].graph_score for name in ("strong", "weak", "far"))
    # direct + shared source + same type > direct only > two-hop only, and
    # every graph-only page stays below ratio x seed relevance (0.7 x 10).
    assert 7.0 > strong_score > weak_score > far_score > 0
    # one direct wikilink (3.0) from one seed sits at half of the relative cap
    assert weak_score == 3.5
    assert ranked["strong"].graph_evidence == 8.0
    assert ranked["strong"].graph_seed_score == 10.0


def test_graph_only_scores_keep_the_fixed_cap_without_a_seed_ratio() -> None:
    ranked = _ranked_graph_only(None)
    strong_score, weak_score, far_score = (ranked[name].graph_score for name in ("strong", "weak", "far"))
    assert 0.75 > strong_score > weak_score > far_score > 0
    assert weak_score == 0.375


def test_graph_only_cap_follows_the_best_contributing_seed() -> None:
    strong_seed, weak_seed, near_strong, near_weak, shared = (
        "wiki/strong-seed.md", "wiki/weak-seed.md", "wiki/near-strong.md", "wiki/near-weak.md", "wiki/shared.md",
    )
    graph = Graph(
        neighbors={
            strong_seed: {near_strong, shared}, weak_seed: {near_weak, shared},
            near_strong: {strong_seed}, near_weak: {weak_seed}, shared: {strong_seed, weak_seed},
        },
        sources={rel: set() for rel in (strong_seed, weak_seed, near_strong, near_weak, shared)},
        types={strong_seed: "a", weak_seed: "b", near_strong: "c", near_weak: "d", shared: "e"},
    )
    seeds = {strong_seed: _candidate(strong_seed, fusion_score=10.0), weak_seed: _candidate(weak_seed, fusion_score=2.0)}
    targets = {rel: _candidate(rel) for rel in (near_strong, near_weak, shared)}

    apply_graph_expansion(dict(seeds), [*seeds.values(), *targets.values()], graph, 1, collect_reasons=False)

    # Same evidence (one direct wikilink), different seed strength.
    assert targets[near_strong].graph_score == 3.5
    assert targets[near_weak].graph_score == 0.7
    # Evidence from both seeds accumulates; the cap uses the stronger seed.
    assert targets[shared].graph_evidence == 6.0
    assert targets[shared].graph_seed_score == 10.0
    assert targets[shared].graph_score == pytest.approx(0.7 * 10.0 * 6.0 / (6.0 + 3.0))
    # A graph-only page never outranks the seed that reached it.
    assert all(target.graph_score < 10.0 for target in targets.values())


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


def test_typed_relations_are_weighted_evidence_and_traversable_edges() -> None:
    spec, origin, other = "wiki/spec.md", "wiki/origin.md", "wiki/other.md"
    graph = Graph(
        neighbors={spec: set(), origin: set(), other: set()},
        sources={spec: set(), origin: set(), other: set()},
        types={spec: "shared_spec", origin: "spec", other: "entity"},
        typed={spec: {origin: {"derived_from"}, other: {"related_objects"}}, origin: {spec: {"derived_from"}}, other: {spec: {"related_objects"}}},
    )

    assert [(reason["kind"], reason["value"], reason["score"]) for reason in relationship_evidence_for(spec, origin, graph).reasons] == [("typed_relation", "derived_from", 3.0)]
    assert relationship_score_for(other, spec, graph) == 2.0
    assert graph.adjacent(spec) == {origin, other}

    seed = _candidate(origin, fusion_score=10.0)
    targets = {rel: _candidate(rel) for rel in (spec, other)}
    apply_graph_expansion({origin: seed}, [seed, *targets.values()], graph, 2, collect_reasons=False)
    # hop 1 through derived_from scores the relation itself ...
    assert targets[spec].graph_evidence == 3.0
    # ... while a typed hop only bridges: common neighbours stay wikilink-based,
    # so a two-hop page with no evidence of its own gains nothing.
    assert targets[other].graph_score == 0.0

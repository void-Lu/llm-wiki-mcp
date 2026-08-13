from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

import pytest

from retrieval.candidate_items import CANDIDATE_CORE_KEYS, FUSION_KEYS, candidate_item, with_fusion
from retrieval.body_budget import PAGE_TOKEN_BUDGET
from retrieval.query_cancellation import QueryCancelled, QueryCancellationContext
from retrieval.query_recovery import (
    FallbackDecision,
    LadderStep,
    RecoveryCondition,
    assemble_recovery,
    compose_score,
    fallback_envelope,
    plan_fallback,
    search_ladder,
    select_best_per_page,
    _build_page_ordered_context,
)
from retrieval.retrieval_index import PassageHit
from retrieval.query_snapshot import QueryCorpusSnapshot


class FakeStore:
    def __init__(self, hits: list[PassageHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[list[str], int]] = []

    def passages_for_pages(self, paths: list[str], *, limit_per_page: int) -> list[PassageHit]:
        self.calls.append((paths, limit_per_page))
        return [hit for hit in self.hits if hit.page_path in paths]


def _item(path: str, passage_id: str, score: float, *, source_kind: str = "wiki") -> dict[str, object]:
    return candidate_item(
        PassageHit(passage_id, path, Path(path).stem, (), f"evidence {passage_id}", score, "active", "high", source_kind),
        score=score,
        fts_rank=1,
    )


def test_candidate_item_has_the_canonical_core_shape_and_defaults() -> None:
    hit = PassageHit("p-1", "wiki/concepts/item.md", "Item", (), "body", 1.0, "active", "high", "wiki")

    item = candidate_item(hit, score=2.5)

    assert set(item) == CANDIDATE_CORE_KEYS
    assert item["hit"] is hit
    assert item["score"] == 2.5
    assert item["fts_rank"] is None
    assert item["title_rank"] is None
    assert item["vector_rank"] is None
    assert item["vector_score"] == 0.0
    assert item["rrf"] == 0.0
    assert item["exact"] is False
    assert item["graph_score"] == 0.0
    assert item["graph_reasons"] == []


def test_with_fusion_adds_only_the_canonical_fusion_keys() -> None:
    item = _item("wiki/concepts/item.md", "p-1", 2.5)

    fused = with_fusion(
        item,
        coverage_terms=["rag"],
        coverage_ratio=1.0,
        source_local_rank=1,
        source_local_rrf=0.9,
        fusion_score=1.9,
        fusion_source="active",
        fusion_local_position=1,
    )

    assert set(fused) == set(item) | FUSION_KEYS
    assert fused["coverage_terms"] == ["rag"]
    assert fused["fusion_score"] == 1.9

    minimal = with_fusion(
        {"hit": item["hit"], "score": item["score"]},
        coverage_terms=[],
        coverage_ratio=0.0,
        source_local_rank=1,
        source_local_rrf=0.5,
        fusion_score=0.5,
        fusion_source="active",
        fusion_local_position=1,
    )
    assert set(minimal) == {"hit", "score", *FUSION_KEYS}


def test_fallback_decision_and_envelope_share_one_shape() -> None:
    expected = {
        "level": "raw",
        "reasons": ["wiki_zero_results"],
        "allowed_source_paths": ["raw/a.md"],
    }

    assert FallbackDecision("raw", ("wiki_zero_results",), ("raw/a.md",)).as_dict() == expected
    assert fallback_envelope("raw", ("wiki_zero_results",), ("raw/a.md",)) == expected
    assert FallbackDecision("none").as_dict() == {
        "level": "none",
        "reasons": [],
        "allowed_source_paths": [],
    }


def test_plan_fallback_covers_each_branch_and_main_path() -> None:
    cases = [
        (
            dict(has_primary_recall=True, effective_scope="knowledge", uncovered_latin_terms=["rag"], wiki_relaxed_answered=False, raw_available=True, relaxed_available=False),
            "coverage",
        ),
        (
            dict(has_primary_recall=False, effective_scope="knowledge", uncovered_latin_terms=[], wiki_relaxed_answered=False, raw_available=False, relaxed_available=True),
            "wiki_relaxed",
        ),
        (
            dict(has_primary_recall=False, effective_scope="all", uncovered_latin_terms=["llm"], wiki_relaxed_answered=True, raw_available=True, relaxed_available=True),
            "all_coverage",
        ),
        (
            dict(has_primary_recall=False, effective_scope="all", uncovered_latin_terms=[], wiki_relaxed_answered=False, raw_available=True, relaxed_available=False),
            "raw_zero",
        ),
    ]

    for inputs, branch in cases:
        plan = plan_fallback(**inputs)
        assert plan is not None
        assert plan.branch == branch
    assert plan_fallback(
        has_primary_recall=True,
        effective_scope="knowledge",
        uncovered_latin_terms=[],
        wiki_relaxed_answered=False,
        raw_available=False,
        relaxed_available=False,
    ) is None


def test_plan_fallback_exhausts_the_decision_matrix() -> None:
    scopes = ("knowledge", "all", "history", "raw", "archive")
    for has_primary_recall in (False, True):
        for effective_scope in scopes:
            for uncovered in ([], ["rag"]):
                for wiki_relaxed_answered in (False, True):
                    for raw_available in (False, True):
                        for relaxed_available in (False, True):
                            plan = plan_fallback(
                                has_primary_recall=has_primary_recall,
                                effective_scope=effective_scope,
                                uncovered_latin_terms=uncovered,
                                wiki_relaxed_answered=wiki_relaxed_answered,
                                raw_available=raw_available,
                                relaxed_available=relaxed_available,
                            )
                            if (
                                has_primary_recall
                                and uncovered
                                and effective_scope in {"knowledge", "all"}
                            ):
                                assert plan is not None and plan.branch == "coverage"
                            elif (
                                not has_primary_recall
                                and effective_scope == "all"
                                and wiki_relaxed_answered
                                and uncovered
                                and raw_available
                            ):
                                assert plan is not None and plan.branch == "all_coverage"
                            elif (
                                not has_primary_recall
                                and effective_scope in {"knowledge", "all"}
                                and relaxed_available
                            ):
                                assert plan is not None and plan.branch == "wiki_relaxed"
                            elif (
                                not has_primary_recall
                                and not wiki_relaxed_answered
                                and effective_scope in {"knowledge", "all"}
                                and raw_available
                            ):
                                assert plan is not None and plan.branch == "raw_zero"
                            else:
                                assert plan is None


def test_search_ladder_supports_all_merge_strategies() -> None:
    strict = PassageHit("strict", "wiki/a.md", "A", (), "", 1.0, "active", "", "wiki")
    qualified = PassageHit("strict", "wiki/a.md", "A", (), "", 2.0, "active", "", "wiki")
    relaxed = PassageHit("relaxed", "wiki/b.md", "B", (), "", 0.5, "active", "", "wiki")

    class Store:
        def search_fts(self, _query, *, mode, **_kwargs):
            return {"strict": [strict], "qualified_code": [qualified], "relaxed": [relaxed]}[mode]

    store = Store()
    merged, merged_mode = search_ladder(
        store,  # type: ignore[arg-type]
        steps=[LadderStep("strict", "strict", "q"), LadderStep("qualified", "qualified_code", "q")],
        merge="merge_by_passage",
        limit=10,
        swallow_index_errors=False,
    )
    assert merged_mode == "qualified_code"
    assert [(item.passage_id, item.score) for item in merged] == [("strict", 2.0)]

    replaced, replaced_mode = search_ladder(
        store,  # type: ignore[arg-type]
        steps=[LadderStep("strict", "strict", "q"), LadderStep("relaxed", "relaxed", "q", run_if_empty=True)],
        merge="replace_if_nonempty",
        limit=10,
        swallow_index_errors=False,
    )
    assert replaced_mode == "strict"
    assert replaced == [strict]

    descended, descended_mode = search_ladder(
        store,  # type: ignore[arg-type]
        steps=[LadderStep("strict", "strict", "q"), LadderStep("relaxed", "relaxed", "q")],
        merge="descend_if_empty",
        limit=10,
        swallow_index_errors=False,
    )
    assert descended_mode == "strict"
    assert descended == [strict]


def test_select_best_per_page_uses_passage_id_tie_break() -> None:
    first = _item("wiki/a.md", "z", 1.0)
    second = _item("wiki/a.md", "a", 1.0)
    assert select_best_per_page([first, second]) == [second]


def test_compose_score_matches_shared_formula() -> None:
    item = _item("wiki/concepts/a.md", "a", 2.0)
    hit = cast(PassageHit, item["hit"])
    metadata = {hit.page_path: {"updated_at": "2000-01-01"}}
    assert compose_score(
        hit,
        "invoice approval",
        "concept",
        "knowledge",
        metadata,
        base=hit.score,
        freshness=True,
        step_bonus=1.0,
    ) == round(hit.score + 0.35 + 1.0, 12)


def test_assembler_owns_stats_context_and_fallback_envelope() -> None:
    first = _item("wiki/concepts/a.md", "a-1", 8.0)
    second = _item("raw/sources/ref.md", "r-1", 7.0, source_kind="raw")
    store = FakeStore(cast(list[PassageHit], [first["hit"], second["hit"]]))

    result = assemble_recovery(
        [first, second],
        condition=RecoveryCondition("raw", ("wiki_zero_results",), "item"),
        candidates=[first, second],
        store=store,  # type: ignore[arg-type]
        raw_store=store,  # type: ignore[arg-type]
        cancellation=QueryCancellationContext.unbounded(),
    )

    assert result.hit_stats["wiki/concepts/a.md"]["max"] == 8.0
    assert result.pool_by_page["raw/sources/ref.md"][0]["hit"].passage_id == "r-1"
    assert result.fallback == {
        "level": "raw",
        "reasons": ["wiki_zero_results"],
        "allowed_source_paths": ["raw/sources/ref.md"],
    }
    assert [item["hit"].passage_id for item in result.context_items] == ["a-1", "r-1"]


def test_assembler_checks_cancellation_before_context_reads() -> None:
    context = QueryCancellationContext.with_timeout(0)
    store = FakeStore([])

    with pytest.raises(QueryCancelled) as error:
        assemble_recovery(
            [],
            condition=RecoveryCondition(),
            candidates=[],
            store=store,  # type: ignore[arg-type]
            cancellation=context,
        )

    assert error.value.cancelled_stage == "fallback"
    assert store.calls == []


def test_page_context_stops_at_the_page_token_budget() -> None:
    selected_hit = PassageHit("selected", "wiki/a.md", "A", (), "selected", 1.0, "active", "high", "wiki")
    fill_hit = PassageHit("fill", "wiki/a.md", "A", (), "word " * PAGE_TOKEN_BUDGET, 0.5, "active", "high", "wiki")
    overflow_hit = PassageHit("overflow", "wiki/a.md", "A", (), "overflow", 0.4, "active", "high", "wiki")
    store = FakeStore([selected_hit, fill_hit, overflow_hit])
    selected = [candidate_item(selected_hit, score=1.0, fts_rank=1)]

    context = _build_page_ordered_context(
        selected,
        store,  # type: ignore[arg-type]
        raw_store=None,
        page_stats={"wiki/a.md": {"max": 1.0}},
        page_candidates={"wiki/a.md": selected},
        cancellation=QueryCancellationContext.unbounded(),
    )

    assert [item["hit"].passage_id for item in context] == ["selected", "fill"]


def test_page_context_uses_ratio_boundary_and_weak_hit_limit() -> None:
    top_hit = PassageHit("top", "wiki/top.md", "Top", (), "top", 1.0, "active", "high", "wiki")
    boundary_hit = PassageHit("boundary", "wiki/boundary.md", "Boundary", (), "boundary", 0.6, "active", "high", "wiki")
    weak_hits = [
        PassageHit(f"weak-{index}", "wiki/weak.md", "Weak", (), f"weak {index}", 0.599 - index / 100, "active", "high", "wiki")
        for index in range(4)
    ]
    store = FakeStore([top_hit, boundary_hit, *weak_hits])
    top = candidate_item(top_hit, score=1.0, fts_rank=1)
    boundary = candidate_item(boundary_hit, score=0.6, fts_rank=1)
    weak = [candidate_item(hit, score=0.599 - index / 100, fts_rank=index + 1) for index, hit in enumerate(weak_hits)]

    context = _build_page_ordered_context(
        [top, boundary, weak[0]],
        store,  # type: ignore[arg-type]
        raw_store=None,
        page_stats={"wiki/top.md": {"max": 1.0}, "wiki/boundary.md": {"max": 0.6}, "wiki/weak.md": {"max": 0.599}},
        page_candidates={"wiki/top.md": [top], "wiki/boundary.md": [boundary], "wiki/weak.md": weak},
        cancellation=QueryCancellationContext.unbounded(),
    )

    assert [item["hit"].passage_id for item in context].count("weak-0") == 1
    assert sum(item["hit"].page_path == "wiki/weak.md" for item in context) == 3
    assert store.calls == [(["wiki/top.md"], 500), (["wiki/boundary.md"], 500)]


def test_assembler_rejects_retired_explicit_maps_path() -> None:
    parameters = inspect.signature(assemble_recovery).parameters
    assert "candidates" in parameters
    assert parameters["candidates"].default is inspect.Parameter.empty
    assert "hit_stats" not in parameters
    assert "pool_by_page" not in parameters

    with pytest.raises(TypeError):
        assemble_recovery(
            [],
            condition=RecoveryCondition(),
            store=FakeStore([]),  # type: ignore[arg-type]
            cancellation=QueryCancellationContext.unbounded(),
            hit_stats={},  # type: ignore[call-arg]
        )


def test_snapshot_captures_metadata_once_and_is_immutable() -> None:
    pages = [
        {
            "path": "wiki/concepts/one.md",
            "title": "One",
            "frontmatter": {"tags": ["a"]},
            "source_kind": "wiki",
            "corpus": "knowledge",
            "authority": "high",
            "content_hash": "hash",
        }
    ]

    class Store:
        scope = "active"

        def __init__(self) -> None:
            self.calls = 0

        def page_candidates(self):
            self.calls += 1
            return pages

    store = Store()
    snapshot = QueryCorpusSnapshot.capture(store, cancellation=QueryCancellationContext.unbounded())  # type: ignore[arg-type]

    assert store.calls == 1
    assert snapshot.pages[0]["path"] == "wiki/concepts/one.md"
    with pytest.raises(TypeError):
        snapshot.metadata["new"] = {}  # type: ignore[index]

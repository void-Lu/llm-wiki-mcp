from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from itertools import product
from pathlib import Path
from typing import Literal, cast

import pytest

from retrieval.candidate_items import candidate_item
from retrieval.body_budget import PAGE_TOKEN_BUDGET
from retrieval.query_cancellation import QueryCancelled, QueryCancellationContext
from retrieval.query_recovery import (
    FallbackState,
    LadderStep,
    RecoveryCondition,
    assemble_recovery,
    compose_score,
    fallback_envelope,
    fusion_score,
    plan_fallback,
    search_ladder,
    select_best_per_page,
    step_bonus,
    _build_page_ordered_context,
)
from retrieval.retrieval_index import PassageHit


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


def test_fallback_envelope_has_stable_shape() -> None:
    expected = {
        "level": "raw",
        "reasons": ["wiki_zero_results"],
        "allowed_source_paths": ["raw/a.md"],
    }

    assert fallback_envelope("raw", ("wiki_zero_results",), ("raw/a.md",)) == expected
    assert fallback_envelope("none") == {
        "level": "none",
        "reasons": [],
        "allowed_source_paths": [],
    }


def _state(
    *,
    has_primary_recall: bool,
    effective_scope: str,
    uncovered_latin_terms: tuple[str, ...],
    wiki_relaxed_answered: bool,
    raw_available: Literal["unknown", "yes", "no"],
    relaxed_available: bool,
) -> FallbackState:
    return FallbackState(
        has_primary_recall=has_primary_recall,
        effective_scope=effective_scope,
        uncovered_latin_terms=uncovered_latin_terms,
        wiki_relaxed_answered=wiki_relaxed_answered,
        raw_available=raw_available,
        relaxed_available=relaxed_available,
    )


def test_fallback_state_is_frozen_and_has_six_evidence_fields() -> None:
    state = _state(
        has_primary_recall=False,
        effective_scope="all",
        uncovered_latin_terms=("rag",),
        wiki_relaxed_answered=True,
        raw_available="unknown",
        relaxed_available=True,
    )

    assert state == FallbackState(
        False,
        "all",
        ("rag",),
        True,
        "unknown",
        True,
    )
    assert state.__dataclass_fields__.keys() == {
        "has_primary_recall",
        "effective_scope",
        "uncovered_latin_terms",
        "wiki_relaxed_answered",
        "raw_available",
        "relaxed_available",
    }
    with pytest.raises(FrozenInstanceError):
        state.raw_available = "yes"  # type: ignore[misc]


def test_plan_fallback_covers_each_branch_and_none_path() -> None:
    cases = [
        (
            _state(
                has_primary_recall=True,
                effective_scope="knowledge",
                uncovered_latin_terms=("rag",),
                wiki_relaxed_answered=False,
                raw_available="unknown",
                relaxed_available=False,
            ),
            "coverage",
        ),
        (
            _state(
                has_primary_recall=False,
                effective_scope="knowledge",
                uncovered_latin_terms=(),
                wiki_relaxed_answered=False,
                raw_available="no",
                relaxed_available=True,
            ),
            "wiki_relaxed",
        ),
        (
            _state(
                has_primary_recall=False,
                effective_scope="all",
                uncovered_latin_terms=("llm",),
                wiki_relaxed_answered=True,
                raw_available="unknown",
                relaxed_available=True,
            ),
            "all_coverage",
        ),
        (
            _state(
                has_primary_recall=False,
                effective_scope="all",
                uncovered_latin_terms=(),
                wiki_relaxed_answered=False,
                raw_available="yes",
                relaxed_available=False,
            ),
            "raw_zero",
        ),
    ]

    for state, branch in cases:
        plan = plan_fallback(state)
        assert plan is not None
        assert plan.branch == branch
    assert plan_fallback(
        _state(
            has_primary_recall=True,
            effective_scope="knowledge",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            raw_available="no",
            relaxed_available=False,
        )
    ) is None


def test_plan_fallback_exhausts_the_decision_matrix() -> None:
    scopes = ("knowledge", "all", "history", "raw", "archive")
    cases = list(
        product(
            (False, True),
            scopes,
            ((), ("rag",)),
            (False, True),
            ("unknown", "yes", "no"),
            (False, True),
        )
    )
    cases.extend(
        product(
            (False, True),
            scopes,
            (("rag", "llm"), ("N", "record")),
            (False, True),
            ("yes", "no"),
            (False,),
        )
    )
    assert len(cases) == 320

    for (
        has_primary_recall,
        effective_scope,
        uncovered,
        wiki_relaxed_answered,
        raw_available,
        relaxed_available,
    ) in cases:
        state = _state(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=uncovered,
            wiki_relaxed_answered=wiki_relaxed_answered,
            raw_available=raw_available,
            relaxed_available=relaxed_available,
        )
        plan = plan_fallback(state)
        raw_ready = raw_available in {"unknown", "yes"}
        if has_primary_recall and uncovered and effective_scope in {"knowledge", "all"}:
            assert plan is not None and plan.branch == "coverage"
        elif not has_primary_recall and effective_scope == "all" and wiki_relaxed_answered and uncovered and raw_ready:
            assert plan is not None and plan.branch == "all_coverage"
        elif not has_primary_recall and effective_scope in {"knowledge", "all"} and relaxed_available:
            assert plan is not None and plan.branch == "wiki_relaxed"
        elif not has_primary_recall and not wiki_relaxed_answered and effective_scope in {"knowledge", "all"} and raw_ready:
            assert plan is not None and plan.branch == "raw_zero"
        else:
            assert plan is None


def test_fallback_state_progression_primary_recall_track_keeps_rung_gate() -> None:
    states = (
        _state(
            has_primary_recall=True,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        ),
        _state(
            has_primary_recall=True,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=True,
        ),
        _state(
            has_primary_recall=True,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=True,
            raw_available="unknown",
            relaxed_available=True,
        ),
        _state(
            has_primary_recall=True,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=True,
            raw_available="unknown",
            relaxed_available=True,
        ),
    )

    plans = [plan_fallback(state) for state in states]
    assert [plan.branch if plan is not None else None for plan in plans] == [
        "coverage",
        "coverage",
        "coverage",
        "coverage",
    ]
    assert plans[0] is not None and plans[0].branch == "coverage"
    assert plans[1] is not None and plans[1].branch != "wiki_relaxed"
    assert plans[2] is not None and plans[2].branch != "all_coverage"
    assert plans[3] is not None and plans[3].branch != "raw_zero"


def test_fallback_state_progression_no_recall_track_keeps_rung_gate() -> None:
    states = (
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        ),
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=True,
        ),
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=True,
            raw_available="unknown",
            relaxed_available=True,
        ),
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=True,
            raw_available="unknown",
            relaxed_available=True,
        ),
    )

    plans = [plan_fallback(state) for state in states]
    assert [plan.branch if plan is not None else None for plan in plans] == [
        "raw_zero",
        "wiki_relaxed",
        "all_coverage",
        "wiki_relaxed",
    ]
    assert plans[0] is not None and plans[0].branch != "coverage"
    assert plans[1] is not None and plans[1].branch == "wiki_relaxed"
    assert plans[2] is not None and plans[2].branch == "all_coverage"
    assert plans[3] is not None and plans[3].branch != "raw_zero"


def test_raw_availability_three_state_gate_is_explicit() -> None:
    all_coverage_unknown = plan_fallback(
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=True,
            raw_available="unknown",
            relaxed_available=False,
        )
    )
    all_coverage_yes = plan_fallback(
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=True,
            raw_available="yes",
            relaxed_available=False,
        )
    )
    all_coverage_no = plan_fallback(
        _state(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=("record",),
            wiki_relaxed_answered=True,
            raw_available="no",
            relaxed_available=False,
        )
    )
    assert all_coverage_unknown is not None and all_coverage_unknown.branch == "all_coverage"
    assert all_coverage_yes is not None and all_coverage_yes.branch == "all_coverage"
    assert all_coverage_no is None

    raw_zero_unknown = plan_fallback(
        _state(
            has_primary_recall=False,
            effective_scope="knowledge",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        )
    )
    raw_zero_no = plan_fallback(
        _state(
            has_primary_recall=False,
            effective_scope="knowledge",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            raw_available="no",
            relaxed_available=False,
        )
    )
    assert raw_zero_unknown is not None and raw_zero_unknown.branch == "raw_zero"
    assert raw_zero_no is None


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


def test_fusion_score_matches_rrf_scaling_and_exact_formula() -> None:
    hit = PassageHit("p-1", "wiki/concepts/target.md", "Target", (), "", 0.0, "active", "high", "wiki")
    metadata: dict[str, dict[str, object]] = {}

    exact_item = {"fts_rank": 1, "title_rank": 0, "vector_rank": None}
    exact_rrf = 1 / 10
    exact_result = fusion_score(
        hit,
        "target",
        "concept",
        "knowledge",
        metadata,
        exact_item,
        effective_rrf_k=9,
    )
    assert exact_result == {
        "score": compose_score(
            hit,
            "target",
            "concept",
            "knowledge",
            metadata,
            rrf=exact_rrf * 10,
            exact=0.5,
        ),
        "rrf": exact_rrf,
        "exact": True,
    }

    non_exact_item = {"fts_rank": 0, "title_rank": None, "vector_rank": 2}
    non_exact_rrf = 1 / 11
    non_exact_result = fusion_score(
        hit,
        "different",
        "concept",
        "knowledge",
        metadata,
        non_exact_item,
        effective_rrf_k=9,
    )
    assert non_exact_result == {
        "score": compose_score(
            hit,
            "different",
            "concept",
            "knowledge",
            metadata,
            rrf=non_exact_rrf * 10,
            exact=0.0,
        ),
        "rrf": non_exact_rrf,
        "exact": False,
    }


def test_stepbonus_is_public_for_pipeline_consumers() -> None:
    assert step_bonus(0) == 0.0
    assert step_bonus(5) == 4.0


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

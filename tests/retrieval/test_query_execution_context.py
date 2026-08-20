from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_execution_context import (
    QueryExecutionContext,
    QueryExecutionView,
    QueryFilters,
    QueryRequestView,
)
from retrieval.query_recall_policy import RRF_K
from retrieval.query_recovery import (
    DEFAULT_RECOVERY_CONDITION,
    FallbackState,
    assemble_recovery,
    plan_fallback,
)
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import RetrievalIndexStore
from wiki.wiki_index import refresh_indexes
from wiki.wiki_paths import create_wiki_root


def _context(root: Path) -> QueryExecutionContext:
    store = RetrievalIndexStore(root)
    cancellation = QueryCancellationContext.unbounded()
    context = QueryExecutionContext(root, store, cancellation, store.status())
    context.recovery = assemble_recovery(
        [],
        condition=DEFAULT_RECOVERY_CONDITION,
        candidates=[],
        store=store,
        cancellation=cancellation,
    )
    return context


def test_query_execution_context_runs_parameterized_raw_branches(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/evidence.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Retrieval Evidence\n---\n\nRAG LLM access reports evidence.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    context = _context(root)
    store = context.store
    cancellation = context.cancellation

    coverage_plan = plan_fallback(
        FallbackState(
            has_primary_recall=True,
            effective_scope="knowledge",
            uncovered_latin_terms=("rag",),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        )
    )
    assert coverage_plan is not None and coverage_plan.branch == "coverage"
    context.uncovered_latin_terms = ["rag"]
    context.run_raw_branch(
        QueryRequestView(
            question="RAG",
            effective_scope="knowledge",
            project=None,
            filters=QueryFilters(),
            top_k=2,
            metadata={},
            intent="concept",
            effective_rrf_k=RRF_K,
        ),
        branch="coverage",
        extra_terms=[],
        term_variants={},
        candidate_pool=[],
        plan=coverage_plan,
    )
    coverage_path = context.selected[0]["hit"].page_path
    coverage_reasons = context.recovery.fallback["reasons"]

    all_coverage_plan = plan_fallback(
        FallbackState(
            has_primary_recall=False,
            effective_scope="all",
            uncovered_latin_terms=("llm",),
            wiki_relaxed_answered=True,
            raw_available="unknown",
            relaxed_available=True,
        )
    )
    assert all_coverage_plan is not None and all_coverage_plan.branch == "all_coverage"
    context.selected = []
    context.uncovered_latin_terms = ["llm"]
    context.lexical_mode = "relaxed"
    context.coverage_fallback = False
    context.run_raw_branch(
        QueryRequestView(
            question="LLM",
            effective_scope="all",
            project=None,
            filters=QueryFilters(),
            top_k=2,
            metadata={},
            intent="concept",
            effective_rrf_k=RRF_K,
        ),
        branch="all_coverage",
        extra_terms=["retrieval"],
        term_variants={"llm": ["LLM"]},
        candidate_pool=[],
        plan=all_coverage_plan,
    )
    all_coverage_path = context.selected[0]["hit"].page_path
    all_coverage_reasons = context.recovery.fallback["reasons"]

    raw_zero_plan = plan_fallback(
        FallbackState(
            has_primary_recall=False,
            effective_scope="knowledge",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        )
    )
    assert raw_zero_plan is not None and raw_zero_plan.branch == "raw_zero"
    context.selected = []
    context.uncovered_latin_terms = []
    context.lexical_mode = "strict"
    context.coverage_fallback = False
    context.run_raw_branch(
        QueryRequestView(
            question="access reports",
            effective_scope="knowledge",
            project=None,
            filters=QueryFilters(),
            top_k=2,
            metadata={},
            intent="concept",
            effective_rrf_k=RRF_K,
        ),
        branch="raw_zero",
        extra_terms=[],
        term_variants={},
        candidate_pool=[],
        plan=raw_zero_plan,
    )
    raw_zero_path = context.selected[0]["hit"].page_path
    raw_zero_reasons = context.recovery.fallback["reasons"]

    expected_path = "raw/sources/file/default/evidence.md"
    assert coverage_path == expected_path
    assert all_coverage_path == expected_path
    assert raw_zero_path == expected_path
    assert coverage_reasons == ["wiki_primary_missing_latin_coverage"]
    assert all_coverage_reasons == ["wiki_primary_missing_latin_coverage"]
    assert raw_zero_reasons == ["wiki_zero_results"]
    assert store is context.store
    assert cancellation is context.cancellation


def test_query_execution_context_memoizes_raw_store_and_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/evidence.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("memoized raw snapshot marker", encoding="utf-8")
    refresh_indexes(root)

    context = _context(root)
    raw_store = context.get_raw_store()
    first = context.capture_raw_snapshot()
    second = context.capture_raw_snapshot()

    assert raw_store is context.get_raw_store()
    assert first is second
    assert first.scope == "raw"
    assert first.pages


@pytest.mark.parametrize(
    ("case", "expected"),
    [("fresh", "fresh"), ("stale", "stale"), ("missing", "missing")],
)
def test_query_execution_context_normalizes_raw_availability_to_three_states(
    tmp_path: Path, case: str, expected: str
) -> None:
    root = tmp_path / case
    create_wiki_root(root)
    if case != "missing":
        raw = root / "raw/sources/file/default/evidence.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"{case} raw marker", encoding="utf-8")
        refresh_indexes(root)

    context = _context(root)
    if case == "stale":
        context.get_raw_store().mark_stale()

    assert context.raw_availability() == expected


def test_query_execution_context_outcome_freezes_state_and_is_read_once(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    context = _context(root)
    context.selected = [{"score": 1.0}]
    context.raw_fts_hits = 2
    context.coverage_fallback = True

    outcome = context.outcome()

    assert outcome is context.outcome()
    assert isinstance(outcome, QueryExecutionView)
    assert outcome.selected == ({"score": 1.0},)
    assert outcome.raw_fts_hits == 2
    assert outcome.coverage_fallback is True
    with pytest.raises(TypeError):
        outcome.status["code"] = "changed"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="sealed"):
        context.raw_fts_hits = 3


def test_query_execution_context_outcome_does_not_open_lazy_raw_store(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    context = _context(root)

    context.outcome()

    assert context.raw_store is None


def test_query_request_view_is_frozen_at_the_execution_seam() -> None:
    view = QueryRequestView(
        question="invoice approval",
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(path_prefix="wiki/concepts/"),
        top_k=2,
        metadata={"wiki/concepts/invoice.md": {"tags": ["finance"]}},
        intent="concept",
        effective_rrf_k=RRF_K,
        expansion_terms={"invoice": ["billing"]},
    )

    with pytest.raises(FrozenInstanceError):
        view.question = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        view.metadata["new"] = {}  # type: ignore[index]
    assert view.expansion_terms == {"invoice": ("billing",)}


def test_execute_owns_the_fallback_then_discovery_batch_order(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    context = _context(root)
    context.snapshot = QueryCorpusSnapshot.empty("active")
    events: list[str] = []
    monkeypatch.setattr(
        context,
        "_run_fallback_recovery",
        lambda _view: events.append("fallback"),
    )
    monkeypatch.setattr(
        context,
        "_run_discovery_and_batch",
        lambda _view: events.append("discovery_batch"),
    )

    view = context.execute(
        QueryRequestView(
            question="invoice approval",
            effective_scope="knowledge",
            project=None,
            filters=QueryFilters(),
            top_k=2,
            metadata={},
            intent="concept",
            effective_rrf_k=RRF_K,
        )
    )

    assert events == ["fallback", "discovery_batch"]
    assert view.recovery is context.recovery

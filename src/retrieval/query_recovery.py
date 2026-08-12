"""Shared recovery-state and context assembly for Query V2."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from common.fallback_policy import FallbackDecision
from retrieval.context_packer import estimate_tokens
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.retrieval_index import RetrievalIndexStore


PAGE_FILL_LIMIT = 500
PAGE_TOKEN_BUDGET = 2_400
PAGE_FULL_FILL_MIN_RATIO = 0.6
PAGE_WEAK_HIT_LIMIT = 3


@dataclass(frozen=True)
class RecoveryCondition:
    """The branch-specific evidence decision supplied by the pipeline."""

    level: Literal["none", "raw"] = "none"
    reasons: tuple[str, ...] = ()
    stats_score: Literal["hit", "item"] = "hit"


@dataclass(frozen=True)
class RecoveryAssembly:
    """The complete state needed by the remainder of a query."""

    selected: list[dict[str, Any]]
    hit_stats: dict[str, dict[str, Any]]
    pool_by_page: dict[str, list[dict[str, Any]]]
    context_items: list[dict[str, Any]]
    fallback: dict[str, Any]


def _build_page_ordered_context(
    selected: Sequence[Mapping[str, Any]],
    store: RetrievalIndexStore,
    *,
    raw_store: RetrievalIndexStore | None,
    hit_stats: Mapping[str, Mapping[str, Any]],
    pool_by_page: Mapping[str, Sequence[Mapping[str, Any]]],
    cancellation: QueryCancellationContext,
) -> list[dict[str, Any]]:
    """Build the bounded, page-ordered context pack without changing ranking."""

    top_score = max((float(stats.get("max", 0.0)) for stats in hit_stats.values()), default=0.0)
    guaranteed: list[dict[str, Any]] = []
    deep_fill: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, sel in enumerate(selected):
        cancellation.checkpoint_batch(index, every=1, stage="context")
        hit = sel["hit"]
        page_path = hit.page_path
        pool = pool_by_page.get(page_path, ())
        if not pool:
            continue
        best = max(pool, key=lambda item: item["score"])
        if best["hit"].passage_id not in seen_ids:
            seen_ids.add(best["hit"].passage_id)
            guaranteed.append(dict(best))
    for index, sel in enumerate(selected):
        cancellation.checkpoint_batch(index, every=1, stage="context")
        hit = sel["hit"]
        page_path = hit.page_path
        stats = hit_stats.get(page_path)
        strong_page = (
            stats is None
            or top_score <= 0
            or float(stats.get("max", 0.0)) >= top_score * PAGE_FULL_FILL_MIN_RATIO
        )
        if not strong_page:
            for item in list(pool_by_page.get(page_path, ()))[:PAGE_WEAK_HIT_LIMIT]:
                if item["hit"].passage_id not in seen_ids:
                    seen_ids.add(item["hit"].passage_id)
                    deep_fill.append(dict(item))
            continue
        page_store = raw_store if raw_store is not None and page_path.startswith("raw/") else store
        cancellation.checkpoint("context")
        page_hits = page_store.passages_for_pages([page_path], limit_per_page=PAGE_FILL_LIMIT)
        page_tokens = 0
        for hit_index, page_hit in enumerate(page_hits):
            cancellation.checkpoint_batch(hit_index, every=16, stage="context")
            if page_hit.passage_id in seen_ids:
                continue
            item_tokens = estimate_tokens(page_hit.text)
            if page_tokens + item_tokens > PAGE_TOKEN_BUDGET:
                break
            page_tokens += item_tokens
            seen_ids.add(page_hit.passage_id)
            deep_fill.append(
                {
                    "hit": page_hit,
                    "fts_rank": sel.get("fts_rank"),
                    "title_rank": None,
                    "vector_rank": None,
                    "vector_score": 0.0,
                    "rrf": 0.0,
                    "exact": False,
                    "graph_score": 0.0,
                    "graph_reasons": [],
                    "score": sel.get("score", 0.0),
                }
            )
    cancellation.checkpoint("context")
    return guaranteed + deep_fill


def assemble_recovery(
    selected: Sequence[Mapping[str, Any]],
    *,
    condition: RecoveryCondition,
    store: RetrievalIndexStore,
    cancellation: QueryCancellationContext,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    raw_store: RetrievalIndexStore | None = None,
    hit_stats: Mapping[str, Mapping[str, Any]] | None = None,
    pool_by_page: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> RecoveryAssembly:
    """Assemble one recovery state from recall candidates.

    ``candidates`` is the preferred input and makes page statistics/pools a
    single owner.  The explicit maps remain accepted for callers that already
    have a prepared projection and keep this seam useful for focused tests.
    """

    cancellation.checkpoint("fallback")
    normalized_selected = [dict(item) for item in selected]
    if candidates is not None:
        normalized_candidates = [dict(item) for item in candidates]
        built_stats: dict[str, dict[str, Any]] = {}
        built_pool: dict[str, list[dict[str, Any]]] = {}
        for index, item in enumerate(normalized_candidates):
            cancellation.checkpoint_batch(index, every=16, stage="fallback")
            page_path = item["hit"].page_path
            built_pool.setdefault(page_path, []).append(item)
            stats = built_stats.setdefault(
                page_path,
                {"max": 0.0, "store": "raw" if page_path.startswith("raw/") else "active"},
            )
            value = item["score"] if condition.stats_score == "item" else item["hit"].score
            stats["max"] = max(float(stats["max"]), float(value))
        normalized_stats = built_stats
        normalized_pool = built_pool
    else:
        normalized_stats = {path: dict(stats) for path, stats in (hit_stats or {}).items()}
        normalized_pool = {
            path: [dict(item) for item in items]
            for path, items in (pool_by_page or {}).items()
        }
    context_items = _build_page_ordered_context(
        normalized_selected,
        store,
        raw_store=raw_store,
        hit_stats=normalized_stats,
        pool_by_page=normalized_pool,
        cancellation=cancellation,
    )
    raw_paths = [
        item["hit"].page_path
        for item in normalized_selected
        if item["hit"].source_kind == "raw"
    ]
    fallback_level = condition.level if condition.level != "none" and raw_paths else "none"
    fallback = {
        **FallbackDecision(
            fallback_level,
            tuple(condition.reasons) if fallback_level != "none" else (),
            tuple(raw_paths) if fallback_level == "raw" else (),
        ).as_dict(),
        "added_token_usage": 0,
    }
    cancellation.checkpoint("fallback")
    return RecoveryAssembly(
        selected=normalized_selected,
        hit_stats=normalized_stats,
        pool_by_page=normalized_pool,
        context_items=context_items,
        fallback=fallback,
    )

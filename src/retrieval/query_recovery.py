"""Shared recovery-state and context assembly for Query V2."""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from retrieval.context_packer import estimate_response_tokens
from retrieval.candidate_items import candidate_item
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore


PAGE_FILL_LIMIT = 500
PAGE_TOKEN_BUDGET = 2_400
PAGE_FULL_FILL_MIN_RATIO = 0.6
PAGE_WEAK_HIT_LIMIT = 3
FRESHNESS_BONUS_MAX = 12.0
FRESHNESS_DECAY_DAYS = 90
STEP_BONUS_MAX = 4.0
STEP_COUNT_FULL = 5
FallbackLevel = Literal["none", "raw"]
FallbackBranch = Literal["main", "coverage", "wiki_relaxed", "all_coverage", "raw_zero"]
CandidatesKind = Literal["scored", "combined_active_raw", "wiki_relaxed", "raw_only"]


def fallback_envelope(
    level: FallbackLevel,
    reasons: tuple[str, ...] = (),
    allowed_source_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build the one public fallback envelope used by Query V2."""

    return {
        "level": level,
        "reasons": list(reasons),
        "allowed_source_paths": list(allowed_source_paths),
    }


@dataclass(frozen=True)
class FallbackDecision:
    level: FallbackLevel
    reasons: tuple[str, ...] = ()
    allowed_source_paths: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return fallback_envelope(self.level, self.reasons, self.allowed_source_paths)


@dataclass(frozen=True)
class FallbackPlan:
    branch: FallbackBranch
    condition: "RecoveryCondition"
    lexical_mode: str | None
    candidates_kind: CandidatesKind


@dataclass(frozen=True)
class LadderStep:
    """One bounded FTS attempt in a recovery/search ladder."""

    name: str
    mode: str
    query: str
    kwargs: Mapping[str, object] = field(default_factory=dict)
    run_if_empty: bool = False
    ignore_errors: bool = False
    searcher: Callable[[RetrievalIndexStore, "LadderStep", int], list[PassageHit]] | None = None


def plan_fallback(
    *,
    has_primary_recall: bool,
    effective_scope: str,
    uncovered_latin_terms: list[str],
    wiki_relaxed_answered: bool,
    raw_available: bool,
    relaxed_available: bool,
) -> FallbackPlan | None:
    """Return the pure fallback branch selected by current evidence."""

    if (
        has_primary_recall
        and uncovered_latin_terms
        and effective_scope in {"knowledge", "all"}
    ):
        return FallbackPlan(
            "coverage",
            RecoveryCondition("raw", ("wiki_primary_missing_latin_coverage",), "item"),
            None,
            "combined_active_raw",
        )
    if (
        not has_primary_recall
        and effective_scope == "all"
        and wiki_relaxed_answered
        and uncovered_latin_terms
        and raw_available
    ):
        return FallbackPlan(
            "all_coverage",
            RecoveryCondition("raw", ("wiki_primary_missing_latin_coverage",), "item"),
            None,
            "combined_active_raw",
        )
    if (
        not has_primary_recall
        and effective_scope in {"knowledge", "all"}
        and relaxed_available
    ):
        return FallbackPlan("wiki_relaxed", RecoveryCondition(), "relaxed", "wiki_relaxed")
    if (
        not has_primary_recall
        and not wiki_relaxed_answered
        and effective_scope in {"knowledge", "all"}
        and raw_available
    ):
        return FallbackPlan(
            "raw_zero",
            RecoveryCondition("raw", ("wiki_zero_results",), "item"),
            "raw",
            "raw_only",
        )
    return None


def resolve_fallback_lexical_mode(
    plan: FallbackPlan | None,
    raw_lexical_mode: str | None = None,
    *,
    qualified_identifier: bool = False,
) -> str | None:
    """Translate a selected plan into the public lexical-mode value."""

    if plan is None or plan.lexical_mode is None:
        return None
    if plan.lexical_mode != "raw":
        return plan.lexical_mode
    raw_modes = {
        "qualified_code": "qualified_code" if qualified_identifier else "strict",
        "identifier_phrase": "identifier_phrase",
        "raw_prefix": "raw_prefix",
        "relaxed": "relaxed",
    }
    return raw_modes.get(raw_lexical_mode or "", "strict")


def _ladder_step(value: LadderStep | tuple[str, str, str]) -> LadderStep:
    if isinstance(value, LadderStep):
        return value
    name, mode, query = value
    return LadderStep(name, mode, query)


def search_ladder(
    store: RetrievalIndexStore,
    *,
    steps: Sequence[LadderStep | tuple[str, str, str]],
    merge: Literal["merge_by_passage", "replace_if_nonempty", "descend_if_empty"],
    limit: int,
    swallow_index_errors: bool,
    counts: MutableMapping[str, int] | None = None,
) -> tuple[list[PassageHit], str]:
    """Run a bounded search ladder with explicit merge and error semantics."""

    normalized_steps = [_ladder_step(step) for step in steps]
    selected: list[PassageHit] = []
    selected_mode = "strict"
    successful_mode = "strict"
    for step in normalized_steps:
        if merge == "replace_if_nonempty" and step.run_if_empty and selected:
            continue
        try:
            if step.searcher is not None:
                hits = step.searcher(store, step, limit)
            else:
                hits = store.search_fts(
                    step.query,
                    limit=limit,
                    mode=step.mode,
                    **dict(step.kwargs),
                )
        except RetrievalIndexError as exc:
            if step.ignore_errors:
                continue
            if not swallow_index_errors:
                raise
            return [], f"error:{exc.code}"
        if counts is not None:
            counts[step.name] = counts.get(step.name, 0) + len(hits)
        if hits:
            successful_mode = step.mode
        if merge == "descend_if_empty":
            if hits:
                return hits, step.mode
            continue
        if merge == "replace_if_nonempty":
            if hits:
                selected = hits
                selected_mode = step.mode
            continue
        if merge == "merge_by_passage":
            selected_by_id = {hit.passage_id: hit for hit in selected}
            for hit in hits:
                current = selected_by_id.get(hit.passage_id)
                if current is None or hit.score > current.score:
                    selected_by_id[hit.passage_id] = hit
            selected = sorted(
                selected_by_id.values(),
                key=lambda hit: (-hit.score, hit.page_path, hit.passage_id),
            )
            continue
        raise ValueError(f"unsupported ladder merge strategy: {merge}")
    if merge == "merge_by_passage":
        return selected, successful_mode if selected else selected_mode
    return selected, selected_mode


def select_best_per_page(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep one deterministic, highest-scoring passage for each page."""

    best: dict[str, dict[str, Any]] = {}
    for raw_item in items:
        item = dict(raw_item)
        hit = item["hit"]
        path = hit.page_path
        current = best.get(path)
        if current is None or item["score"] > current["score"] or (
            item["score"] == current["score"]
            and hit.passage_id < current["hit"].passage_id
        ):
            best[path] = item
    return list(best.values())


def _freshness_bonus(hit: PassageHit, metadata: Mapping[str, Mapping[str, Any]]) -> float:
    frontmatter = metadata.get(hit.page_path, {})
    raw_date = str(frontmatter.get("updated_at") or frontmatter.get("created") or "")
    if not raw_date:
        return 0.0
    try:
        updated = datetime.date.fromisoformat(raw_date[:10])
    except ValueError:
        return 0.0
    days = (datetime.date.today() - updated).days
    if days < 0 or days > FRESHNESS_DECAY_DAYS:
        return 0.0
    return round(FRESHNESS_BONUS_MAX * (1 - days / FRESHNESS_DECAY_DAYS), 3)


def _authority_bonus(
    hit: PassageHit,
    intent: str,
    scope: str,
    metadata: Mapping[str, Mapping[str, Any]],
) -> float:
    values = {
        "formal_knowledge": 0.35,
        "concept": 0.35,
        "entity": 0.35,
        "project": 0.23,
        "raw": 0.0,
        "raw_chat": -0.15,
    }
    if hit.corpus == "history" or hit.source_kind == "raw_chat":
        authority = "raw_chat"
    elif "/entities/" in hit.page_path:
        authority = "entity"
    elif "/projects/" in hit.page_path:
        authority = "project"
    elif "/concepts/" in hit.page_path:
        authority = "concept"
    else:
        authority = "raw" if hit.source_kind.startswith("raw") else "formal_knowledge"
    bonus = values.get(authority, 0.0)
    if intent == "history" and hit.corpus == "history":
        bonus += 0.25
    if scope == "history" and hit.corpus == "history":
        bonus += 0.15
    if str(metadata.get(hit.page_path, {}).get("freshness") or "fresh") in {"stale", "review_required"}:
        bonus -= 0.20
    return bonus


def _title_overlap_bonus(hit: PassageHit, question: str) -> float:
    question_terms = {
        term.casefold() for term in re.findall(r"[\w一-鿿]+", question) if len(term) > 1
    }
    title_terms = {
        term.casefold() for term in re.findall(r"[\w一-鿿]+", hit.title) if len(term) > 1
    }
    if not question_terms or not title_terms:
        overlap_bonus = 0.0
    else:
        overlap_bonus = 0.15 * len(question_terms & title_terms) / len(question_terms)
    latin_terms = re.findall(r"[a-z0-9_]+", question.casefold())
    compact_title = re.sub(r"[^a-z0-9_]+", "", hit.title.casefold())
    if any(
        len(left + right) >= 5 and left + right in compact_title
        for left, right in zip(latin_terms, latin_terms[1:])
    ):
        return overlap_bonus + 2.0
    return overlap_bonus


def _step_bonus(count: int) -> float:
    return STEP_BONUS_MAX * min(count, STEP_COUNT_FULL) / STEP_COUNT_FULL


def compose_score(
    hit: PassageHit,
    question: str,
    intent: str,
    effective_scope: str,
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    base: float = 0.0,
    rrf: float | None = None,
    exact: float = 0.0,
    freshness: bool = False,
    step_bonus: float = 0.0,
) -> float:
    """Compose shared ranking signals while keeping raw ranking independent."""

    return round(
        base
        + (rrf or 0.0)
        + _authority_bonus(hit, intent, effective_scope, metadata)
        + _title_overlap_bonus(hit, question)
        + exact
        + (_freshness_bonus(hit, metadata) if freshness else 0.0)
        + step_bonus,
        12,
    )


@dataclass(frozen=True)
class RecoveryCondition:
    """The branch-specific evidence decision supplied by the pipeline."""

    level: Literal["none", "raw"] = "none"
    reasons: tuple[str, ...] = ()
    stats_score: Literal["hit", "item"] = "hit"


DEFAULT_RECOVERY_CONDITION = RecoveryCondition()


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
    page_stats: Mapping[str, Mapping[str, Any]],
    page_candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    cancellation: QueryCancellationContext,
) -> list[dict[str, Any]]:
    """Build the bounded, page-ordered context pack without changing ranking."""

    top_score = max((float(stats.get("max", 0.0)) for stats in page_stats.values()), default=0.0)
    guaranteed: list[dict[str, Any]] = []
    deep_fill: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, sel in enumerate(selected):
        cancellation.checkpoint_batch(index, every=1, stage="context")
        hit = sel["hit"]
        page_path = hit.page_path
        pool = page_candidates.get(page_path, ())
        if not pool:
            continue
        best_items = select_best_per_page(pool)
        if not best_items:
            continue
        best = best_items[0]
        if best["hit"].passage_id not in seen_ids:
            seen_ids.add(best["hit"].passage_id)
            guaranteed.append(dict(best))
    for index, sel in enumerate(selected):
        cancellation.checkpoint_batch(index, every=1, stage="context")
        hit = sel["hit"]
        page_path = hit.page_path
        stats = page_stats.get(page_path)
        strong_page = (
            stats is None
            or top_score <= 0
            or float(stats.get("max", 0.0)) >= top_score * PAGE_FULL_FILL_MIN_RATIO
        )
        if not strong_page:
            for item in list(page_candidates.get(page_path, ()))[:PAGE_WEAK_HIT_LIMIT]:
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
            item_tokens = estimate_response_tokens(page_hit.text)
            if page_tokens + item_tokens > PAGE_TOKEN_BUDGET:
                break
            page_tokens += item_tokens
            seen_ids.add(page_hit.passage_id)
            deep_fill.append(
                candidate_item(
                    page_hit,
                    score=sel.get("score", 0.0),
                    fts_rank=sel.get("fts_rank"),
                )
            )
    cancellation.checkpoint("context")
    return guaranteed + deep_fill


def assemble_recovery(
    selected: Sequence[Mapping[str, Any]],
    *,
    condition: RecoveryCondition,
    store: RetrievalIndexStore,
    cancellation: QueryCancellationContext,
    candidates: Sequence[Mapping[str, Any]],
    raw_store: RetrievalIndexStore | None = None,
) -> RecoveryAssembly:
    """Assemble one recovery state from recall candidates.

    ``candidates`` is the only input for page statistics and candidate pools.
    The assembler derives those internal projections so every recovery path
    observes the same minimum candidate contract: ``hit`` (with page path,
    score and source kind) and, when ``stats_score`` is ``"item"``, ``score``.
    """

    cancellation.checkpoint("fallback")
    normalized_selected = [dict(item) for item in selected]
    normalized_candidates = [dict(item) for item in candidates]
    page_stats: dict[str, dict[str, Any]] = {}
    page_candidates: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(normalized_candidates):
        cancellation.checkpoint_batch(index, every=16, stage="fallback")
        page_path = item["hit"].page_path
        page_candidates.setdefault(page_path, []).append(item)
        stats = page_stats.setdefault(
            page_path,
            {"max": 0.0, "store": "raw" if page_path.startswith("raw/") else "active"},
        )
        value = item["score"] if condition.stats_score == "item" else item["hit"].score
        stats["max"] = max(float(stats["max"]), float(value))
    context_items = _build_page_ordered_context(
        normalized_selected,
        store,
        raw_store=raw_store,
        page_stats=page_stats,
        page_candidates=page_candidates,
        cancellation=cancellation,
    )
    raw_paths = [
        item["hit"].page_path
        for item in normalized_selected
        if item["hit"].source_kind == "raw"
    ]
    fallback_level = condition.level if condition.level != "none" and raw_paths else "none"
    fallback = fallback_envelope(
            fallback_level,
            tuple(condition.reasons) if fallback_level != "none" else (),
            tuple(raw_paths) if fallback_level == "raw" else (),
        )
    cancellation.checkpoint("fallback")
    return RecoveryAssembly(
        selected=normalized_selected,
        hit_stats=page_stats,
        pool_by_page=page_candidates,
        context_items=context_items,
        fallback=fallback,
    )

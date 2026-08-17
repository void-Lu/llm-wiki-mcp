"""Query V2 召回与回退启发式策略的唯一 owner。

本模块只持有召回/回退策略的常量、正则和决策函数，不持有可变的查询执行
状态、raw store 记忆化或 outcome 冻结逻辑。它复用现有 retrieval index、
snapshot、取消上下文和 recovery primitive；不得依赖 QueryExecutionContext
或 Query V2 pipeline，以保持策略 owner 与执行编排单向分离。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from retrieval.candidate_items import candidate_item, with_fusion
from retrieval.lexical_analyzer import (
    edit_distance,
    has_qualified_identifier,
    identifier_phrases,
    tokens,
)
from retrieval.metadata_filters import page_matches_filters
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_recovery import (
    LadderStep,
    search_ladder,
    select_best_per_page,
    step_bonus,
)
from retrieval.query_shared import QueryFilters, eligible, matches_request
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore


DEFAULT_TOP_K = 10
RANKING_POLICY_VERSION = "query-v2-passage-rrf-10"
RRF_K = 60
PASSAGE_SCAN_LIMIT = 500
PASSAGE_PROBE_LIMIT = 20
RAW_FALLBACK_LIMIT = 20
RAW_FALLBACK_CANDIDATE_LIMIT = 160
IDENTIFIER_PHRASE_BONUS = 20.0
IDENTIFIER_PHRASE_CANDIDATES = 200
ADAPTIVE_EXPAND_MAX = 40
ADAPTIVE_SCORE_RATIO = 0.9
MAX_ADAPTIVE_SCORE_RATIO = 0.7

_STEP_ITEM_RE = re.compile(r"(?:^\s*\d{1,3}\s*[\.\)、]|^\s*第[一二三四五六七八九十百\d]+步)", re.M)
_COMPARE_RE = re.compile(r"(?:比较|区别|差异|对比|compare|versus|vs\.?|difference)", re.I)
_HISTORY_RE = re.compile(r"(?:之前|上次|讨论|会话|当时|历史|previous|last\s+(?:time|session)|history)", re.I)
_EXACT_RE = re.compile(r"(?:原文|逐字|代码|字段|field\s+id|api|record|script|exact|verbatim)", re.I)
_RESEARCH_RE = re.compile(r"(?:研究|深入|调研|research|deep\s+dive)", re.I)
_HOWTO_RE = re.compile(
    r"(?:步骤|怎么|如何|怎样|流程|做法|配置|安装|介绍|指南|guide|how\s+to|steps?|setup|configure|install)",
    re.I,
)

__all__ = [
    "ADAPTIVE_EXPAND_MAX",
    "ADAPTIVE_SCORE_RATIO",
    "DEFAULT_TOP_K",
    "IDENTIFIER_PHRASE_BONUS",
    "IDENTIFIER_PHRASE_CANDIDATES",
    "MAX_ADAPTIVE_SCORE_RATIO",
    "PASSAGE_PROBE_LIMIT",
    "PASSAGE_SCAN_LIMIT",
    "RANKING_POLICY_VERSION",
    "RAW_FALLBACK_CANDIDATE_LIMIT",
    "RAW_FALLBACK_LIMIT",
    "RRF_K",
    "adaptive_expand",
    "classify_intent",
    "merge_coverage_items",
    "query_expansion",
    "raw_recovery_candidates",
    "relaxed_recovery_items",
    "step_counts_for_pages",
    "uncovered_latin_terms",
]


def step_counts_for_pages(
    paths: list[str],
    store: RetrievalIndexStore,
    raw_store: RetrievalIndexStore | None,
) -> dict[str, int]:
    """Count ordered-list items per page as a topic- and language-agnostic
    procedural signal.

    How-to, install and fix pages are almost always written as numbered steps
    (``1.``, ``1)``, ``1、`` or ``第一步``), while reference, FAQ and overview
    pages typically are not.  Counting those list items needs no vocabulary:
    it is a pure document-structure feature that works for any topic in any
    language.
    """

    counts: dict[str, int] = {}
    for sub_paths, sub_store in (
        ([p for p in paths if not p.startswith("raw/")], store),
        ([p for p in paths if p.startswith("raw/")], raw_store),
    ):
        if not sub_paths or sub_store is None:
            continue
        hits = sub_store.passages_for_pages(sub_paths, limit_per_page=PASSAGE_SCAN_LIMIT)
        for hit in hits:
            counts[hit.page_path] = counts.get(hit.page_path, 0) + len(_STEP_ITEM_RE.findall(hit.text))
    return counts


def adaptive_expand(
    ranked: list[dict[str, Any]],
    base_top_k: int,
    *,
    max_top_k: int = ADAPTIVE_EXPAND_MAX,
    score_ratio: float = ADAPTIVE_SCORE_RATIO,
) -> list[dict[str, Any]]:
    """Extend the result list when scores stay close to the requested boundary.

    The requested ``top_k`` is a floor, not a hard cap: pages ranked just
    behind the boundary that still score within ``score_ratio`` of it are
    plausibly part of the same answer set, so they are kept — up to
    ``max_top_k`` (40).  Expansion also has a global score floor relative to
    the highest-ranked page, preventing a low-scoring plateau from expanding
    the response when the boundary score itself is already weak.  The decision
    is purely score-driven, so it applies to any topic and language without
    domain vocabulary.
    """

    selected = ranked[:base_top_k]
    if len(ranked) <= base_top_k or base_top_k <= 0:
        return selected
    boundary = ranked[base_top_k - 1]["score"]
    if boundary <= 0:
        return selected
    top_score = ranked[0]["score"]
    threshold = max(
        boundary * score_ratio,
        top_score * MAX_ADAPTIVE_SCORE_RATIO,
    )
    index = base_top_k
    while index < min(len(ranked), max_top_k):
        if ranked[index]["score"] >= threshold:
            selected.append(ranked[index])
            index += 1
        else:
            break
    return selected


def query_expansion(
    question: str,
    store: RetrievalIndexStore,
    raw_store: RetrievalIndexStore | None,
    agent_terms: dict[str, list[str]] | None = None,
    project: str | None = None,
    *,
    snapshot: QueryCorpusSnapshot | None = None,
    raw_snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Build query-expansion terms from page titles and caller-supplied maps.

    A user's phrasing often differs from document vocabulary by one edit
    (``chatbox`` vs ``ChatBot``), which the corpus-driven title matcher covers
    automatically.  Semantic gaps such as abbreviations (``sl`` vs
    ``Suitelet``) cannot be bridged by character-level matching, so the caller
    (the agent driving this MCP) supplies the mapping via ``agent_terms``
    after consulting its own model.  The third return value lists the terms
    that still lack a variant: they are the fuzzy hints the caller may want to
    resolve before retrying the query.
    """

    title_words: set[str] = set()
    active_pages = snapshot.pages if snapshot is not None else store.page_candidates()
    for index, page in enumerate(active_pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="snapshot")
        raw_frontmatter = page.get("frontmatter")
        frontmatter: Mapping[str, Any] = raw_frontmatter if isinstance(raw_frontmatter, Mapping) else {}
        if not page_matches_filters(frontmatter, "wiki", project=project):
            continue
        title_words.update(tokens(str(page.get("title") or "")))
    if raw_store is not None:
        raw_pages = raw_snapshot.pages if raw_snapshot is not None else raw_store.page_candidates()
        for index, page in enumerate(raw_pages):
            if cancellation is not None:
                cancellation.checkpoint_batch(index, every=16, stage="snapshot")
            raw_frontmatter = page.get("frontmatter")
            frontmatter: Mapping[str, Any] = raw_frontmatter if isinstance(raw_frontmatter, Mapping) else {}
            if not page_matches_filters(frontmatter, "raw", project=project):
                continue
            title_words.update(tokens(str(page.get("title") or "")))
    base_latin = [
        value
        for value in tokens(question)
        if len(value) >= 2 and re.fullmatch(r"[a-z0-9_]+", value)
    ]
    variants: dict[str, list[str]] = {}
    for term in base_latin:
        close: list[str] = []
        if title_words:
            close = [
                word
                for word in title_words
                if word != term and len(word) >= 4 and edit_distance(term, word) <= 1
            ]
        for alias in (agent_terms or {}).get(term, ()):
            if alias != term and alias not in close:
                close.append(alias)
        if close:
            variants[term] = close
    extra_terms = [value for values in variants.values() for value in values]
    suggestions = [term for term in base_latin if term not in title_words and term not in variants]
    return extra_terms, variants, suggestions


def classify_intent(question: str) -> str:
    if _HISTORY_RE.search(question):
        return "history"
    if _EXACT_RE.search(question):
        return "exact_evidence"
    if _COMPARE_RE.search(question):
        return "comparison"
    if _RESEARCH_RE.search(question):
        return "research"
    if _HOWTO_RE.search(question):
        return "concept"
    if re.search(r"\b[A-Za-z][\w.:-]{2,}\b", question):
        return "exact_entity"
    return "concept"


def relaxed_recovery_items(
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    question: str,
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    extra_terms: list[str],
    cancellation: QueryCancellationContext | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    """Read a bounded relaxed candidate set from one existing projection."""

    try:
        hits = store.search_fts(
            question,
            limit=IDENTIFIER_PHRASE_CANDIDATES,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
            mode="relaxed",
            extra_terms=extra_terms,
        )
    except RetrievalIndexError as exc:
        return [], 0, exc.code
    items: list[dict[str, Any]] = []
    for rank, hit in enumerate(hits, 1):
        if cancellation is not None:
            cancellation.checkpoint_batch(rank - 1, every=16, stage="fallback")
        if not eligible(hit, metadata, scope=scope) or not matches_request(
            hit, metadata, project=project, filters=filters
        ):
            continue
        items.append(candidate_item(hit, score=round(hit.score, 12), fts_rank=rank))
    return items, len(hits), ""


def _raw_recovery_bonus(hit: PassageHit, question: str, identifier_terms: list[str]) -> float:
    """Rank raw pages with bounded title, heading, phrase, and anchor signals."""

    query_terms = set(tokens(question))
    title_terms = set(tokens(hit.title))
    heading_text = " ".join(hit.heading_path)
    heading_terms = set(tokens(heading_text))
    title_overlap = len(query_terms & title_terms)
    heading_overlap = len(query_terms & heading_terms)
    title_heading = f"{hit.title} {heading_text}".casefold()
    body = hit.text.casefold()
    phrase_matches = [term.casefold() for term in identifier_terms if term]
    phrase_bonus = 5.0 if any(term in title_heading for term in phrase_matches) else 0.0
    anchor_bonus = 2.0 if any(term in heading_text.casefold() for term in phrase_matches) else 0.0
    exact_phrase_bonus = 1.5 if question.strip().casefold() in body and len(question.strip()) >= 4 else 0.0
    return min(title_overlap, 3) * 4.0 + min(heading_overlap, 3) * 2.0 + phrase_bonus + anchor_bonus + exact_phrase_bonus


def raw_recovery_candidates(
    raw_store: RetrievalIndexStore,
    question: str,
    *,
    project: str | None,
    filters: QueryFilters,
    scope: str,
    extra_terms: list[str],
    term_variants: dict[str, list[str]],
    top_k: int,
    snapshot: QueryCorpusSnapshot,
    raw_availability: Literal["fresh", "stale", "missing"],
    raw_index_warning: str = "",
    cancellation: QueryCancellationContext | None = None,
) -> tuple[RetrievalIndexStore, list[dict[str, Any]], int, str, str]:
    """Run the bounded raw projection recovery for raw evidence paths.

    The helper owns raw-store status checks and the strict/qualified/
    identifier/prefix/relaxed sequence.  Coverage callers normalize its
    page-local ranking against active Wiki candidates; raw BM25 is never a
    cross-corpus score.

    Search-ladder failures are returned as the underlying index code.  The
    execution-context owner is responsible for converting that code into the
    public raw-index warning vocabulary.
    """

    raw_fts_hits = 0
    raw_index_warning = ""
    raw_lexical_mode = "strict"
    candidate_limit = min(max(top_k * 8, RAW_FALLBACK_LIMIT), RAW_FALLBACK_CANDIDATE_LIMIT)
    if raw_availability != "fresh":
        return raw_store, [], raw_fts_hits, raw_index_warning, raw_lexical_mode
    raw_metadata: dict[str, dict[str, Any]] = {}
    pages = snapshot.pages
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="snapshot")
        frontmatter = page.get("frontmatter")
        raw_metadata[str(page["path"])] = dict(frontmatter) if isinstance(frontmatter, Mapping) else {}

    common_kwargs = {
        "project": project,
        "page_type": filters.type,
        "tags": list(filters.tags),
    }
    ladder_steps = [LadderStep("strict", "strict", question, common_kwargs)]
    if has_qualified_identifier(question):
        ladder_steps.append(LadderStep("qualified_code", "qualified_code", question, common_kwargs))
    ladder_steps.extend(
        [
            LadderStep(
                "identifier_phrase",
                "identifier_phrase",
                question,
                {**common_kwargs, "term_variants": term_variants},
                run_if_empty=True,
                ignore_errors=True,
            ),
            LadderStep("raw_prefix", "raw_prefix", question, common_kwargs, run_if_empty=True),
            LadderStep(
                "relaxed",
                "relaxed",
                question,
                {**common_kwargs, "extra_terms": extra_terms},
                run_if_empty=True,
            ),
        ]
    )
    raw_hits, raw_lexical_mode = search_ladder(
        raw_store,
        steps=ladder_steps,
        merge="replace_if_nonempty",
        limit=candidate_limit,
        swallow_index_errors=True,
    )
    if raw_lexical_mode.startswith("error:"):
        return raw_store, [], raw_fts_hits, raw_lexical_mode[6:], "strict"

    raw_fts_hits = len(raw_hits)
    raw_items: list[dict[str, Any]] = []
    query_identifier_phrases = identifier_phrases(question) if raw_lexical_mode == "identifier_phrase" else []
    for rank, hit in enumerate(raw_hits, 1):
        if cancellation is not None:
            cancellation.checkpoint_batch(rank - 1, every=16, stage="fallback")
        if not eligible(hit, raw_metadata, scope=scope):
            continue
        score = hit.score + _raw_recovery_bonus(hit, question, query_identifier_phrases)
        if query_identifier_phrases:
            compact = f"{hit.title}\n{hit.text}".casefold()
            if any(phrase in compact for phrase in query_identifier_phrases):
                score += IDENTIFIER_PHRASE_BONUS
            elif any(variant in compact for variants in term_variants.values() for variant in variants):
                score += IDENTIFIER_PHRASE_BONUS * 0.5
        raw_items.append(candidate_item(hit, score=score, fts_rank=rank))
    raw_candidate_items = select_best_per_page(raw_items)
    if raw_candidate_items and raw_lexical_mode != "qualified_code":
        step_counts = step_counts_for_pages(
            sorted({item["hit"].page_path for item in raw_candidate_items}),
            raw_store,
            raw_store,
        )
        for item in raw_candidate_items:
            count = step_counts.get(item["hit"].page_path, 0)
            item["score"] = round(
                item["score"] + step_bonus(count),
                12,
            )
    raw_candidate_items.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    return raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, raw_lexical_mode


def uncovered_latin_terms(question: str, selected: Sequence[dict[str, Any]]) -> list[str]:
    """Return query Latin tokens absent from selected primary passages."""

    query_terms = [
        term
        for term in tokens(question)
        if len(term) >= 3 and re.fullmatch(r"[a-z0-9_]+", term)
    ]
    if not query_terms:
        return []
    covered: set[str] = set()
    for item in selected:
        hit = item.get("hit")
        if isinstance(hit, PassageHit):
            covered.update(
                term
                for term in tokens(f"{hit.title} {hit.text}")
                if len(term) >= 3 and re.fullmatch(r"[a-z0-9_]+", term)
            )
    return list(dict.fromkeys(term for term in query_terms if term not in covered))


def _coverage_terms(item: dict[str, Any], terms: Sequence[str]) -> set[str]:
    hit = item["hit"]
    text = f"{hit.title} {hit.text}"
    available = set(tokens(text))
    return {term for term in terms if term in available}


def merge_coverage_items(
    active_items: Sequence[dict[str, Any]],
    raw_items: Sequence[dict[str, Any]],
    uncovered_terms: Sequence[str],
    *,
    rrf_k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Fuse active and raw page ranks using normalized, source-local signals."""

    active_pages = select_best_per_page(list(active_items))
    raw_pages = select_best_per_page(list(raw_items))
    active_pages.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    raw_pages.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    ranked: list[dict[str, Any]] = []
    total_terms = max(len(uncovered_terms), 1)
    for local_rank, item in enumerate([*active_pages, *raw_pages], 1):
        is_raw = item["hit"].source_kind == "raw"
        if is_raw:
            source_rank = raw_pages.index(item) + 1
        else:
            source_rank = active_pages.index(item) + 1
        covered = _coverage_terms(item, uncovered_terms)
        if is_raw and not covered:
            continue
        coverage_ratio = len(covered) / total_terms
        source_rrf = (rrf_k + 1) / (rrf_k + source_rank)
        authority = 0.0 if is_raw else min(max(float(item.get("score", 0.0)) / 100.0, 0.0), 0.35)
        fused_score = round(coverage_ratio + source_rrf + authority, 12)
        ranked.append(
            with_fusion(
                {**item, "score": fused_score},
                coverage_terms=sorted(covered),
                coverage_ratio=coverage_ratio,
                source_local_rank=source_rank,
                source_local_rrf=source_rrf,
                fusion_score=fused_score,
                fusion_source="raw" if is_raw else "active",
                fusion_local_position=local_rank,
            )
        )
    ranked.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    return ranked

"""Execution state and bounded recovery helpers for one Query V2 call."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
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
from retrieval.query_shared import (
    QueryFilters,
    eligible,
    matches_request,
)
from retrieval.query_recovery import (
    FallbackPlan,
    FallbackState,
    LadderStep,
    RecoveryAssembly,
    assemble_recovery,
    compose_score,
    plan_fallback,
    resolve_fallback_lexical_mode,
    search_ladder,
    select_best_per_page,
    step_bonus,
)
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from retrieval import discovery as discovery_owner
from retrieval import entity_batch as entity_batch_owner


DEFAULT_TOP_K = 10
RANKING_POLICY_VERSION = "query-v2-passage-rrf-10"
RawAvailability = Literal["fresh", "stale", "missing"]
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
def _step_counts_for_pages(
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


def _query_expansion(
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


_COMPARE_RE = re.compile(r"(?:比较|区别|差异|对比|compare|versus|vs\.?|difference)", re.I)
_HISTORY_RE = re.compile(r"(?:之前|上次|讨论|会话|当时|历史|previous|last\s+(?:time|session)|history)", re.I)
_EXACT_RE = re.compile(r"(?:原文|逐字|代码|字段|field\s+id|api|record|script|exact|verbatim)", re.I)
_RESEARCH_RE = re.compile(r"(?:研究|深入|调研|research|deep\s+dive)", re.I)
_HOWTO_RE = re.compile(r"(?:步骤|怎么|如何|怎样|流程|做法|配置|安装|介绍|指南|guide|how\s+to|steps?|setup|configure|install)", re.I)


def classify_intent(question: str) -> str:
    if _HISTORY_RE.search(question): return "history"
    if _EXACT_RE.search(question): return "exact_evidence"
    if _COMPARE_RE.search(question): return "comparison"
    if _RESEARCH_RE.search(question): return "research"
    if _HOWTO_RE.search(question): return "concept"
    if re.search(r"\b[A-Za-z][\w.:-]{2,}\b", question): return "exact_entity"
    return "concept"


def _relaxed_recovery_items(
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
        if not eligible(hit, metadata, scope=scope) or not matches_request(hit, metadata, project=project, filters=filters):
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


def _raw_index_warning(status: Mapping[str, object]) -> str:
    """Expose raw-store failures without conflating them with the Wiki index."""

    state = str(status.get("state") or "")
    if state == "stale":
        return "raw_index_stale"
    code = str(status.get("code") or "unavailable")
    if code.startswith("index_"):
        code = code.removeprefix("index_")
    return f"raw_index_{code}"


def _raw_recovery_candidates(
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
    raw_availability: RawAvailability,
    raw_index_warning: str = "",
    cancellation: QueryCancellationContext | None = None,
) -> tuple[RetrievalIndexStore, list[dict[str, Any]], int, str, str]:
    """Run the bounded raw projection recovery for raw evidence paths.

    The helper owns raw-store status checks and the strict/qualified/
    identifier/prefix/relaxed sequence.  Coverage callers normalize its
    page-local ranking against active Wiki candidates; raw BM25 is never a
    cross-corpus score.
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
        return raw_store, [], raw_fts_hits, _raw_index_warning({"code": raw_lexical_mode[6:]}), "strict"

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
        step_counts = _step_counts_for_pages(
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


def _uncovered_latin_terms(question: str, selected: Sequence[dict[str, Any]]) -> list[str]:
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


def _merge_coverage_items(
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


def _freeze_value(value: Any) -> Any:
    """Recursively project query state into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_recovery(value: RecoveryAssembly) -> RecoveryAssembly:
    return replace(
        value,
        selected=_freeze_value(value.selected),
        hit_stats=_freeze_value(value.hit_stats),
        pool_by_page=_freeze_value(value.pool_by_page),
        context_items=_freeze_value(value.context_items),
        fallback=_freeze_value(value.fallback),
    )


@dataclass(frozen=True)
class QueryExecutionOutcome:
    """Immutable read model published once a query execution is complete."""

    selected: tuple[Mapping[str, Any], ...]
    context_items: tuple[Mapping[str, Any], ...]
    recovery: RecoveryAssembly
    status: Mapping[str, Any]
    raw_availability: RawAvailability
    raw_fts_hits: int
    relaxed_fts_hits: int
    raw_index_warning: str
    coverage_fallback: bool
    lexical_mode: str
    expansion_suggestions: tuple[str, ...]
    uncovered_latin_terms: tuple[str, ...]
    discovery: Mapping[str, Any]
    discovery_entities: tuple[Mapping[str, Any], ...]
    discovery_source_items: tuple[Mapping[str, Any], ...]
    batch_payload: Mapping[str, Any]
    discovery_requested: bool


@dataclass
class QueryExecutionContext:
    """Mutable state owner for one Query V2 invocation.

    The context owns the physical stores and immutable snapshots used by all
    fallback/discovery stages.  It deliberately keeps the raw store lazy so a
    successful active Wiki query never opens the second corpus.
    """

    root: Path
    store: RetrievalIndexStore
    cancellation: QueryCancellationContext
    status: dict[str, Any]
    raw_store: RetrievalIndexStore | None = field(default=None, init=False)
    raw_snapshot: QueryCorpusSnapshot | None = field(default=None, init=False)
    selected: list[dict[str, Any]] = field(default_factory=list, init=False)
    context_items: list[dict[str, Any]] = field(default_factory=list, init=False)
    recovery: RecoveryAssembly | None = field(default=None, init=False)
    raw_fts_hits: int = field(default=0, init=False)
    relaxed_fts_hits: int = field(default=0, init=False)
    raw_index_warning: str = field(default="", init=False)
    coverage_fallback: bool = field(default=False, init=False)
    lexical_mode: str = field(default="strict", init=False)
    expansion_suggestions: list[str] = field(default_factory=list, init=False)
    uncovered_latin_terms: list[str] = field(default_factory=list, init=False)
    discovery: dict[str, Any] = field(default_factory=dict, init=False)
    discovery_entities: list[dict[str, Any]] = field(default_factory=list, init=False)
    discovery_source_items: list[dict[str, Any]] = field(default_factory=list, init=False)
    batch_payload: dict[str, Any] = field(default_factory=dict, init=False)
    discovery_requested: bool = field(default=False, init=False)
    _raw_status: dict[str, object] | None = field(default=None, init=False, repr=False)
    _raw_availability: RawAvailability = field(default="missing", init=False, repr=False)
    _sealed: bool = field(default=False, init=False, repr=False)
    _outcome: QueryExecutionOutcome | None = field(default=None, init=False, repr=False)

    def __setattr__(self, name: str, value: object) -> None:
        if name not in {"_sealed", "_outcome"} and getattr(self, "_sealed", False):
            raise RuntimeError("query execution context is sealed")
        object.__setattr__(self, name, value)

    def _ensure_open(self) -> None:
        if self._sealed:
            raise RuntimeError("query execution context is sealed")

    def get_raw_store(self) -> RetrievalIndexStore:
        """Return the single lazily-created raw projection for this query."""

        self._ensure_open()
        if self.raw_store is None:
            self.raw_store = RetrievalIndexStore(self.root, scope="raw")
        return self.raw_store

    def raw_availability(self) -> RawAvailability:
        """Normalize raw index status to the query's three-state vocabulary."""

        self._ensure_open()
        status = self.get_raw_store().status()
        self._raw_status = dict(status)
        state = str(status.get("state") or "")
        if bool(status.get("ok")) and state == "fresh":
            self._raw_availability = "fresh"
        elif state == "stale":
            self._raw_availability = "stale"
        else:
            self._raw_availability = "missing"
        return self._raw_availability

    def capture_raw_snapshot(self) -> QueryCorpusSnapshot:
        """Capture the raw metadata view at most once, including an empty view."""

        self._ensure_open()
        if self.raw_snapshot is None:
            availability = self.raw_availability()
            if availability != "fresh":
                self.raw_snapshot = QueryCorpusSnapshot.empty("raw")
            else:
                self.raw_snapshot = QueryCorpusSnapshot.capture(
                    self.get_raw_store(),
                    cancellation=self.cancellation,
                )
        return self.raw_snapshot

    def _raw_warning(self) -> str:
        return _raw_index_warning(self._raw_status or {})

    def run_raw_branch(
        self,
        *,
        branch: Literal["coverage", "all_coverage", "raw_zero"],
        question: str,
        project: str | None,
        filters: QueryFilters,
        top_k: int,
        extra_terms: list[str],
        term_variants: dict[str, list[str]],
        uncovered_latin_terms: Sequence[str],
        effective_rrf_k: int,
        selected: list[dict[str, Any]],
        candidate_pool: Sequence[dict[str, Any]],
        plan: FallbackPlan,
        lexical_mode: str,
        coverage_fallback: bool,
    ) -> None:
        """Execute one raw branch and migrate its state onto this context."""

        self._ensure_open()
        self.cancellation.checkpoint("fallback")
        raw_store = self.get_raw_store()
        availability = self.raw_availability()
        warning = self._raw_warning()
        raw_snapshot = self.capture_raw_snapshot()
        if availability != "fresh":
            self.selected = selected
            self.raw_fts_hits = 0
            self.raw_index_warning = warning
            self.lexical_mode = lexical_mode
            self.coverage_fallback = coverage_fallback
            return

        _, raw_candidate_items, raw_fts_hits, raw_index_warning, raw_lexical_mode = _raw_recovery_candidates(
            raw_store,
            question,
            project=project,
            filters=filters,
            scope="raw",
            extra_terms=extra_terms,
            term_variants=term_variants,
            top_k=top_k,
            snapshot=raw_snapshot,
            raw_availability=availability,
            raw_index_warning=warning,
            cancellation=self.cancellation,
        )
        if branch in {"coverage", "all_coverage"}:
            raw_candidate_items = [
                item for item in raw_candidate_items if _coverage_terms(item, uncovered_latin_terms)
            ]
        if not raw_candidate_items:
            self.selected = selected
            self.raw_fts_hits = raw_fts_hits
            self.raw_index_warning = raw_index_warning
            self.lexical_mode = lexical_mode
            self.coverage_fallback = coverage_fallback
            return

        if branch in {"coverage", "all_coverage"}:
            merged = _merge_coverage_items(
                selected,
                raw_candidate_items,
                uncovered_latin_terms,
                rrf_k=effective_rrf_k,
            )
            selected = adaptive_expand(merged, top_k)[:top_k]
            candidates = [*candidate_pool, *raw_candidate_items]
            coverage_fallback = any(item["hit"].source_kind == "raw" for item in selected)
        else:
            selected = select_best_per_page(raw_candidate_items)
            selected = adaptive_expand(selected, top_k)[:top_k]
            candidates = raw_candidate_items
            lexical_mode = resolve_fallback_lexical_mode(
                plan,
                raw_lexical_mode,
                qualified_identifier=has_qualified_identifier(question),
            ) or lexical_mode

        recovery = assemble_recovery(
            selected,
            condition=plan.condition,
            candidates=candidates,
            store=self.store,
            raw_store=raw_store,
            cancellation=self.cancellation,
        )
        self.selected = selected
        self.context_items = recovery.context_items
        self.recovery = recovery
        self.raw_fts_hits = raw_fts_hits
        self.raw_index_warning = raw_index_warning
        self.lexical_mode = lexical_mode
        self.coverage_fallback = coverage_fallback

    def recovery_or_raise(self) -> RecoveryAssembly:
        if self.recovery is None:
            raise RuntimeError("query execution recovery has not been initialized")
        return self.recovery

    def outcome(self) -> QueryExecutionOutcome:
        """Freeze and publish the complete state for the envelope assembler."""

        if self._outcome is not None:
            return self._outcome
        recovery = self.recovery_or_raise()
        outcome = QueryExecutionOutcome(
            selected=_freeze_value(self.selected),
            context_items=_freeze_value(self.context_items),
            recovery=_freeze_recovery(recovery),
            status=_freeze_value(self.status),
            raw_availability=self._raw_availability,
            raw_fts_hits=self.raw_fts_hits,
            relaxed_fts_hits=self.relaxed_fts_hits,
            raw_index_warning=self.raw_index_warning,
            coverage_fallback=self.coverage_fallback,
            lexical_mode=self.lexical_mode,
            expansion_suggestions=_freeze_value(self.expansion_suggestions),
            uncovered_latin_terms=_freeze_value(self.uncovered_latin_terms),
            discovery=_freeze_value(self.discovery),
            discovery_entities=_freeze_value(self.discovery_entities),
            discovery_source_items=_freeze_value(self.discovery_source_items),
            batch_payload=_freeze_value(self.batch_payload),
            discovery_requested=self.discovery_requested,
        )
        object.__setattr__(self, "_outcome", outcome)
        object.__setattr__(self, "_sealed", True)
        return outcome

    def _run_fallback_recovery(
        self,
        *,
        question: str,
        effective_scope: str,
        project: str | None,
        filters: QueryFilters,
        top_k: int,
        metadata: dict[str, dict[str, Any]],
        snapshot: QueryCorpusSnapshot,
        expansion_terms: dict[str, list[str]] | None,
        intent: str,
        effective_rrf_k: int,
        has_primary_recall: bool,
        selected: list[dict[str, Any]],
        scored: list[dict[str, Any]],
        recovery: RecoveryAssembly,
        stage_lexical_mode: str,
    ) -> None:
        """Run fallback branches and retain their state on this context."""

        self._ensure_open()
        self.selected = selected
        self.context_items = recovery.context_items
        self.recovery = recovery
        self.raw_fts_hits = 0
        self.relaxed_fts_hits = 0
        self.raw_index_warning = ""
        self.coverage_fallback = False
        self.lexical_mode = stage_lexical_mode
        self.expansion_suggestions = []
        self.uncovered_latin_terms = (
            _uncovered_latin_terms(question, selected)
            if has_primary_recall and effective_scope in {"knowledge", "all"}
            else []
        )
        query_extra_terms: list[str] = []
        query_term_variants: dict[str, list[str]] = {}
        wiki_relaxed_answered = False

        self.cancellation.checkpoint("fallback")
        coverage_state = FallbackState(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=tuple(self.uncovered_latin_terms),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        )
        coverage_plan = plan_fallback(coverage_state)
        if coverage_plan is not None and coverage_plan.branch == "coverage":
            self.run_raw_branch(
                branch="coverage",
                question=question,
                project=project,
                filters=filters,
                top_k=top_k,
                extra_terms=[],
                term_variants={},
                uncovered_latin_terms=self.uncovered_latin_terms,
                effective_rrf_k=effective_rrf_k,
                selected=self.selected,
                candidate_pool=scored,
                plan=coverage_plan,
                lexical_mode=self.lexical_mode,
                coverage_fallback=self.coverage_fallback,
            )

        relaxed_state = FallbackState(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=tuple(self.uncovered_latin_terms),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=True,
        )
        relaxed_plan = plan_fallback(relaxed_state)
        if relaxed_plan is not None and relaxed_plan.branch == "wiki_relaxed":
            self.cancellation.checkpoint("fallback")
            query_extra_terms, query_term_variants, self.expansion_suggestions = _query_expansion(
                question,
                self.store,
                None,
                expansion_terms,
                project,
                snapshot=snapshot,
                raw_snapshot=self.raw_snapshot,
                cancellation=self.cancellation,
            )
            self.cancellation.checkpoint("fallback")
            wiki_relaxed_items, self.relaxed_fts_hits, relaxed_warning = _relaxed_recovery_items(
                self.store,
                metadata,
                question,
                scope=effective_scope,
                project=project,
                filters=filters,
                extra_terms=query_extra_terms,
                cancellation=self.cancellation,
            )
            if relaxed_warning:
                self.status = {**self.status, "code": relaxed_warning}
            if wiki_relaxed_items:
                step_counts = _step_counts_for_pages(
                    sorted({item["hit"].page_path for item in wiki_relaxed_items}),
                    self.store,
                    None,
                )
                for item in wiki_relaxed_items:
                    count = step_counts.get(item["hit"].page_path, 0)
                    item["score"] = compose_score(
                        item["hit"],
                        question,
                        intent,
                        effective_scope,
                        metadata,
                        base=item["hit"].score,
                        freshness=True,
                        step_bonus=step_bonus(count),
                    )
                wiki_relaxed_items.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
                self.selected = adaptive_expand(select_best_per_page(wiki_relaxed_items), top_k)
                self.recovery = assemble_recovery(
                    self.selected,
                    condition=relaxed_plan.condition,
                    candidates=wiki_relaxed_items,
                    store=self.store,
                    cancellation=self.cancellation,
                )
                self.context_items = self.recovery.context_items
                self.lexical_mode = relaxed_plan.lexical_mode or self.lexical_mode
                wiki_relaxed_answered = True
                self.uncovered_latin_terms = _uncovered_latin_terms(question, self.selected)
                all_coverage_state = FallbackState(
                    has_primary_recall=has_primary_recall,
                    effective_scope=effective_scope,
                    uncovered_latin_terms=tuple(self.uncovered_latin_terms),
                    wiki_relaxed_answered=True,
                    raw_available="unknown",
                    relaxed_available=True,
                )
                all_coverage_plan = plan_fallback(all_coverage_state)
                if all_coverage_plan is not None and all_coverage_plan.branch == "all_coverage":
                    self.run_raw_branch(
                        branch="all_coverage",
                        question=question,
                        project=project,
                        filters=filters,
                        top_k=top_k,
                        extra_terms=query_extra_terms,
                        term_variants=query_term_variants,
                        uncovered_latin_terms=self.uncovered_latin_terms,
                        effective_rrf_k=effective_rrf_k,
                        selected=self.selected,
                        candidate_pool=wiki_relaxed_items,
                        plan=all_coverage_plan,
                        lexical_mode=self.lexical_mode,
                        coverage_fallback=self.coverage_fallback,
                    )

        raw_zero_state = FallbackState(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=tuple(self.uncovered_latin_terms),
            wiki_relaxed_answered=wiki_relaxed_answered,
            raw_available="unknown",
            relaxed_available=wiki_relaxed_answered,
        )
        raw_zero_plan = plan_fallback(raw_zero_state)
        if raw_zero_plan is not None and raw_zero_plan.branch == "raw_zero":
            self.run_raw_branch(
                branch="raw_zero",
                question=question,
                project=project,
                filters=filters,
                top_k=top_k,
                extra_terms=query_extra_terms,
                term_variants=query_term_variants,
                uncovered_latin_terms=self.uncovered_latin_terms,
                effective_rrf_k=effective_rrf_k,
                selected=self.selected,
                candidate_pool=(),
                plan=raw_zero_plan,
                lexical_mode=self.lexical_mode,
                coverage_fallback=self.coverage_fallback,
            )

    def _run_discovery_and_batch(
        self,
        *,
        question: str,
        metadata: dict[str, dict[str, Any]],
        effective_scope: str,
        project: str | None,
        filters: QueryFilters,
        snapshot: QueryCorpusSnapshot,
        retrieval_mode: Literal["lexical", "vector", "hybrid"],
        hard_budget_tokens: int,
        confirmation_token: str | None,
    ) -> None:
        """Invoke the discovery and entity-batch owners at their seam."""

        self._ensure_open()

        def raw_discovery_source() -> tuple[RetrievalIndexStore, QueryCorpusSnapshot] | None:
            if self.raw_availability() != "fresh":
                return None
            return self.get_raw_store(), self.capture_raw_snapshot()

        discovery_result = discovery_owner.discover_catalog(
            store=self.store,
            snapshot=snapshot,
            question=question,
            selected=self.selected,
            context_items=self.context_items,
            effective_scope=effective_scope,
            project=project,
            filters=filters,
            cancellation=self.cancellation,
            raw_provider=raw_discovery_source,
        )
        discovery_entities = [dict(entity) for entity in discovery_result.entities]
        self.discovery = dict(discovery_result.discovery)
        self.discovery_entities = discovery_entities
        self.discovery_source_items = [dict(item) for item in discovery_result.source_items]
        self.discovery_requested = discovery_result.requested

        self.batch_payload = {
            "status": "not_triggered",
            "reason": "structured_enumeration_evidence_insufficient",
            "entities": [],
        }
        if discovery_entities:
            self.cancellation.checkpoint("fallback")
            batch_raw_snapshot: QueryCorpusSnapshot | None = self.raw_snapshot
            batch_raw_availability: RawAvailability = "missing"
            if effective_scope in {"knowledge", "all"}:
                batch_raw_availability = self.raw_availability()
                batch_raw_snapshot = self.capture_raw_snapshot()
            batch_result = entity_batch_owner.run_entity_batch(
                discovery_entities,
                question,
                primary_store=self.store,
                effective_scope=effective_scope,
                project=project,
                filters=filters,
                retrieval_mode=retrieval_mode,
                hard_budget_tokens=hard_budget_tokens,
                confirmation_token=confirmation_token,
                snapshot=snapshot,
                raw_snapshot=batch_raw_snapshot,
                raw_store=self.raw_store,
                raw_availability=batch_raw_availability,
                cancellation=self.cancellation,
            )
            self.batch_payload = dict(batch_result.payload)

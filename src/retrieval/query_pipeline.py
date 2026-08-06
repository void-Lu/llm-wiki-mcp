"""Query V2: typed, passage-first retrieval behind the compact MCP response."""

from __future__ import annotations

import datetime
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from retrieval.context_packer import ContextPassage, estimate_tokens, pack_context
from codegraph.codegraph_policy import is_codegraph_raw_path, is_project_code_page
from retrieval.lexical_analyzer import (
    edit_distance,
    identifier_phrases,
    module_qualified,
    tokens,
)
from retrieval.query_telemetry import QueryTelemetry
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from runtime.runtime_config import EmbeddingSettings, TelemetrySettings
from retrieval.vector_index import VectorIndexError, VectorIndexStore, vector_settings_from_embedding
from retrieval.vector_provider import LocalBgeM3Provider, VectorProviderError
from wiki.wiki_query import QueryCandidate, _apply_graph_expansion, _build_graph


RANKING_POLICY_VERSION = "query-v2-passage-rrf-10"
RRF_K = 60
RAW_FALLBACK_LIMIT = 20
RAW_FALLBACK_CANDIDATE_LIMIT = 160
IDENTIFIER_PHRASE_BONUS = 20.0
IDENTIFIER_PHRASE_CANDIDATES = 200
PAGE_FILL_LIMIT = 500
PAGE_TOKEN_BUDGET = 2_400
PAGE_FULL_FILL_MIN_RATIO = 0.6
PAGE_WEAK_HIT_LIMIT = 3
FRESHNESS_BONUS_MAX = 12.0
FRESHNESS_DECAY_DAYS = 90
STEP_BONUS_MAX = 4.0
STEP_COUNT_FULL = 5
ADAPTIVE_EXPAND_MAX = 40
ADAPTIVE_SCORE_RATIO = 0.9
_STEP_ITEM_RE = re.compile(r"(?:^\s*\d{1,3}\s*[\.\)、]|^\s*第[一二三四五六七八九十百\d]+步)", re.M)
def _freshness_bonus(hit: PassageHit, metadata: dict[str, dict[str, Any]]) -> float:
    """Reward recently updated wiki pages so a newer fix patch outranks an
    older note that happens to share the same vocabulary.

    The signal decays linearly over ``FRESHNESS_DECAY_DAYS``.  Raw source pages
    are absent from the active metadata map, so they never receive this bonus:
    official documentation stays stable while project troubleshooting notes
    compete on how current their fix is.
    """

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
        hits = sub_store.passages_for_pages(sub_paths, limit_per_page=PAGE_FILL_LIMIT)
        for hit in hits:
            counts[hit.page_path] = counts.get(hit.page_path, 0) + len(_STEP_ITEM_RE.findall(hit.text))
    return counts


def _adaptive_expand(
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
    ``max_top_k`` (40).  The decision is purely score-driven, so it applies to
    any topic and language without domain vocabulary.
    """

    selected = ranked[:base_top_k]
    if len(ranked) <= base_top_k or base_top_k <= 0:
        return selected
    boundary = ranked[base_top_k - 1]["score"]
    if boundary <= 0:
        return selected
    threshold = boundary * score_ratio
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
    for page in store.page_candidates():
        frontmatter = page.get("frontmatter") if isinstance(page.get("frontmatter"), dict) else {}
        if not _project_page_allowed(frontmatter, project):
            continue
        title_words.update(tokens(str(page.get("title") or "")))
    if raw_store is not None:
        for page in raw_store.page_candidates():
            frontmatter = page.get("frontmatter") if isinstance(page.get("frontmatter"), dict) else {}
            if not _project_page_allowed(frontmatter, project):
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


def _build_page_ordered_context(
    selected: list[dict[str, Any]],
    store: RetrievalIndexStore,
    *,
    raw_store: RetrievalIndexStore | None = None,
    hit_stats: dict[str, dict[str, Any]] | None = None,
    pool_by_page: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Group context candidates by page, then order them by reading order.

    Paragraph-level BM25 decides which pages are relevant, but its per-segment
    scores are not a document map: in a how-to or troubleshooting note the fix
    or install steps usually live in later sections and contain code blocks or
    tables that dilute BM25, so keeping only the top-scored passages per page
    drops exactly those steps.  Once a page is selected, every passage of that
    page is carried in ordinal (reading) order and the global pack budget
    decides how much fits.  The approach is language- and topic-agnostic: no
    step/guide keyword lists are involved, so it works for any answer that
    spans several sections of one document.
    """

    hit_stats = hit_stats or {}
    pool_by_page = pool_by_page or {}
    top_score = max((stats.get("max", 0.0) for stats in hit_stats.values()), default=0.0)
    # Two-phase assembly.  Phase one guarantees every selected page contributes
    # its single best-scored passage, so a long FAQ page cannot starve later
    # relevant pages entirely out of the pack.  Phase two deep-fills the
    # remaining budget page by page in reading order, so fix steps that live in
    # later sections of a document are still carried.  The packer consumes
    # items in this exact order, making the guarantee effective.
    guaranteed: list[dict[str, Any]] = []
    deep_fill: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for sel in selected:
        page_path = sel["hit"].page_path
        pool = pool_by_page.get(page_path, [])
        if not pool:
            continue
        best = max(pool, key=lambda item: item["score"])
        if best["hit"].passage_id not in seen_ids:
            seen_ids.add(best["hit"].passage_id)
            guaranteed.append(best)
    for sel in selected:
        page_path = sel["hit"].page_path
        stats = hit_stats.get(page_path)
        # A page whose best hit is far below the corpus-best score is usually a
        # broad OR-match (an unrelated clipping or quiz note).  Only its top
        # scoring hit passages join the pack instead of the whole page.
        strong_page = stats is None or top_score <= 0 or stats.get("max", 0.0) >= top_score * PAGE_FULL_FILL_MIN_RATIO
        if not strong_page:
            for item in pool_by_page.get(page_path, [])[:PAGE_WEAK_HIT_LIMIT]:
                if item["hit"].passage_id not in seen_ids:
                    seen_ids.add(item["hit"].passage_id)
                    deep_fill.append(item)
            continue
        page_store = raw_store if raw_store is not None and page_path.startswith("raw/") else store
        page_hits = page_store.passages_for_pages([page_path], limit_per_page=PAGE_FILL_LIMIT)
        page_tokens = 0
        for hit in page_hits:
            if hit.passage_id in seen_ids:
                continue
            tokens = estimate_tokens(hit.text)
            # A per-page token budget keeps one long reference page (a FAQ or
            # an overview with many sections) from consuming the whole pack
            # before later selected pages contribute their fix or setup steps.
            if page_tokens + tokens > PAGE_TOKEN_BUDGET:
                break
            page_tokens += tokens
            seen_ids.add(hit.passage_id)
            deep_fill.append(
                {
                    "hit": hit,
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
    return guaranteed + deep_fill
_HISTORY_RE = re.compile(r"(?:之前|上次|讨论|会话|当时|历史|previous|last\s+(?:time|session)|history)", re.I)
_EXACT_RE = re.compile(r"(?:原文|逐字|代码|字段|field\s+id|api|record|script|exact|verbatim)", re.I)
_COMPARE_RE = re.compile(r"(?:比较|区别|差异|对比|compare|versus|vs\.?|difference)", re.I)
_RESEARCH_RE = re.compile(r"(?:研究|深入|调研|research|deep\s+dive)", re.I)
_HOWTO_RE = re.compile(r"(?:步骤|怎么|如何|怎样|流程|做法|配置|安装|介绍|指南|guide|how\s+to|steps?|setup|configure|install)", re.I)


@dataclass(frozen=True)
class QueryFilters:
    type: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "QueryFilters":
        value = value or {}
        tags = value.get("tags", ())
        if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence) or not all(isinstance(item, str) for item in tags):
            raise ValueError("filters.tags must be a sequence of strings")
        page_type = value.get("type")
        if page_type is not None and not isinstance(page_type, str):
            raise ValueError("filters.type must be a string")
        return cls(page_type, tuple(tags))


def classify_intent(question: str) -> str:
    if _HISTORY_RE.search(question): return "history"
    if _EXACT_RE.search(question): return "exact_evidence"
    if _COMPARE_RE.search(question): return "comparison"
    if _RESEARCH_RE.search(question): return "research"
    if _HOWTO_RE.search(question): return "concept"
    if re.search(r"\b[A-Za-z][\w.:-]{2,}\b", question): return "exact_entity"
    return "concept"


def _effective_scope(scope: str, intent: str) -> tuple[str, tuple[str, ...]]:
    if scope not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        raise ValueError("scope must be auto, knowledge, history, all, archive, or raw")
    if scope == "auto":
        return ("history" if intent == "history" else "knowledge"), (("history_intent",) if intent == "history" else ())
    return scope, ()


def _is_source_index(path: str, frontmatter: dict[str, Any]) -> bool:
    page_type = str(frontmatter.get("type") or "").casefold()
    return (
        page_type in {"source_index", "source-index", "index", "source_summary"}
        or path.endswith("/index.md") and "/sources/" in path and "/capsules/" not in path
        or re.search(r"/(?:manifest|_toc_manifest|_path_aliases)\.json$", path.casefold()) is not None
    )


def _is_retired_source_namespace(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized == "wiki/sources" or normalized.startswith("wiki/sources/")


def _eligible(hit: PassageHit, metadata: dict[str, dict[str, Any]], *, scope: str) -> bool:
    fm = metadata.get(hit.page_path, {})
    if scope == "raw":
        normalized_path = hit.page_path.replace("\\", "/")
        if (
            not normalized_path.startswith("raw/sources/")
            or normalized_path.startswith("raw/sources/chat/")
            or is_codegraph_raw_path(normalized_path)
        ):
            return False
    if scope != "archive" and _is_retired_source_namespace(hit.page_path):
        return False
    lifecycle = str(fm.get("lifecycle") or fm.get("lifecycle_status") or "active")
    # An archive store contains only archived material.  Its rows must not be
    # rejected merely because their lifecycle is correctly marked archived.
    if scope != "archive" and lifecycle in {"superseded", "deprecated", "archived"}:
        return False
    if _is_source_index(hit.page_path, fm):
        return False
    history = hit.corpus == "history" or hit.source_kind in {"raw_chat", "legacy_chatlog"}
    return scope == "raw" or scope == "all" or (scope == "history" and history) or (scope == "knowledge" and not history) or scope == "archive"


def _matches_request(hit: PassageHit, metadata: dict[str, dict[str, Any]], *, project: str | None, filters: QueryFilters) -> bool:
    """Apply the same boundary filters to FTS and vector-only candidates."""
    frontmatter = metadata.get(hit.page_path, {})
    if not _project_page_allowed(frontmatter, project):
        return False
    return _filters_allow_page(frontmatter, hit.source_kind, filters)


def _project_page_allowed(frontmatter: Mapping[str, Any], project: str | None) -> bool:
    """Apply the logical project corpus boundary consistently after recall."""

    page_project = str(frontmatter.get("project") or "").casefold()
    if is_project_code_page(frontmatter):
        return project is not None and page_project == project.casefold()
    if project and page_project and page_project != project.casefold():
        return False
    return True


def _filters_allow_page(frontmatter: Mapping[str, Any], source_kind: str, filters: QueryFilters) -> bool:
    if filters.type and str(frontmatter.get("type") or source_kind) != filters.type:
        return False
    if filters.tags:
        tags = frontmatter.get("tags") or ()
        if not isinstance(tags, (list, tuple)) or not set(filters.tags).issubset({str(tag) for tag in tags}):
            return False
    return True


def _authority_bonus(hit: PassageHit, intent: str, scope: str, metadata: dict[str, dict[str, Any]]) -> float:
    values = {"formal_knowledge": 0.35, "concept": 0.35, "entity": 0.35, "project": 0.23, "raw": 0.0, "raw_chat": -0.15}
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
    if intent == "history" and hit.corpus == "history": bonus += 0.25
    if scope == "history" and hit.corpus == "history": bonus += 0.15
    if str(metadata.get(hit.page_path, {}).get("freshness") or "fresh") in {"stale", "review_required"}: bonus -= 0.20
    return bonus


def _heading(hit: PassageHit) -> str:
    return " / ".join(hit.heading_path) if hit.heading_path else hit.title


def _title_overlap_bonus(hit: PassageHit, question: str) -> float:
    """Reward a specific title match over a broad semantic project match."""
    question_terms = {term.casefold() for term in re.findall(r"[\w一-鿿]+", question) if len(term) > 1}
    title_terms = {term.casefold() for term in re.findall(r"[\w一-鿿]+", hit.title) if len(term) > 1}
    if not question_terms or not title_terms:
        overlap_bonus = 0.0
    else:
        overlap_bonus = 0.15 * len(question_terms & title_terms) / len(question_terms)
    latin_terms = re.findall(r"[a-z0-9_]+", question.casefold())
    compact_title = re.sub(r"[^a-z0-9_]+", "", hit.title.casefold())
    if any(len(left + right) >= 5 and left + right in compact_title for left, right in zip(latin_terms, latin_terms[1:])):
        return overlap_bonus + 2.0
    return overlap_bonus


def _citation_metadata(hit: PassageHit, provenance: dict[str, dict[str, str]]) -> dict[str, str]:
    """Expose the minimum traceability fields for low-authority chat evidence."""
    if hit.corpus != "history" and hit.source_kind not in {"raw_chat", "legacy_chatlog"}:
        return {}
    return {
        key: value
        for key, value in provenance.get(hit.page_path, {}).items()
        if key in {"session_id", "occurred_at", "project", "content_hash"} and value
    }


def _title_candidates(
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    question: str,
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    limit: int = 10,
) -> list[PassageHit]:
    """Add bounded title/provenance signals from existing DB projections."""
    terms = {term.casefold() for term in re.findall(r"[\w一-鿿]+", question) if len(term) > 1}
    if not terms:
        return []
    candidates: list[tuple[int, str]] = []
    for page in store.page_candidates():
        path = str(page["path"])
        frontmatter = metadata.get(path, {})
        title_terms = {term.casefold() for term in re.findall(r"[\w一-鿿]+", str(page["title"])) if len(term) > 1}
        sources = frontmatter.get("sources") or ()
        source_values = sources if isinstance(sources, (list, tuple)) else [sources]
        provenance_terms = {
            term.casefold()
            for value in source_values
            if isinstance(value, str)
            for term in re.findall(r"[\w一-鿿]+", value)
            if len(term) > 1
        }
        overlap = len(terms & (title_terms | provenance_terms))
        if not overlap:
            continue
        probe = PassageHit("", path, str(page["title"]), (), "", 0.0, str(page.get("corpus") or "active"), str(page.get("authority") or ""), str(page.get("source_kind") or ""))
        if _eligible(probe, metadata, scope=scope) and _matches_request(probe, metadata, project=project, filters=filters):
            candidates.append((overlap, path))
    paths = [path for _overlap, path in sorted(candidates, key=lambda item: (-item[0], item[1]))[:limit]]
    return store.passages_for_pages(paths, limit_per_page=1)


def _relaxed_recovery_items(
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    question: str,
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    extra_terms: list[str],
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
        if not _eligible(hit, metadata, scope=scope) or not _matches_request(hit, metadata, project=project, filters=filters):
            continue
        items.append(
            {
                "hit": hit,
                "fts_rank": rank,
                "title_rank": None,
                "vector_rank": None,
                "vector_score": 0.0,
                "rrf": 0.0,
                "exact": False,
                "graph_score": 0.0,
                "graph_reasons": [],
                "score": round(hit.score, 12),
            }
        )
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


def _best_passage_per_page(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound raw ranking to one deterministic representative per page."""

    best: dict[str, dict[str, Any]] = {}
    for item in items:
        path = item["hit"].page_path
        current = best.get(path)
        if current is None or item["score"] > current["score"] or (
            item["score"] == current["score"] and item["hit"].passage_id < current["hit"].passage_id
        ):
            best[path] = item
    return list(best.values())


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
    root: Path,
    question: str,
    *,
    project: str | None,
    filters: QueryFilters,
    scope: str,
    extra_terms: list[str],
    term_variants: dict[str, list[str]],
    top_k: int,
) -> tuple[RetrievalIndexStore, list[dict[str, Any]], int, str, str]:
    """Run the bounded raw projection recovery for raw evidence paths.

    The helper owns raw-store status checks and the strict/qualified/
    identifier/prefix/relaxed sequence.  Coverage callers normalize its
    page-local ranking against active Wiki candidates; raw BM25 is never a
    cross-corpus score.
    """

    raw_store = RetrievalIndexStore(root, scope="raw")
    raw_fts_hits = 0
    raw_index_warning = ""
    raw_lexical_mode = "strict"
    raw_status = raw_store.status()
    candidate_limit = min(max(top_k * 8, RAW_FALLBACK_LIMIT), RAW_FALLBACK_CANDIDATE_LIMIT)
    if raw_status.get("ok") and raw_status.get("state") != "fresh":
        raw_index_warning = _raw_index_warning(raw_status)
        return raw_store, [], raw_fts_hits, raw_index_warning, raw_lexical_mode
    if not raw_status.get("ok"):
        return raw_store, [], raw_fts_hits, _raw_index_warning(raw_status), raw_lexical_mode
    raw_metadata: dict[str, dict[str, Any]] = {}
    for page in raw_store.page_candidates():
        frontmatter = page.get("frontmatter")
        raw_metadata[str(page["path"])] = dict(frontmatter) if isinstance(frontmatter, dict) else {}

    raw_items: list[dict[str, Any]] = []
    try:
        raw_hits = raw_store.search_fts(
            question,
            limit=candidate_limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
        )
        qualified_code_hits = raw_store.search_fts(
            question,
            limit=candidate_limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
            mode="qualified_code",
        )
        identifier_phrase_hits: list[PassageHit] = []
        if not raw_hits and not (qualified_code_hits and module_qualified(question)):
            try:
                identifier_phrase_hits = raw_store.search_fts(
                    question,
                    limit=candidate_limit,
                    project=project,
                    page_type=filters.type,
                    tags=list(filters.tags),
                    mode="identifier_phrase",
                    term_variants=term_variants,
                )
            except RetrievalIndexError:
                identifier_phrase_hits = []
        if qualified_code_hits and module_qualified(question):
            raw_hits = qualified_code_hits
            raw_lexical_mode = "qualified_code"
        elif identifier_phrase_hits:
            raw_hits = identifier_phrase_hits
            raw_lexical_mode = "identifier_phrase"
        elif not raw_hits:
            prefix_hits = raw_store.search_fts(
                question,
                limit=candidate_limit,
                project=project,
                page_type=filters.type,
                tags=list(filters.tags),
                mode="raw_prefix",
            )
            if prefix_hits:
                raw_hits = prefix_hits
                raw_lexical_mode = "raw_prefix"
            else:
                raw_hits = raw_store.search_fts(
                    question,
                    limit=candidate_limit,
                    project=project,
                    page_type=filters.type,
                    tags=list(filters.tags),
                    mode="relaxed",
                    extra_terms=extra_terms,
                )
                if raw_hits:
                    raw_lexical_mode = "relaxed"
    except RetrievalIndexError as exc:
        return raw_store, [], raw_fts_hits, _raw_index_warning({"code": exc.code}), raw_lexical_mode

    raw_fts_hits = len(raw_hits)
    query_identifier_phrases = identifier_phrases(question) if raw_lexical_mode == "identifier_phrase" else []
    for rank, hit in enumerate(raw_hits, 1):
        if not _eligible(hit, raw_metadata, scope=scope):
            continue
        score = hit.score + _raw_recovery_bonus(hit, question, query_identifier_phrases)
        if query_identifier_phrases:
            compact = f"{hit.title}\n{hit.text}".casefold()
            if any(phrase in compact for phrase in query_identifier_phrases):
                score += IDENTIFIER_PHRASE_BONUS
            elif any(variant in compact for variants in term_variants.values() for variant in variants):
                score += IDENTIFIER_PHRASE_BONUS * 0.5
        raw_items.append(
            {
                "hit": hit,
                "fts_rank": rank,
                "title_rank": None,
                "vector_rank": None,
                "vector_score": 0.0,
                "rrf": 0.0,
                "exact": False,
                "graph_score": 0.0,
                "graph_reasons": [],
                "score": score,
            }
        )
    raw_candidate_items = _best_passage_per_page(raw_items)
    if raw_candidate_items and raw_lexical_mode != "qualified_code":
        step_counts = _step_counts_for_pages(
            sorted({item["hit"].page_path for item in raw_candidate_items}),
            raw_store,
            raw_store,
        )
        for item in raw_candidate_items:
            count = step_counts.get(item["hit"].page_path, 0)
            item["score"] = round(
                item["score"] + STEP_BONUS_MAX * min(count, STEP_COUNT_FULL) / STEP_COUNT_FULL,
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
) -> list[dict[str, Any]]:
    """Fuse active and raw page ranks using normalized, source-local signals."""

    active_pages = _best_passage_per_page(list(active_items))
    raw_pages = _best_passage_per_page(list(raw_items))
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
        source_rrf = (RRF_K + 1) / (RRF_K + source_rank)
        authority = 0.0 if is_raw else min(max(float(item.get("score", 0.0)) / 100.0, 0.0), 0.35)
        fused_score = round(coverage_ratio + source_rrf + authority, 12)
        ranked.append(
            {
                **item,
                "score": fused_score,
                "coverage_terms": sorted(covered),
                "coverage_ratio": coverage_ratio,
                "source_local_rank": source_rank,
                "source_local_rrf": source_rrf,
                "fusion_score": fused_score,
                "fusion_source": "raw" if is_raw else "active",
                "fusion_local_position": local_rank,
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    return ranked


def _vector_hits(
    root: Path,
    question: str,
    embedding: EmbeddingSettings | None,
    *,
    scope: str,
    allowed_paths: set[str] | None = None,
) -> tuple[dict[str, tuple[int, float]], list[str]]:
    if embedding is None or not embedding.enabled or scope in {"archive", "raw"}:
        return {}, []
    try:
        settings = vector_settings_from_embedding(root, embedding)
        store = VectorIndexStore(root, settings.index_path)
        status = store.status()
        if not status.get("ok") or status.get("state") != "fresh":
            return {}, [str(status.get("code") or "index_stale")]
        if settings.model_path is None:
            return {}, ["model_missing"]
        provider = LocalBgeM3Provider(settings.model_path, device=settings.device, batch_size=settings.batch_size, max_sequence_length=settings.max_sequence_length)
        store.validate_provider(provider.identity(), include_raw_sources=False)
        results = store.search(provider.embed_query(question), allowed_paths=allowed_paths, limit=settings.candidate_limit)
        return {
            result.passage_id: (result.rank, result.score)
            for result in results
            if result.score >= settings.min_vector_score and result.passage_id
        }, []
    except (VectorIndexError, VectorProviderError) as exc:
        return {}, [exc.code]


def _graph_expand(
    root: Path,
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    seed_scores: dict[str, float],
    debug: bool,
) -> tuple[dict[str, QueryCandidate], list[PassageHit]]:
    """Reuse the legacy bounded expander over DB projections, never files.

    The expander owns the existing two-hop, fan-out and score-cap behaviour;
    this adapter only supplies its already-filtered candidate boundary.
    """
    if scope in {"archive", "raw"} or not seed_scores:
        return {}, []
    candidates: list[QueryCandidate] = []
    for page in store.page_candidates():
        path = str(page["path"])
        frontmatter = metadata.get(path, {})
        probe = PassageHit(
            "",
            path,
            str(page["title"]),
            (),
            "",
            0.0,
            str(page.get("corpus") or "active"),
            str(page.get("authority") or ""),
            str(page.get("source_kind") or ""),
        )
        lifecycle = str(frontmatter.get("lifecycle") or frontmatter.get("lifecycle_status") or "active")
        if (
            not path.startswith("wiki/")
            or _is_retired_source_namespace(path)
            or lifecycle in {"superseded", "deprecated", "archived"}
            or _is_source_index(path, frontmatter)
            or not _eligible(probe, metadata, scope=scope)
            or not _matches_request(probe, metadata, project=project, filters=filters)
        ):
            continue
        candidates.append(
            QueryCandidate(
                path=root / path,
                rel=path,
                title=str(page["title"]),
                body=str(page["body"]),
                frontmatter=dict(frontmatter),
            )
        )
    by_path = {candidate.rel: candidate for candidate in candidates}
    scored = {path: by_path[path] for path in seed_scores if path in by_path}
    for path, candidate in scored.items():
        candidate.keyword_score = seed_scores[path]
        candidate.fusion_score = seed_scores[path]
    if not scored:
        return {}, []
    _apply_graph_expansion(scored, candidates, _build_graph(root, candidates), max_graph_hops=2, collect_reasons=debug)
    added = [path for path in scored if path not in seed_scores]
    return scored, store.passages_for_pages(added, limit_per_page=1)


def run_query_v2(
    vault_root: str | Path,
    question: str,
    *,
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"] = "auto",
    project: str | None = None,
    filters: QueryFilters | None = None,
    top_k: int = 10,
    hard_budget_tokens: int = 16_000,
    embedding: EmbeddingSettings | None = None,
    telemetry: TelemetrySettings | None = None,
    debug: bool = False,
    include_context_pack: bool = True,
    lexical_enabled: bool = True,
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    expansion_terms: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Read only existing passage/vector projections and return compact context."""
    started = time.perf_counter()
    if not question.strip():
        return {"ok": False, "code": "missing_question", "error": "question is required"}
    if not 1 <= top_k <= 40:
        return {"ok": False, "code": "invalid_top_k", "error": "top_k must be between 1 and 40"}
    if not lexical_enabled:
        retrieval_mode = "vector"
    if retrieval_mode not in {"lexical", "vector", "hybrid"}:
        return {
            "ok": False,
            "code": "invalid_retrieval_mode",
            "error": "retrieval_mode must be lexical, vector, or hybrid",
        }
    filters = filters or QueryFilters()
    if filters.type and filters.type.casefold() == "code_fact" and not project:
        return {"ok": False, "code": "project_required_for_codegraph", "error": "project is required to query CodeGraph pages"}
    if project:
        project = project.casefold()
    intent = classify_intent(question)
    effective_scope, scope_rules = _effective_scope(scope, intent)
    root = Path(vault_root).expanduser().resolve()
    store_scope = "raw" if effective_scope == "raw" else "archive" if effective_scope == "archive" else "active"
    store = RetrievalIndexStore(root, scope=store_scope)
    status = store.status()
    index_warnings = ["index_stale"] if status.get("ok") and status.get("state") == "stale" else []
    if not status.get("ok"):
        k_budget = min(hard_budget_tokens, 400 * top_k)
        return {
            "ok": True,
            "code": str(status.get("code") or "index_unavailable"),
            "message": "The retrieval index is unavailable; the query was not executed.",
            "question": question,
            "scope": scope,
            "results": [],
            "context_pack": {"passages": [], "citations": [], "budget": {"total": k_budget, "used": 0}},
            "pipeline": {"ranking_version": RANKING_POLICY_VERSION, "warnings": [*index_warnings, str(status.get("code"))], "fallback": {"level": "none", "reasons": ["index_unavailable"], "allowed_source_paths": []}},
        }

    metadata: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, str]] = {}
    for item in store.page_candidates():
        frontmatter = item.get("frontmatter")
        path = str(item["path"])
        metadata[path] = dict(frontmatter) if isinstance(frontmatter, dict) else dict()
        provenance[path] = {
            key: str(item.get(key) or "")
            for key in ("session_id", "occurred_at", "project", "content_hash")
        }
    try:
        fts = (
            store.search_fts(question, limit=50, project=project, page_type=filters.type, tags=list(filters.tags))
            if retrieval_mode != "vector"
            else []
        )
    except RetrievalIndexError as exc:
        fts = []
        status = {**status, "code": exc.code}
    allowed_vector_paths = {
        str(item["path"])
        for item in store.page_candidates()
        if _project_page_allowed(item.get("frontmatter") if isinstance(item.get("frontmatter"), dict) else {}, project)
        and _filters_allow_page(
            item.get("frontmatter") if isinstance(item.get("frontmatter"), dict) else {},
            str(item.get("source_kind") or ""),
            filters,
        )
        and _eligible(
            PassageHit("", str(item["path"]), str(item["title"]), (), "", 0.0, str(item.get("corpus") or "active"), str(item.get("authority") or ""), str(item.get("source_kind") or "")),
            metadata,
            scope=effective_scope,
        )
    }
    vector, vector_warnings = (
        _vector_hits(root, question, embedding, scope=effective_scope, allowed_paths=allowed_vector_paths)
        if retrieval_mode != "lexical" and effective_scope != "raw"
        else ({}, [])
    )
    ranked: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(fts, 1):
        if not _eligible(hit, metadata, scope=effective_scope) or not _matches_request(hit, metadata, project=project, filters=filters):
            continue
        item = ranked.setdefault(hit.passage_id, {"hit": hit, "fts_rank": rank, "title_rank": None, "vector_rank": None, "vector_score": 0.0})
        item["fts_rank"] = rank
    # A vector hit is identified by passage ID.  Load its existing retrieval
    # projection rather than scanning Markdown, so vector-only recall remains
    # available without adding query-time corpus reads.
    for hit in store.load_passages(vector):
        if not _eligible(hit, metadata, scope=effective_scope) or not _matches_request(hit, metadata, project=project, filters=filters):
            continue
        ranked.setdefault(hit.passage_id, {"hit": hit, "fts_rank": None, "title_rank": None, "vector_rank": None, "vector_score": 0.0})
    # A title-only match is deliberately not primary retrieval.  A generic
    # title overlap (such as "script") must not prevent a natural-language
    # question, in any language, from using relaxed lexical recovery.
    has_primary_recall = bool(ranked)
    expansion_suggestions: list[str] = []
    for rank, hit in enumerate(_title_candidates(store, metadata, question, scope=effective_scope, project=project, filters=filters), 1):
        ranked.setdefault(hit.passage_id, {"hit": hit, "fts_rank": None, "title_rank": rank, "vector_rank": None, "vector_score": 0.0})
        ranked[hit.passage_id]["title_rank"] = rank
    for item in ranked.values():
        vector_data = vector.get(item["hit"].passage_id)
        if vector_data:
            item["vector_rank"], item["vector_score"] = vector_data
    scored: list[dict[str, Any]] = []
    for item in ranked.values():
        hit = item["hit"]
        rrf = (
            (1 / (RRF_K + item["fts_rank"]) if item["fts_rank"] else 0.0)
            + (1 / (RRF_K + item["title_rank"]) if item["title_rank"] else 0.0)
            + (1 / (RRF_K + item["vector_rank"]) if item["vector_rank"] else 0.0)
        )
        exact = int(question.casefold() in {hit.title.casefold(), hit.page_path.casefold()})
        total = round(
            rrf * (RRF_K + 1)
            + _authority_bonus(hit, intent, effective_scope, metadata)
            + _title_overlap_bonus(hit, question)
            + exact * 0.5,
            12,
        )
        scored.append({**item, "score": total, "rrf": rrf, "exact": bool(exact), "graph_score": 0.0, "graph_reasons": []})
    graph_candidates, graph_passages = (
        _graph_expand(
            root, store, metadata, scope=effective_scope, project=project, filters=filters,
            seed_scores={item["hit"].page_path: item["score"] for item in scored}, debug=debug,
        )
        if effective_scope != "raw"
        else ({}, [])
    )
    existing_by_path = {item["hit"].page_path: item for item in scored}
    for path, candidate in graph_candidates.items():
        if path in existing_by_path:
            existing_by_path[path]["graph_score"] = candidate.graph_score
            existing_by_path[path]["graph_reasons"] = list(candidate.rank_breakdown.graph_reasons)
            existing_by_path[path]["score"] = round(existing_by_path[path]["score"] + candidate.graph_score, 12)
    for hit in graph_passages:
        candidate = graph_candidates[hit.page_path]
        scored.append({"hit": hit, "fts_rank": None, "title_rank": None, "vector_rank": None, "vector_score": 0.0, "rrf": 0.0, "exact": False, "graph_score": candidate.graph_score, "graph_reasons": list(candidate.rank_breakdown.graph_reasons), "score": candidate.total_score})
    scored.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    # Retrieval is passage-first.  The public result list keeps the single
    # best passage per page, while the context pack may carry several passages
    # from the same page so an answer table or code block is not lost behind a
    # higher-scoring intro passage.  Both lists consume the same
    # Retrieval is page-first: the public result list keeps one best passage
    # per page, while the context pack is assembled page-by-page in reading
    # order so multi-section answers (fix steps, install checklists) survive
    # regardless of which sections carried the highest BM25 scores.
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    for item in scored:
        page_path = item["hit"].page_path
        if page_path not in selected_paths:
            selected.append(item)
            selected_paths.add(page_path)
    selected = _adaptive_expand(selected, top_k)
    selected_paths = {item["hit"].page_path for item in selected}
    hit_stats: dict[str, dict[str, Any]] = {}
    pool_by_page: dict[str, list[dict[str, Any]]] = {}
    for item in scored:
        page_path = item["hit"].page_path
        pool_by_page.setdefault(page_path, []).append(item)
        stats = hit_stats.setdefault(page_path, {"max": 0.0, "store": "active"})
        stats["max"] = max(stats["max"], item["hit"].score)
    context_items = _build_page_ordered_context(selected, store, hit_stats=hit_stats, pool_by_page=pool_by_page)
    # A true Wiki zero-result query has two sequential recovery stages: active
    # Wiki relaxed recovery, followed only when that stage is empty by the
    # dedicated raw-source store.  That path keeps raw evidence isolated from
    # Wiki ranking.  The narrow Latin-coverage path below is the explicit,
    # observable exception: it normalizes source-local ranks before mixing
    # selected active and raw evidence.  Both paths read SQLite projections
    # only, never walk raw files or load an embedding model.
    raw_fts_hits = 0
    relaxed_fts_hits = 0
    raw_index_warning = ""
    raw_fallback = False
    coverage_fallback = False
    lexical_mode = "strict"
    raw_store: RetrievalIndexStore | None = None
    query_extra_terms: list[str] = []
    query_term_variants: dict[str, list[str]] = {}
    wiki_relaxed_answered = False
    uncovered_latin_terms = (
        _uncovered_latin_terms(question, selected)
        if has_primary_recall and effective_scope in {"knowledge", "all"}
        else []
    )

    if uncovered_latin_terms:
        raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, _raw_lexical_mode = _raw_recovery_candidates(
            root,
            question,
            project=project,
            filters=filters,
            scope="raw",
            extra_terms=[],
            term_variants={},
            top_k=top_k,
        )
        raw_candidate_items = [
            item for item in raw_candidate_items if _coverage_terms(item, uncovered_latin_terms)
        ]
        if raw_candidate_items:
            merged = _merge_coverage_items(selected, raw_candidate_items, uncovered_latin_terms)
            selected = _adaptive_expand(merged, top_k)[:top_k]
            selected_paths = {item["hit"].page_path for item in selected}
            combined_items = [*scored, *raw_candidate_items]
            hit_stats = {}
            pool_by_page = {}
            for item in combined_items:
                page_path = item["hit"].page_path
                pool_by_page.setdefault(page_path, []).append(item)
                store_key = "raw" if page_path.startswith("raw/") else "active"
                stats = hit_stats.setdefault(page_path, {"max": 0.0, "store": store_key})
                stats["max"] = max(stats["max"], item["score"])
            context_items = _build_page_ordered_context(
                selected,
                store,
                raw_store=raw_store,
                hit_stats=hit_stats,
                pool_by_page=pool_by_page,
            )
            coverage_fallback = any(item["hit"].source_kind == "raw" for item in selected)
    if not has_primary_recall and effective_scope in {"knowledge", "all"}:
        # Wiki is the primary corpus.  Try its bounded relaxed projection
        # before opening the independent raw store; a successful Wiki answer
        # must not be mixed with raw evidence or even query the raw DB.
        query_extra_terms, query_term_variants, expansion_suggestions = _query_expansion(
            question,
            store,
            None,
            expansion_terms,
            project,
        )
        wiki_relaxed_items, relaxed_fts_hits, relaxed_warning = _relaxed_recovery_items(
            store,
            metadata,
            question,
            scope=effective_scope,
            project=project,
            filters=filters,
            extra_terms=query_extra_terms,
        )
        if relaxed_warning:
            status = {**status, "code": relaxed_warning}
        if wiki_relaxed_items:
            for item in wiki_relaxed_items:
                item["score"] = round(
                    item["hit"].score
                    + _authority_bonus(item["hit"], intent, effective_scope, metadata)
                    + _title_overlap_bonus(item["hit"], question)
                    + _freshness_bonus(item["hit"], metadata),
                    12,
                )
            step_counts = _step_counts_for_pages(
                sorted({item["hit"].page_path for item in wiki_relaxed_items}),
                store,
                None,
            )
            for item in wiki_relaxed_items:
                count = step_counts.get(item["hit"].page_path, 0)
                item["score"] = round(
                    item["score"] + STEP_BONUS_MAX * min(count, STEP_COUNT_FULL) / STEP_COUNT_FULL,
                    12,
                )
            wiki_relaxed_items.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
            selected = []
            selected_paths = set()
            for item in wiki_relaxed_items:
                page_path = item["hit"].page_path
                if page_path not in selected_paths:
                    selected.append(item)
                    selected_paths.add(page_path)
            selected = _adaptive_expand(selected, top_k)
            selected_paths = {item["hit"].page_path for item in selected}
            hit_stats = {}
            pool_by_page = {}
            for item in wiki_relaxed_items:
                page_path = item["hit"].page_path
                pool_by_page.setdefault(page_path, []).append(item)
                stats = hit_stats.setdefault(page_path, {"max": 0.0, "store": "active"})
                stats["max"] = max(stats["max"], item["hit"].score)
            context_items = _build_page_ordered_context(
                selected,
                store,
                hit_stats=hit_stats,
                pool_by_page=pool_by_page,
            )
            lexical_mode = "relaxed"
            wiki_relaxed_answered = True

    if not has_primary_recall and not wiki_relaxed_answered and effective_scope in {"knowledge", "all"}:
        raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, raw_lexical_mode = _raw_recovery_candidates(
            root,
            question,
            project=project,
            filters=filters,
            scope="raw",
            extra_terms=query_extra_terms,
            term_variants=query_term_variants,
            top_k=top_k,
        )
        if raw_candidate_items:
            selected = []
            selected_paths = set()
            for item in raw_candidate_items:
                page_path = item["hit"].page_path
                if page_path not in selected_paths:
                    selected.append(item)
                    selected_paths.add(page_path)
            selected = _adaptive_expand(selected, top_k)[:top_k]
            selected_paths = {item["hit"].page_path for item in selected}
            hit_stats = {}
            pool_by_page = {}
            for item in raw_candidate_items:
                page_path = item["hit"].page_path
                pool_by_page.setdefault(page_path, []).append(item)
                stats = hit_stats.setdefault(page_path, {"max": 0.0, "store": "raw"})
                stats["max"] = max(stats["max"], item["hit"].score)
            context_items = _build_page_ordered_context(
                selected, store, raw_store=raw_store, hit_stats=hit_stats, pool_by_page=pool_by_page
            )
            if raw_lexical_mode == "qualified_code" and module_qualified(question):
                lexical_mode = "qualified_code"
            elif raw_lexical_mode == "identifier_phrase":
                lexical_mode = "identifier_phrase"
            elif raw_lexical_mode == "raw_prefix":
                lexical_mode = "raw_prefix"
            elif raw_lexical_mode == "relaxed":
                lexical_mode = "relaxed"
            raw_fallback = bool(selected)
    passages = [
        ContextPassage(
            item["hit"].passage_id,
            item["hit"].page_path,
            _heading(item["hit"]),
            item["hit"].text,
            item["score"],
            "raw_evidence" if item["hit"].source_kind == "raw" else "history_evidence" if item["hit"].corpus == "history" else "formal_knowledge",
            _citation_metadata(item["hit"], provenance),
        )
        for item in context_items
    ]
    k_budget = min(hard_budget_tokens, 400 * max(top_k, len(selected)))
    packed: dict[str, Any] = (
        pack_context(passages, hard_limit=hard_budget_tokens, intent=intent, budget_scale=k_budget)
        if include_context_pack
        else {"passages": [], "citations": [], "budget": {"total": k_budget, "used": 0, "omitted": 0}}
    )
    contains_raw = any(item["hit"].source_kind == "raw" for item in selected)
    raw_fallback_response = effective_scope != "raw" and contains_raw
    fallback_payload = {
        "level": "raw" if raw_fallback_response else "none",
        "reasons": ["wiki_primary_missing_latin_coverage"] if coverage_fallback else ["wiki_zero_results"] if raw_fallback else [],
        "allowed_source_paths": [item["hit"].page_path for item in selected if item["hit"].source_kind == "raw"] if raw_fallback_response else [],
        "added_token_usage": 0,
    }
    results = [
        {
            "path": item["hit"].page_path,
            "heading": _heading(item["hit"]),
            "snippet": item["hit"].text[:240],
            "score": item["score"],
            "scores": {"fts": item["hit"].score, "vector": item["vector_score"], "rrf": item["rrf"], "graph": item["graph_score"]},
            "source_kind": item["hit"].source_kind,
            "metadata": (
                {"type": item["hit"].source_kind, "tags": []}
                if item["hit"].source_kind == "raw"
                else {"type": metadata.get(item["hit"].page_path, {}).get("type"), "tags": metadata.get(item["hit"].page_path, {}).get("tags", [])}
            ),
        }
        for item in selected
    ]
    warnings = list(dict.fromkeys([*filter(None, scope_rules), *vector_warnings, *index_warnings]))
    if raw_index_warning:
        warnings = list(dict.fromkeys([*warnings, raw_index_warning]))
    pipeline: dict[str, Any] = {"ranking_version": RANKING_POLICY_VERSION, "scope": scope, "corpus": "raw" if contains_raw else "archive" if effective_scope == "archive" else "active", "authority": "active:formal>project>raw_chat;fallback:wiki_relaxed>raw", "intent": intent, "retrieval_mode": retrieval_mode, "lexical_enabled": lexical_enabled, "lexical": {"mode": lexical_mode}, "coverage": {"uncovered_latin_terms": uncovered_latin_terms, "triggered": coverage_fallback}, "counters": {"fts_hits": len(fts), "relaxed_fts_hits": relaxed_fts_hits, "raw_fts_hits": raw_fts_hits, "vector_hits": len(vector), "graph_hits": sum(1 for item in selected if item["graph_score"] > 0), "selected": len(selected)}, "warnings": warnings, "fallback": fallback_payload}
    if debug:
        pipeline["debug"] = [{"passage_id": item["hit"].passage_id, "path": item["hit"].page_path, "fts_rank": item["fts_rank"], "vector_rank": item["vector_rank"], "rrf": item["rrf"], "graph": item["graph_score"], "graph_reasons": item["graph_reasons"], "exact_match": item["exact"], "final_score": item["score"], "coverage_terms": item.get("coverage_terms", []), "coverage_ratio": item.get("coverage_ratio"), "source_local_rank": item.get("source_local_rank"), "source_local_rrf": item.get("source_local_rrf"), "fusion_score": item.get("fusion_score"), "fusion_source": item.get("fusion_source"), "fusion_local_position": item.get("fusion_local_position")}]
    elapsed = (time.perf_counter() - started) * 1_000
    if telemetry is None or telemetry.enabled:
        QueryTelemetry(root).record(question=question, scope=scope, project=project, passage_ids=[item["hit"].passage_id for item in selected], fallback_level=str(fallback_payload["level"]), token_count=int(packed["budget"]["used"]), latency_ms=elapsed, retention_days=(telemetry.retention_days if telemetry else 90))
    response = {
        "ok": True,
        "question": question,
        "scope": scope,
        "project": project or "",
        "results": results,
        "expansion_suggestions": expansion_suggestions,
        "context_pack": packed,
        "pipeline": pipeline,
    }
    if not results:
        response.update(
            code="no_results",
            message="No indexed documentation matched the query.",
        )
    return response


def legacy_response_from_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Render a deprecated legacy context from the already-selected V2 pack.

    This adapter intentionally performs no retrieval or reranking: legacy and
    compact callers therefore cite the identical selected-results set.
    """
    result = dict(payload)
    context_pack = payload.get("context_pack")
    passages = context_pack.get("passages", []) if isinstance(context_pack, dict) else []
    result["context"] = [
        {
            "citation": item.get("citation", ""),
            "path": item.get("path", ""),
            "title": item.get("heading", ""),
            "content": item.get("content", ""),
        }
        for item in passages
        if isinstance(item, dict)
    ]
    result.setdefault("warnings", []).append("legacy_response_adapter_v2_selected_results")
    return result

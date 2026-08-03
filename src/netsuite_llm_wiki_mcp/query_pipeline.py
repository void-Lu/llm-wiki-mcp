"""Query V2: typed, passage-first retrieval behind the compact MCP response."""

from __future__ import annotations

import datetime
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from netsuite_llm_wiki_mcp.context_packer import ContextPassage, estimate_tokens, pack_context
from netsuite_llm_wiki_mcp.lexical_analyzer import identifier_phrases, module_qualified
from netsuite_llm_wiki_mcp.query_telemetry import QueryTelemetry
from netsuite_llm_wiki_mcp.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from netsuite_llm_wiki_mcp.runtime_config import EmbeddingSettings, TelemetrySettings
from netsuite_llm_wiki_mcp.vector_index import VectorIndexError, VectorIndexStore, vector_settings_from_embedding
from netsuite_llm_wiki_mcp.vector_provider import LocalBgeM3Provider, VectorProviderError
from netsuite_llm_wiki_mcp.wiki_query import QueryCandidate, _apply_graph_expansion, _build_graph


RANKING_POLICY_VERSION = "query-v2-passage-rrf-4"
RRF_K = 60
RAW_FALLBACK_LIMIT = 20
IDENTIFIER_PHRASE_BONUS = 20.0
IDENTIFIER_GUIDE_TITLE_BONUS = 12.0
IDENTIFIER_PHRASE_CANDIDATES = 200
PAGE_FILL_LIMIT = 500
PAGE_TOKEN_BUDGET = 1_200
PAGE_FULL_FILL_MIN_RATIO = 0.6
PAGE_WEAK_HIT_LIMIT = 3
FRESHNESS_BONUS_MAX = 12.0
FRESHNESS_DECAY_DAYS = 90
_GUIDE_TITLE_TERMS = (
    "installing",
    "install",
    "setup",
    "configure",
    "configuring",
    "connecting",
    "connect",
    "get started",
    "using",
    "overview",
    "faq",
    "best practices",
    "required",
    "permissions",
    "guide",
    "companion",
)


def _title_has_guide_term(title: str) -> bool:
    """Match guide words on word boundaries so ``connect`` never matches
    ``connector`` and every NetSuite AI Connector page is not boosted alike."""

    lowered = title.casefold()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered) is not None for term in _GUIDE_TITLE_TERMS)


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
    if scope not in {"auto", "knowledge", "history", "all", "archive"}:
        raise ValueError("scope must be auto, knowledge, history, all, or archive")
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


def _eligible(hit: PassageHit, metadata: dict[str, dict[str, Any]], *, scope: str) -> bool:
    fm = metadata.get(hit.page_path, {})
    lifecycle = str(fm.get("lifecycle") or fm.get("lifecycle_status") or "active")
    # An archive store contains only archived material.  Its rows must not be
    # rejected merely because their lifecycle is correctly marked archived.
    if scope != "archive" and lifecycle in {"superseded", "deprecated", "archived"}:
        return False
    if _is_source_index(hit.page_path, fm):
        return False
    history = hit.corpus == "history" or hit.source_kind in {"raw_chat", "legacy_chatlog"}
    return scope == "all" or (scope == "history" and history) or (scope == "knowledge" and not history) or scope == "archive"


def _matches_request(hit: PassageHit, metadata: dict[str, dict[str, Any]], *, project: str | None, filters: QueryFilters) -> bool:
    """Apply the same boundary filters to FTS and vector-only candidates."""
    frontmatter = metadata.get(hit.page_path, {})
    if project and str(frontmatter.get("project") or "") != project:
        return False
    if filters.type and str(frontmatter.get("type") or hit.source_kind) != filters.type:
        return False
    if filters.tags:
        tags = frontmatter.get("tags") or ()
        if not isinstance(tags, (list, tuple)) or not set(filters.tags).issubset({str(tag) for tag in tags}):
            return False
    return True


def _authority_bonus(hit: PassageHit, intent: str, scope: str, metadata: dict[str, dict[str, Any]]) -> float:
    values = {"formal_knowledge": 0.35, "concept": 0.35, "entity": 0.35, "project": 0.23, "capsule": 0.22, "raw": 0.0, "raw_chat": -0.15}
    if hit.corpus == "history" or hit.source_kind == "raw_chat":
        authority = "raw_chat"
    elif "/capsules/" in hit.page_path or hit.source_kind == "capsule":
        authority = "capsule"
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


def _vector_hits(root: Path, question: str, embedding: EmbeddingSettings | None, *, scope: str) -> tuple[dict[str, tuple[int, float]], list[str]]:
    if embedding is None or not embedding.enabled or scope == "archive":
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
        results = store.search(provider.embed_query(question), limit=settings.candidate_limit)
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
    if scope == "archive" or not seed_scores:
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
    scope: Literal["auto", "knowledge", "history", "all", "archive"] = "auto",
    project: str | None = None,
    filters: QueryFilters | None = None,
    top_k: int = 10,
    hard_budget_tokens: int = 16_000,
    embedding: EmbeddingSettings | None = None,
    telemetry: TelemetrySettings | None = None,
    debug: bool = False,
    include_context_pack: bool = True,
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
) -> dict[str, Any]:
    """Read only existing passage/vector projections and return compact context."""
    started = time.perf_counter()
    if not question.strip():
        return {"ok": False, "code": "missing_question", "error": "question is required"}
    if not 1 <= top_k <= 100:
        return {"ok": False, "code": "invalid_top_k", "error": "top_k must be between 1 and 100"}
    if retrieval_mode not in {"lexical", "vector", "hybrid"}:
        return {
            "ok": False,
            "code": "invalid_retrieval_mode",
            "error": "retrieval_mode must be lexical, vector, or hybrid",
        }
    filters = filters or QueryFilters()
    intent = classify_intent(question)
    effective_scope, scope_rules = _effective_scope(scope, intent)
    root = Path(vault_root).expanduser().resolve()
    store = RetrievalIndexStore(root, scope="archive" if effective_scope == "archive" else "active")
    status = store.status()
    if not status.get("ok"):
        return {
            "ok": True,
            "code": str(status.get("code") or "index_unavailable"),
            "message": "The retrieval index is unavailable; the query was not executed.",
            "question": question,
            "scope": scope,
            "results": [],
            "context_pack": {"passages": [], "citations": [], "budget": {"total": min(hard_budget_tokens, 4_000), "used": 0}},
            "pipeline": {"ranking_version": RANKING_POLICY_VERSION, "warnings": [str(status.get("code"))], "fallback": {"level": "none", "reasons": ["index_unavailable"], "allowed_source_paths": []}},
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
    vector, vector_warnings = (
        _vector_hits(root, question, embedding, scope=effective_scope)
        if retrieval_mode != "lexical"
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
    graph_candidates, graph_passages = _graph_expand(
        root, store, metadata, scope=effective_scope, project=project, filters=filters,
        seed_scores={item["hit"].page_path: item["score"] for item in scored}, debug=debug,
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
    selected = selected[:top_k]
    selected_paths = {item["hit"].page_path for item in selected}
    hit_stats: dict[str, dict[str, Any]] = {}
    pool_by_page: dict[str, list[dict[str, Any]]] = {}
    for item in scored:
        page_path = item["hit"].page_path
        pool_by_page.setdefault(page_path, []).append(item)
        stats = hit_stats.setdefault(page_path, {"max": 0.0, "store": "active"})
        stats["max"] = max(stats["max"], item["hit"].score)
    context_items = _build_page_ordered_context(selected, store, hit_stats=hit_stats, pool_by_page=pool_by_page)
    # A true Wiki zero-result query recovers evidence from two places: the
    # active store under relaxed multilingual lexical recovery, and the
    # dedicated raw-source store.  Both stores share the same tokenizer and
    # schema, so their bm25 scores are directly comparable and the candidates
    # are ranked together.  Wiki pages are curated and carry an authority
    # bonus when only wiki candidates exist; against raw evidence the raw
    # bm25 score is the comparable lexical signal.  The fallback still reads
    # SQLite projections only, never walks raw files or loads an embedding
    # model.
    raw_fts_hits = 0
    relaxed_fts_hits = 0
    raw_index_warning = ""
    raw_fallback = False
    lexical_mode = "strict"
    if not has_primary_recall and effective_scope in {"knowledge", "all"}:
        # Stage one: bounded multilingual recovery over the curated wiki.
        relaxed_items: list[dict[str, Any]] = []
        try:
            relaxed_hits = store.search_fts(
                question,
                limit=IDENTIFIER_PHRASE_CANDIDATES,
                project=project,
                page_type=filters.type,
                tags=list(filters.tags),
                mode="relaxed",
            )
        except RetrievalIndexError as exc:
            relaxed_hits = []
            status = {**status, "code": exc.code}
        relaxed_fts_hits = len(relaxed_hits)
        for rank, hit in enumerate(relaxed_hits, 1):
            if (
                not _eligible(hit, metadata, scope=effective_scope)
                or not _matches_request(hit, metadata, project=project, filters=filters)
            ):
                continue
            relaxed_items.append(
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
        # Stage two: dedicated raw-store fallback (strict, then slash-qualified
        # identifiers, then multi-word English identifiers, then relaxed).
        # A slash-qualified identifier wins outright only for a genuine module
        # namespace such as ``N/record``; a generic slash term such as
        # ``List/Record`` stays in the merged pool so curated wiki evidence is
        # not starved by it.
        raw_items: list[dict[str, Any]] = []
        raw_lexical_mode = "strict"
        raw_store = RetrievalIndexStore(root, scope="raw")
        raw_status = raw_store.status()
        if raw_status.get("ok"):
            try:
                raw_hits = raw_store.search_fts(
                    question,
                    limit=min(top_k, RAW_FALLBACK_LIMIT),
                    project=project,
                    page_type=filters.type,
                    tags=list(filters.tags),
                )
                qualified_code_hits = raw_store.search_fts(
                    question,
                    limit=min(top_k, RAW_FALLBACK_LIMIT),
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
                            limit=IDENTIFIER_PHRASE_CANDIDATES,
                            project=project,
                            page_type=filters.type,
                            tags=list(filters.tags),
                            mode="identifier_phrase",
                        )
                    except RetrievalIndexError:
                        identifier_phrase_hits = []
                # A slash-qualified module identifier is a stronger signal
                # than incidental English terms such as "record" or "methods".
                # Prefer its dedicated lookup whenever available, regardless
                # of whether the surrounding question is Chinese or English.
                if qualified_code_hits and module_qualified(question):
                    raw_hits = qualified_code_hits
                    raw_lexical_mode = "qualified_code"
                elif identifier_phrase_hits:
                    # A multi-word English run such as ``NetSuite AI
                    # Connector`` names a precise feature.  Its strict AND
                    # lookup is a stronger recovery signal than incidental
                    # Chinese bigram overlap, so it replaces the noisy
                    # relaxed raw pool.
                    raw_hits = identifier_phrase_hits
                    raw_lexical_mode = "identifier_phrase"
                elif not raw_hits:
                    raw_hits = raw_store.search_fts(
                        question,
                        limit=min(top_k, RAW_FALLBACK_LIMIT),
                        project=project,
                        page_type=filters.type,
                        tags=list(filters.tags),
                        mode="relaxed",
                    )
                    if raw_hits:
                        raw_lexical_mode = "relaxed"
            except RetrievalIndexError as exc:
                raw_hits = []
                raw_index_warning = exc.code
            raw_fts_hits = len(raw_hits)
            query_identifier_phrases = identifier_phrases(question) if raw_lexical_mode == "identifier_phrase" else []
            for rank, hit in enumerate(raw_hits, 1):
                # Raw pages are not present in the active-store metadata map,
                # so project/type/tag boundaries are already enforced by the
                # raw FTS query itself.  Only eligibility (lifecycle and
                # navigation metadata) is re-checked here.
                if not _eligible(hit, metadata, scope=effective_scope):
                    continue
                score = hit.score
                if query_identifier_phrases:
                    compact = f"{hit.title}\n{hit.text}".casefold()
                    if any(phrase in compact for phrase in query_identifier_phrases):
                        score += IDENTIFIER_PHRASE_BONUS
                    if _title_has_guide_term(hit.title):
                        score += IDENTIFIER_GUIDE_TITLE_BONUS
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
        else:
            raw_index_warning = str(raw_status.get("code") or "raw_index_unavailable")
        if raw_lexical_mode == "qualified_code" and module_qualified(question):
            # A slash-qualified module identifier is a precise, strong signal.
            # The dedicated raw lookup wins outright over incidental wiki term
            # overlap (for example "record" or "methods").
            merged_items = raw_items
        elif raw_lexical_mode == "identifier_phrase":
            # A multi-word English identifier is the strongest recovery
            # signal.  It leads the merged pool; curated wiki evidence from
            # relaxed recovery is still merged afterwards so it is never
            # starved, while incidental relaxed raw noise is dropped.
            for item in relaxed_items:
                item["score"] = round(
                    item["hit"].score
                    + _authority_bonus(item["hit"], intent, effective_scope, metadata)
                    + _title_overlap_bonus(item["hit"], question)
                    + _freshness_bonus(item["hit"], metadata),
                    12,
                )
            merged_items = raw_items + relaxed_items
        else:
            merged_items = relaxed_items + raw_items
            if relaxed_items and not raw_items:
                # Wiki-only recovery keeps the full curation bonus so an
                # overlapping knowledge page still outranks weaker lexical
                # neighbours.
                for item in merged_items:
                    item["score"] = round(
                        item["hit"].score
                        + _authority_bonus(item["hit"], intent, effective_scope, metadata)
                        + _title_overlap_bonus(item["hit"], question)
                        + _freshness_bonus(item["hit"], metadata),
                        12,
                    )
            else:
                # When raw evidence is also present, curated wiki pages compete
                # on lexical score plus their freshness signal, while raw hits
                # keep a bare score: a raw answer with a strong exact match
                # must still beat a generic wiki overview, and a broad raw
                # OR-match (clipping, quiz note) cannot silently outrank a
                # recent wiki fix note.
                for item in relaxed_items:
                    item["score"] = round(
                        item["hit"].score
                        + _freshness_bonus(item["hit"], metadata),
                        12,
                    )
        if merged_items:
            merged_items.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
            selected = []
            selected_paths = set()
            for item in merged_items:
                page_path = item["hit"].page_path
                if page_path not in selected_paths:
                    selected.append(item)
                    selected_paths.add(page_path)
            selected = selected[:top_k]
            selected_paths = {item["hit"].page_path for item in selected}
            hit_stats = {}
            pool_by_page = {}
            for item in merged_items:
                page_path = item["hit"].page_path
                pool_by_page.setdefault(page_path, []).append(item)
                store_key = "raw" if page_path.startswith("raw/") else "active"
                stats = hit_stats.setdefault(page_path, {"max": 0.0, "store": store_key})
                stats["max"] = max(stats["max"], item["hit"].score)
            context_items = _build_page_ordered_context(
                selected, store, raw_store=raw_store, hit_stats=hit_stats, pool_by_page=pool_by_page
            )
            if raw_lexical_mode == "qualified_code" and module_qualified(question):
                lexical_mode = "qualified_code"
            elif raw_lexical_mode == "identifier_phrase":
                lexical_mode = "identifier_phrase"
            elif raw_lexical_mode == "relaxed" or relaxed_items:
                lexical_mode = "relaxed"
            # A merged result set is a raw fallback only when the best-ranked
            # evidence is a raw source; a curated wiki page on top means the
            # wiki answered the question and raw evidence is supplementary.
            raw_fallback = bool(selected) and selected[0]["hit"].source_kind == "raw"
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
    packed: dict[str, Any] = pack_context(passages, hard_limit=hard_budget_tokens, intent=intent) if include_context_pack else {"passages": [], "citations": [], "budget": {"total": min(hard_budget_tokens, 16_000), "used": 0, "omitted": 0}}
    fallback_payload = {
        "level": "raw" if raw_fallback else "none",
        "reasons": ["wiki_zero_results"] if raw_fallback else [],
        "allowed_source_paths": [item["hit"].page_path for item in selected if item["hit"].source_kind == "raw"] if raw_fallback else [],
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
    warnings = [*filter(None, scope_rules), *vector_warnings]
    if raw_index_warning:
        warnings.append(raw_index_warning)
    pipeline: dict[str, Any] = {"ranking_version": RANKING_POLICY_VERSION, "scope": scope, "corpus": "raw" if raw_fallback else "archive" if effective_scope == "archive" else "active", "authority": "active:formal>project>capsule>raw_chat;fallback:active_relaxed>raw_identifier>raw", "intent": intent, "retrieval_mode": retrieval_mode, "lexical": {"mode": lexical_mode}, "counters": {"fts_hits": len(fts), "relaxed_fts_hits": relaxed_fts_hits, "raw_fts_hits": raw_fts_hits, "vector_hits": len(vector), "graph_hits": sum(1 for item in selected if item["graph_score"] > 0), "selected": len(selected)}, "warnings": warnings, "fallback": fallback_payload}
    if debug:
        pipeline["debug"] = [{"passage_id": item["hit"].passage_id, "path": item["hit"].page_path, "fts_rank": item["fts_rank"], "vector_rank": item["vector_rank"], "rrf": item["rrf"], "graph": item["graph_score"], "graph_reasons": item["graph_reasons"], "exact_match": item["exact"], "final_score": item["score"]} for item in selected]
    elapsed = (time.perf_counter() - started) * 1_000
    if telemetry is None or telemetry.enabled:
        QueryTelemetry(root).record(question=question, scope=scope, project=project, passage_ids=[item["hit"].passage_id for item in selected], fallback_level=str(fallback_payload["level"]), token_count=int(packed["budget"]["used"]), latency_ms=elapsed, retention_days=(telemetry.retention_days if telemetry else 90))
    response = {"ok": True, "question": question, "scope": scope, "project": project or "", "results": results, "context_pack": packed, "pipeline": pipeline}
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

"""Execution state and bounded recovery helpers for one Query V2 call."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from retrieval.candidate_items import candidate_item, with_fusion
from retrieval.graph_retrieval import QueryCandidate, apply_graph_expansion, build_graph
from retrieval.lexical_analyzer import (
    QualifiedIdentifier,
    edit_distance,
    extract_qualified_identifiers,
    extract_namespace_wildcards,
    has_qualified_identifier,
    identifier_phrases,
    plan_query,
    tokens,
)
from retrieval.metadata_filters import QUERY_METADATA_FILTERS, normalize_metadata_filters, page_matches_filters
from retrieval.query_cancellation import QueryCancellationContext
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
from retrieval.vector_index import VectorIndexError, VectorIndexStore, vector_settings_from_embedding
from retrieval.vector_provider import LocalBgeM3Provider, VectorProviderError
from runtime.runtime_config import EmbeddingSettings


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
BATCH_MAX_ITEMS = 40
BATCH_WORKERS = 4
BATCH_CANDIDATE_POOL_LIMIT = 80
BATCH_MIN_RELEVANCE_SCORE = 0.05
BATCH_HIGH_SCORE_RATIO = 0.82
BATCH_AMBIGUITY_RATIO = 0.93
BATCH_MAX_SELECTED_CANDIDATES = 12
DISCOVERY_CANDIDATE_LIMIT = 240
DISCOVERY_PAGE_LIMIT = 32
DISCOVERY_SOURCE_PAGE_LIMIT = 8
_DISCOVERY_INTENT_RE = re.compile(
    r"(?:\blist\b|\ball\b|\beach\b|\bevery\b|\bvarious\b|\bdifferent\b|\btypes?\b|\bavailable\b|\bmodules?\b|\bcatalog\b|\bdirectory\b|\boverview\b|列出|有哪些|各|每|分别|类型|目录|模块|清单|列表)",
    re.I,
)
_DISCOVERY_ANCHOR_STOPWORDS = frozenset(
    {
        "all",
        "available",
        "catalog",
        "directory",
        "list",
        "module",
        "modules",
        "overview",
        "reference",
        "show",
        "which",
    }
)
_DISCOVERY_CJK_ALIASES = {
    "模块": ("module", "modules"),
    "脚本": ("script", "scripts"),
    "类型": ("type", "types"),
    "目录": ("catalog", "directory", "overview", "list"),
    "清单": ("catalog", "directory", "list"),
    "列表": ("list", "listing"),
}
_STEP_ITEM_RE = re.compile(r"(?:^\s*\d{1,3}\s*[\.\)、]|^\s*第[一二三四五六七八九十百\d]+步)", re.M)
def probe_hit(
    path: str,
    title: str,
    *,
    corpus: str = "active",
    authority: str = "",
    source_kind: str = "",
) -> PassageHit:
    """Build a placeholder hit used only by eligibility/filter probes."""

    return PassageHit("", path, title, (), "", 0.0, corpus, authority, source_kind)


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


@dataclass(frozen=True)
class QueryFilters:
    type: str | None = None
    tags: tuple[str, ...] = ()
    path_prefix: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "QueryFilters":
        normalized = normalize_metadata_filters(value, allowed=QUERY_METADATA_FILTERS, preserve_path_trailing=True)
        return cls(
            normalized.get("type"),
            tuple(normalized.get("tags", ())),
            normalized.get("path_prefix"),
        )


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


def _is_source_index(path: str, frontmatter: Mapping[str, Any]) -> bool:
    page_type = str(frontmatter.get("type") or "").casefold()
    return (
        page_type in {"source_index", "source-index", "index", "source_summary"}
        or path.endswith("/index.md") and "/sources/" in path and "/capsules/" not in path
        or re.search(r"/(?:manifest|_toc_manifest|_path_aliases)\.json$", path.casefold()) is not None
    )


def _is_retired_source_namespace(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized == "wiki/sources" or normalized.startswith("wiki/sources/")


def _eligible(hit: PassageHit, metadata: Mapping[str, Mapping[str, Any]], *, scope: str) -> bool:
    fm = metadata.get(hit.page_path, {})
    if scope == "raw":
        normalized_path = hit.page_path.replace("\\", "/")
        if (
            not normalized_path.startswith("raw/sources/")
            or normalized_path.startswith("raw/sources/chat/")
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


def _matches_request(
    hit: PassageHit,
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    project: str | None,
    filters: QueryFilters,
) -> bool:
    """Apply the same boundary filters to FTS and vector-only candidates."""
    frontmatter = metadata.get(hit.page_path, {})
    return page_matches_filters(
        frontmatter,
        hit.source_kind,
        project=project,
        page_type=filters.type,
        tags=filters.tags,
        path_prefix=filters.path_prefix,
        page_path=hit.page_path,
    )


@dataclass(frozen=True)
class AdaptiveCandidateScorePolicy:
    """Local score policy used only by the entity batch fan-out."""

    candidate_pool_limit: int = BATCH_CANDIDATE_POOL_LIMIT
    minimum_relevance_score: float = BATCH_MIN_RELEVANCE_SCORE
    high_score_ratio: float = BATCH_HIGH_SCORE_RATIO
    ambiguity_ratio: float = BATCH_AMBIGUITY_RATIO
    max_selected_candidates: int = BATCH_MAX_SELECTED_CANDIDATES


@dataclass(frozen=True)
class _EntityStoreSpec:
    """One entity-batch store paired with its invocation snapshot."""

    store: RetrievalIndexStore
    scope: str
    snapshot: QueryCorpusSnapshot


_DISCOVERY_HEADING_RE = re.compile(r"^\s*(?P<marks>#{2,6})\s+(?P<label>.+?)\s*$")
_DISCOVERY_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}\s*[.)、]\s+|[一二三四五六七八九十百\d]+\s*[、.)]\s+)(?P<label>.+?)\s*$")
_DISCOVERY_LINK_RE = re.compile(r"\[([^\]]+)\]\s*\([^)]*\)")
_DISCOVERY_STOPWORDS = frozenset(
    {
        "available",
        "example",
        "examples",
        "list",
        "module",
        "modules",
        "name",
        "names",
        "note",
        "notes",
        "overview",
        "script",
        "scripts",
        "type",
        "types",
        "usage",
    }
)


def _discovery_fragments(line: str) -> list[tuple[str, str, str]]:
    """Return structured labels, including rows flattened into one passage."""

    heading = _DISCOVERY_HEADING_RE.match(line)
    if heading:
        label = re.sub(r"^\s*\d{1,3}\s*[.)、]\s*", "", heading.group("label"))
        return [(f"heading:{len(heading.group('marks'))}", label, "heading")]
    # The retrieval index may split a long Markdown table passage in the
    # middle of a row, so a chunk need not retain both outer table pipes.
    if line.count("|") >= 3 and (line.lstrip().startswith("|") or " | " in line):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows: list[list[str]] = []
        current: list[str] = []
        for cell in cells:
            if cell:
                current.append(cell)
            elif current:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        fragments: list[tuple[str, str, str]] = []
        starts_at_table_boundary = line.lstrip().startswith("|")
        for row_index, row in enumerate(rows):
            # Passage chunking can begin in the middle of a long table row.
            # Such a leading cell contains the tail of a description or a
            # linked URL, not the entity in the first column.  Later rows in
            # the same chunk still begin at an explicit ``| |`` boundary and
            # remain eligible.  A normal Markdown table line starts with ``|``
            # and is unaffected.
            if row_index == 0 and not starts_at_table_boundary:
                continue
            if not row or all(re.fullmatch(r":?(?:-+\s*){2,}:?", cell) for cell in row):
                continue
            fragments.append(("table", row[0], "table"))
        return fragments
    item = _DISCOVERY_LIST_RE.match(line)
    if item:
        return [("list", item.group("label"), "list")]
    return []


def _clean_discovery_label(value: str) -> str:
    if "|" in value:
        value = value.split("|", 1)[0]
    value = re.sub(r"\[([^\]]+)\]\s*\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_]", "", value).strip()
    value = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", value)
    value = re.split(r"\s+(?:[-–—]|:|：)\s+|[:：]", value, maxsplit=1)[0].strip()
    return value.strip("-–—,，;；。.")


def _generic_discovery_entity(label: str) -> tuple[str, tuple[str, ...]] | None:
    cleaned = _clean_discovery_label(label)
    words = re.findall(r"[A-Za-z0-9_\u3400-\u9fff][A-Za-z0-9_\u3400-\u9fff -]*", cleaned)
    if not words or len(cleaned) > 80 or len(cleaned.split()) > 8:
        return None
    canonical = re.sub(r"\s+", " ", cleaned.casefold()).strip()
    if not canonical or canonical in _DISCOVERY_STOPWORDS or canonical.split()[0] in _DISCOVERY_STOPWORDS:
        return None
    if not re.search(r"[A-Za-z\u3400-\u9fff]", canonical):
        return None
    return canonical, (cleaned, canonical)


def _discovery_candidate_for_fragment(fragment: str) -> list[tuple[str, tuple[str, ...], QualifiedIdentifier | None]]:
    """Extract qualified IDs first, then a structured generic entity label."""

    visible_fragment = re.sub(r"\[([^\]]+)\]\s*\([^)]*\)", r"\1", fragment)
    visible_fragment = re.sub(r"\s*/\s*", "/", visible_fragment).strip()
    link_labels = _DISCOVERY_LINK_RE.findall(fragment)
    candidate_fragments = link_labels or [visible_fragment]
    qualified: list[tuple[str, tuple[str, ...], QualifiedIdentifier | None]] = []
    for candidate_fragment in candidate_fragments:
        candidate_text = re.sub(r"\s*/\s*", "/", candidate_fragment).strip()
        identifiers = extract_qualified_identifiers(candidate_text)
        for identifier in identifiers:
            # A space-separated phrase such as ``Client Script`` is a generic
            # enumeration label, not a qualified identifier.  Keep explicit
            # boundary forms deterministic.  A bare title-case word such as
            # ``Object`` or ``Customer`` is also a generic entity label, even
            # though the query-time parser accepts the compact ``Nauth``
            # alias form: discovery reads source cells, where qualified IDs
            # appear with an explicit boundary.
            explicit_boundary = "/" in candidate_text or re.search(r"[A-Za-z0-9][-_]\s*[A-Za-z0-9]", candidate_text) is not None
            spaced_entity = re.fullmatch(r"\s*[A-Za-z]\s+[A-Za-z][A-Za-z0-9_-]*\s*", candidate_text) is not None
            single_segment_prefix = len(identifier.prefix_segments) == 1 and len(identifier.prefix_segments[0]) == 1 and spaced_entity
            if not explicit_boundary and candidate_text.casefold() in _DISCOVERY_STOPWORDS:
                continue
            match = re.search(
                rf"(?<![a-z0-9_]){re.escape(identifier.prefix_segments[0])}"
                rf"(?P<separator>/|[-_]|[ \\t]+)"
                rf"[a-z][a-z0-9_-]*",
                candidate_text,
                re.I,
            )
            if match:
                prefix_is_single_letter = len(identifier.prefix_segments) == 1 and len(identifier.prefix_segments[0]) == 1
                starts_with_identifier = not candidate_text[: match.start()].strip(" -*+`[]()")
                separator = match.group("separator")
                # Natural prose frequently contains slash or hyphen phrases
                # such as ``reading/writing`` and ``hard-code``.  Boundary
                # aliases remain supported for one-letter namespaces (for
                # example N/auth, N auth, N-auth, and N_auth), while a
                # multi-letter qualified name must start the structured cell.
                if separator in {"-", "_"} and not prefix_is_single_letter:
                    continue
                if not starts_with_identifier and not prefix_is_single_letter:
                    continue
                if not starts_with_identifier and identifier.prefix_segments == ("a",):
                    continue
            if explicit_boundary or single_segment_prefix:
                qualified.append((identifier.canonical_id, identifier.aliases, identifier))
        if qualified:
            continue
        generic = _generic_discovery_entity(candidate_text)
        if generic is None:
            continue
        canonical, aliases = generic
        qualified.append((canonical, aliases, None))
    return qualified


def _discovery_requested(question: str) -> bool:
    """Recognize listing/wildcard intent without widening ordinary queries."""

    return bool(extract_namespace_wildcards(question) or _DISCOVERY_INTENT_RE.search(question))


def _discovery_anchor_query(question: str) -> str:
    """Keep Latin anchors independent from CJK bigrams and wildcard prefixes."""

    plan = plan_query(question)
    wildcard_prefixes = {segment for item in plan.namespace_wildcards for segment in item.prefix_segments}
    anchors = [
        term
        for term in plan.latin_terms
        if len(term) > 1 and term not in wildcard_prefixes and term not in _DISCOVERY_ANCHOR_STOPWORDS
    ]
    return " ".join(dict.fromkeys(anchors))


def _discovery_focus_terms(question: str) -> set[str]:
    """Return topic words for title/path scoring, including CJK synonyms."""

    plan = plan_query(question)
    terms = {
        term
        for term in plan.latin_terms
        if len(term) > 1 and term not in _DISCOVERY_ANCHOR_STOPWORDS
    }
    for phrase, aliases in _DISCOVERY_CJK_ALIASES.items():
        if phrase in question:
            terms.update(aliases)
    return terms


def _wildcard_evidence_names(question: str, text: str) -> set[str]:
    """Find distinct qualified names for a wildcard from indexed passage text."""

    names: set[str] = set()
    for wildcard in extract_namespace_wildcards(question):
        prefix = r"\s*\.\s*".join(re.escape(segment) for segment in wildcard.prefix_segments)
        pattern = re.compile(
            rf"(?<![a-z0-9]){prefix}\s*/\s*"
            rf"(?P<name>[a-z][a-z0-9_-]*(?:\s*/\s*[a-z][a-z0-9_-]*)*)"
            rf"(?![a-z0-9_/])",
            re.I,
        )
        for match in pattern.finditer(text):
            name = re.sub(r"\s*/\s*", "/", match.group("name")).casefold()
            if name not in {"a", "na", "none", "null"}:
                names.add(f"{wildcard.canonical_prefix}/{name}")
    return names


def _discovery_source_items(
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    question: str,
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
) -> list[dict[str, Any]]:
    """Find bounded discovery pages from an existing SQLite projection.

    Relaxed FTS is only a recall source here.  The caller still requires
    same-page structured enumeration before any entity can reach the batch.
    No source files or index lifecycle methods are touched.
    """

    anchor_query = _discovery_anchor_query(question)
    hits: list[PassageHit] = []
    if anchor_query:
        try:
            hits = store.search_fts(
                anchor_query,
                limit=DISCOVERY_CANDIDATE_LIMIT,
                project=project,
                page_type=filters.type,
                tags=list(filters.tags),
                mode="strict",
            )
            if not hits:
                hits = store.search_fts(
                    anchor_query,
                    limit=DISCOVERY_CANDIDATE_LIMIT,
                    project=project,
                    page_type=filters.type,
                    tags=list(filters.tags),
                    mode="relaxed",
                )
        except RetrievalIndexError:
            hits = []

    page_paths: list[str] = []
    seen_paths: set[str] = set()
    for hit in hits:
        if hit.page_path in seen_paths:
            continue
        if _eligible(hit, metadata, scope=scope) and _matches_request(hit, metadata, project=project, filters=filters):
            seen_paths.add(hit.page_path)
            page_paths.append(hit.page_path)

    # Use bounded title/path metadata on every discovery pass.  A pure
    # wildcard has no useful FTS term after its prefix is removed, while a
    # multilingual or broad listing query can fill the FTS page pool with
    # generic product pages.  Prefer catalog signals in the title over mere
    # path overlap, then let same-page structured evidence validate the page.
    raw_anchor_terms = set(tokens(anchor_query))
    wildcard = bool(extract_namespace_wildcards(question))
    eligible_pages: list[tuple[str, str, str]] = []
    pages = snapshot.pages if snapshot is not None else store.page_candidates()
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="discovery")
        path = str(page["path"])
        title = str(page.get("title") or "")
        probe = probe_hit(
            path,
            title,
            corpus=str(page.get("corpus") or "active"),
            authority=str(page.get("authority") or ""),
            source_kind=str(page.get("source_kind") or ""),
        )
        if not _eligible(probe, metadata, scope=scope) or not _matches_request(probe, metadata, project=project, filters=filters):
            continue
        eligible_pages.append((path, title, f"{title} {path}".casefold()))

    term_frequency = {
        term: sum(term in title_path for _path, _title, title_path in eligible_pages)
        for term in raw_anchor_terms
    }
    common_term_limit = max(1, len(eligible_pages) // 2)
    anchor_terms = {
        term for term in raw_anchor_terms if term_frequency.get(term, 0) <= common_term_limit
    }
    focus_terms = {
        term
        for term in _discovery_focus_terms(question)
        if term not in raw_anchor_terms or term_frequency.get(term, 0) <= common_term_limit
    }

    def contains_term(value: str, term: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value, re.I) is not None

    metadata_candidates: list[tuple[int, str]] = []
    clean_catalog_paths: set[str] = set()
    catalog_paths: set[str] = set()
    for path, title, title_path in eligible_pages:
        overlap = sum(contains_term(title_path, term) for term in anchor_terms)
        title_focus = sum(contains_term(title, term) for term in focus_terms)
        path_focus = sum(contains_term(path, term) for term in focus_terms)
        title_catalog = bool(re.search(r"catalog|directory|overview|modules?|types?|reference|list|目录|模块|类型|清单|列表", title, re.I))
        path_catalog = bool(re.search(r"catalog|directory|overview|modules?|types?|reference|list|目录|模块|类型|清单|列表", path, re.I))
        plural_catalog = bool(re.search(r"\b(?:modules|types|catalogs|directories|lists|listings)\b", title, re.I))
        noisy_title = bool(re.search(r"sample|reference|difference|tutorial|code|entry points?|api|file", title, re.I))
        explicit_catalog_title = bool(re.search(r"catalog|directory|overview|index|contents|list(?:ing)?|目录|模块|类型|清单|列表", title, re.I))
        strong_catalog = plural_catalog or explicit_catalog_title
        if strong_catalog:
            catalog_paths.add(path)
            if not noisy_title:
                clean_catalog_paths.add(path)
        likely_catalog = title_catalog or path_catalog
        if overlap or title_focus or (wildcard and likely_catalog):
            score = (
                overlap
                + title_focus * 2
                + path_focus
                + (3 if title_catalog else 0)
                + (1 if path_catalog else 0)
                + (2 if plural_catalog else 0)
                - (5 if noisy_title else 0)
            )
            metadata_candidates.append((score, path))

    qualified_names_by_path: dict[str, set[str]] = {}
    if wildcard and metadata_candidates:
        probe_paths = [
            path
            for _score, path in sorted(metadata_candidates, key=lambda item: (-item[0], item[1]))[
                :DISCOVERY_CANDIDATE_LIMIT
            ]
        ]
        for hit in store.passages_for_pages(probe_paths, limit_per_page=PASSAGE_PROBE_LIMIT):
            qualified_names_by_path.setdefault(hit.page_path, set()).update(
                _wildcard_evidence_names(question, hit.text)
            )
        max_qualified_names = max(
            (len(names) for names in qualified_names_by_path.values()), default=0
        )
        minimum_qualified_names = max(2, max_qualified_names // 10)
        qualified_paths = {
            path
            for path, names in qualified_names_by_path.items()
            if len(names) >= minimum_qualified_names
        }
        if qualified_paths:
            metadata_candidates = [
                item for item in metadata_candidates if item[1] in qualified_paths
            ]

    sorted_metadata_candidates = sorted(
        metadata_candidates, key=lambda item: (-item[0], item[1])
    )
    if wildcard and qualified_names_by_path:
        metadata_paths = [
            path
            for _score, path in sorted(
                sorted_metadata_candidates,
                key=lambda item: (-len(qualified_names_by_path.get(item[1], ())), -item[0], item[1]),
            )
        ][:DISCOVERY_SOURCE_PAGE_LIMIT]
    elif focus_terms and sorted_metadata_candidates:
        # A listing question should prefer an explicit catalog/overview page
        # when one is available.  Detail/reference pages often inherit the
        # same topic words through their parent directory, but their
        # structured rows enumerate entry points, methods, or examples rather
        # than the requested top-level entities.  This is a generic title
        # signal; it does not name a product or document path.
        catalog_candidates = [
            item for item in sorted_metadata_candidates if item[1] in clean_catalog_paths
        ] or [item for item in sorted_metadata_candidates if item[1] in catalog_paths]
        ranking_candidates = catalog_candidates or sorted_metadata_candidates
        best_score = ranking_candidates[0][0]
        metadata_paths = [
            path
            for score, path in ranking_candidates
            if score >= best_score - 1
        ][:DISCOVERY_SOURCE_PAGE_LIMIT]
    else:
        metadata_paths = [path for _score, path in sorted_metadata_candidates]
    # Once the metadata pass has found candidate catalog pages, treat that
    # bounded set as authoritative.  Merging the original relaxed-FTS page
    # pool back in here defeats the title/path and same-page-evidence filters:
    # generic product pages can re-enter the discovery context even though
    # they contributed no structured enumeration evidence.  Keep FTS paths as
    # a fallback only when metadata did not produce any candidate page.
    if metadata_paths:
        page_paths = list(dict.fromkeys(metadata_paths))[:DISCOVERY_PAGE_LIMIT]
    else:
        page_paths = page_paths[:DISCOVERY_PAGE_LIMIT]

    passages = store.passages_for_pages(page_paths, limit_per_page=PASSAGE_SCAN_LIMIT)
    return [
        candidate_item(hit, score=hit.score)
        for hit in passages
    ]


def _compact_discovery_aliases(aliases: Sequence[str]) -> list[str]:
    """Deduplicate boundary variants by normalizing separators and case.

    ``QualifiedIdentifier`` emits slash, space, compact, hyphen and underscore
    forms, which explode the discovery payload for long multi-segment names.
    Matching uses ``_alias_pattern``, which already collapses all those
    separators to one, so each normalized spelling only needs one alias.
    """

    seen: set[str] = set()
    compact: list[str] = []
    for alias in aliases:
        if not alias:
            continue
        key = re.sub(r"[/\s_-]+", "/", alias).casefold().strip("/")
        if key not in seen:
            seen.add(key)
            compact.append(alias)
    return compact


def _discover_enumerated_entities(
    selected: Sequence[dict[str, Any]],
    context_items: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Discover entities only from same-page structured evidence."""

    ordered_items: list[dict[str, Any]] = []
    seen_passages: set[tuple[str, str]] = set()
    for item in [*selected, *context_items]:
        hit = item.get("hit")
        if not isinstance(hit, PassageHit):
            continue
        key = (hit.page_path, hit.passage_id)
        if key in seen_passages:
            continue
        seen_passages.add(key)
        ordered_items.append(item)

    by_page: dict[str, dict[str, Any]] = {}
    for item in ordered_items:
        hit = item["hit"]
        page = by_page.setdefault(hit.page_path, {"groups": {}, "entities": {}, "order": len(by_page), "next_order": 0})
        lines = hit.text.splitlines()
        for line_index, line in enumerate(lines):
            if (
                line.lstrip().startswith("|")
                and line_index + 1 < len(lines)
                and re.fullmatch(r"\s*\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?\s*", lines[line_index + 1])
            ):
                continue
            for group, label, kind in _discovery_fragments(line):
                for canonical, aliases, identifier in _discovery_candidate_for_fragment(label):
                    entities = page["entities"]
                    entity = entities.get(canonical)
                    if entity is None:
                        entity = {
                            "canonical_id": canonical,
                            "aliases": _compact_discovery_aliases(aliases),
                            "evidence": {
                                "path": hit.page_path,
                                "passage_id": hit.passage_id,
                                "heading": _heading(hit),
                                "excerpt": line.strip()[:360],
                            },
                            "evidence_fragments": [],
                            "_identifier": identifier,
                            "_order": page["next_order"],
                        }
                        entities[canonical] = entity
                        page["next_order"] += 1
                    else:
                        entity["evidence_fragments"].append(
                            {
                                "path": hit.page_path,
                                "passage_id": hit.passage_id,
                                "heading": _heading(hit),
                                "excerpt": line.strip()[:360],
                            }
                        )
                    page["groups"].setdefault((group, kind), set()).add(canonical)

    valid_pages: list[tuple[str, dict[str, Any], set[str]]] = []
    for path, page in by_page.items():
        table_groups = [
            members
            for (group, kind), members in page["groups"].items()
            if kind == "table" and len(members) >= 2
        ]
        # A catalog table is stronger evidence than unrelated bullet lists on
        # the same page (for example, implementation guidelines or samples).
        # Keep list/heading enumeration as a fallback for pages that have no
        # multi-row table at all.
        valid_groups = table_groups or [
            members for members in page["groups"].values() if len(members) >= 2
        ]
        valid_entities = set().union(*valid_groups) if valid_groups else set()
        if len(valid_entities) >= 2:
            valid_pages.append((path, page, valid_entities))

    candidates: list[dict[str, Any]] = []
    for path, page, valid_entities in valid_pages:
        for canonical, entity in page["entities"].items():
            if canonical not in valid_entities:
                continue
            candidates.append(entity)
    candidates.sort(
        key=lambda item: (
            next(index for index, entry in enumerate(ordered_items) if entry["hit"].page_path == item["evidence"]["path"]),
            item.get("_order", 0),
        )
    )
    unique_candidates: list[dict[str, Any]] = []
    by_canonical: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        existing = by_canonical.get(candidate["canonical_id"])
        if existing is None:
            by_canonical[candidate["canonical_id"]] = candidate
            unique_candidates.append(candidate)
            continue
        existing["aliases"] = list(dict.fromkeys([*existing["aliases"], *candidate["aliases"]]))
        existing["evidence_fragments"].extend(candidate["evidence_fragments"])
    candidates = unique_candidates
    discovery = {
        "triggered": bool(candidates),
        "enumeration_evidence": bool(candidates),
        "candidate_entities": [_public_discovery_entity(entity) for entity in candidates],
        "source_pages": [path for path, _page, _entities in valid_pages],
    }
    return discovery, candidates if len(candidates) >= 2 else []

def _public_discovery_entity(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entity.items()
        if not key.startswith("_")
    }


def _constrain_discovery_entities(
    question: str,
    discovery: dict[str, Any],
    entities: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply query-level entity constraints before discovery can batch.

    A wildcard is a namespace request, not permission to batch every generic
    label found on a candidate page.  Keep only qualified evidence whose
    prefix matches the requested wildcard and drop source pages that no
    longer have an eligible entity after that constraint.
    """

    wildcards = extract_namespace_wildcards(question)
    if not wildcards:
        return discovery, list(entities)
    prefixes = [item.prefix_segments for item in wildcards]
    constrained: list[dict[str, Any]] = []
    for entity in entities:
        identifier = entity.get("_identifier")
        if not isinstance(identifier, QualifiedIdentifier):
            continue
        name = "/".join(identifier.name_segments).casefold()
        if name in {"a", "na", "none", "null"}:
            continue
        if any(identifier.prefix_segments[: len(prefix)] == prefix for prefix in prefixes):
            constrained.append(entity)
    source_paths = {str(entity["evidence"]["path"]) for entity in constrained}
    constrained_discovery = {
        **discovery,
        "triggered": len(constrained) >= 2,
        "enumeration_evidence": len(constrained) >= 2,
        "candidate_entities": [_public_discovery_entity(entity) for entity in constrained],
        "source_pages": [
            path for path in discovery.get("source_pages", ()) if path in source_paths
        ],
    }
    return constrained_discovery, constrained if len(constrained) >= 2 else []


def _heading(hit: PassageHit) -> str:
    return " / ".join(hit.heading_path) if hit.heading_path else hit.title


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
    snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
) -> list[PassageHit]:
    """Add bounded title/provenance signals from existing DB projections."""
    terms = {term.casefold() for term in re.findall(r"[\w一-鿿]+", question) if len(term) > 1}
    if not terms:
        return []
    candidates: list[tuple[int, str]] = []
    pages = snapshot.pages if snapshot is not None else store.page_candidates()
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="snapshot")
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
        probe = probe_hit(
            path,
            str(page["title"]),
            corpus=str(page.get("corpus") or "active"),
            authority=str(page.get("authority") or ""),
            source_kind=str(page.get("source_kind") or ""),
        )
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
        if not _eligible(hit, metadata, scope=scope) or not _matches_request(hit, metadata, project=project, filters=filters):
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
        if not _eligible(hit, raw_metadata, scope=scope):
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


def _effective_rrf_k(embedding: EmbeddingSettings | None) -> int:
    """Resolve one query invocation's immutable RRF scale."""

    value = getattr(embedding, "rrf_k", RRF_K)
    return value if isinstance(value, int) and value > 0 else RRF_K


def _vector_hits(
    root: Path,
    question: str,
    embedding: EmbeddingSettings | None,
    *,
    scope: str,
    allowed_paths: set[str] | None = None,
    cancellation: QueryCancellationContext | None = None,
    provider_factory: Any | None = None,
) -> tuple[dict[str, tuple[int, float]], list[str]]:
    if cancellation is not None:
        cancellation.checkpoint("vector")
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
        provider_type = provider_factory or LocalBgeM3Provider
        provider = provider_type(
            settings.model_path,
            device=settings.device,
            batch_size=settings.batch_size,
            max_sequence_length=settings.max_sequence_length,
        )
        store.validate_provider(provider.identity(), include_raw_sources=False)
        results = store.search(provider.embed_query(question, context=cancellation), allowed_paths=allowed_paths, limit=settings.candidate_limit)
        if cancellation is not None:
            cancellation.checkpoint("vector")
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
    snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
) -> tuple[dict[str, QueryCandidate], list[PassageHit]]:
    """Reuse the legacy bounded expander over DB projections, never files.

    The expander owns the existing two-hop, fan-out and score-cap behaviour;
    this adapter only supplies its already-filtered candidate boundary.
    """
    if scope in {"archive", "raw"} or not seed_scores:
        return {}, []
    candidates: list[QueryCandidate] = []
    pages = snapshot.pages if snapshot is not None else store.page_candidates()
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="graph")
        path = str(page["path"])
        frontmatter = metadata.get(path, {})
        probe = probe_hit(
            path,
            str(page["title"]),
            corpus=str(page.get("corpus") or "active"),
            authority=str(page.get("authority") or ""),
            source_kind=str(page.get("source_kind") or ""),
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
    apply_graph_expansion(scored, candidates, build_graph(root, candidates), max_graph_hops=2, collect_reasons=debug)
    added = [path for path in scored if path not in seed_scores]
    return scored, store.passages_for_pages(added, limit_per_page=1)


def _store_metadata(
    store: RetrievalIndexStore,
    *,
    snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    pages = snapshot.pages if snapshot is not None else store.page_candidates()
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="metadata")
        frontmatter = page.get("frontmatter")
        metadata[str(page["path"])] = dict(frontmatter) if isinstance(frontmatter, Mapping) else {}
    return metadata


def _entity_query_text(entity: dict[str, Any], base_question: str, all_entities: Sequence[dict[str, Any]]) -> str:
    identifier = entity.get("_identifier")
    canonical = identifier.canonical_id if isinstance(identifier, QualifiedIdentifier) else str(entity["canonical_id"])
    excluded: set[str] = set()
    for item in all_entities:
        excluded.update(tokens(" ".join(str(alias) for alias in item.get("aliases", []))))
    remaining = [term for term in tokens(base_question) if term not in excluded]
    return " ".join([canonical, *remaining])


def _alias_pattern(alias: str) -> str:
    pieces = [piece for piece in re.split(r"[/\s_-]+", alias.casefold()) if piece]
    if not pieces:
        return ""
    return r"(?<![a-z0-9_])" + r"[/\s_-]+".join(re.escape(piece) for piece in pieces) + r"(?![a-z0-9_])"


def _entity_match_signals(entity: dict[str, Any], hit: PassageHit) -> tuple[float, bool, dict[str, bool]]:
    aliases = [str(alias) for alias in entity.get("aliases", ())]
    identifier = entity.get("_identifier")
    if isinstance(identifier, QualifiedIdentifier):
        aliases = [*aliases, identifier.canonical_id, "".join((*identifier.prefix_segments, *identifier.name_segments))]
    title = hit.title.casefold()
    heading = " ".join(hit.heading_path).casefold()
    path = hit.page_path.casefold()
    body = hit.text.casefold()
    fields = {"title": title, "heading": heading, "path": path, "body": body}
    title_match = any(alias.casefold() in title or re.search(_alias_pattern(alias), title) for alias in aliases if alias)
    heading_match = any(alias.casefold() in heading or re.search(_alias_pattern(alias), heading) for alias in aliases if alias)
    path_match = any(alias.casefold() in path or re.search(_alias_pattern(alias), path) for alias in aliases if alias)
    body_match = any(alias.casefold() in body or re.search(_alias_pattern(alias), body) for alias in aliases if alias)
    exact_match = title_match or heading_match or path_match or body_match
    score = max(float(hit.score), 0.0)
    score += 3.0 if title_match else 0.0
    score += 2.0 if heading_match else 0.0
    score += 1.5 if path_match else 0.0
    score += 1.0 if body_match else 0.0
    del fields
    return round(score, 12), exact_match, {"title": title_match, "heading": heading_match, "path": path_match, "body": body_match}


def _batch_candidate_items(entity: dict[str, Any], hits: Sequence[PassageHit]) -> list[dict[str, Any]]:
    by_page: dict[str, dict[str, Any]] = {}
    for hit in hits:
        score, exact_match, signals = _entity_match_signals(entity, hit)
        item = {
            "hit": hit,
            "score": score,
            "raw_score": round(float(hit.score), 12),
            "exact_match": exact_match,
            "signals": signals,
        }
        current = by_page.get(hit.page_path)
        if current is None or (item["score"], hit.passage_id) > (current["score"], current["hit"].passage_id):
            by_page[hit.page_path] = item
    return sorted(by_page.values(), key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))


def _adaptive_select_candidates(
    items: Sequence[dict[str, Any]],
    policy: AdaptiveCandidateScorePolicy,
) -> tuple[list[dict[str, Any]], str]:
    """Keep a score platform and stop at the first meaningful drop."""

    ranked = list(items)
    if not ranked:
        return [], "unresolved:no_candidates"
    best_score = float(ranked[0]["score"])
    if best_score < policy.minimum_relevance_score:
        return [], "unresolved:below_minimum_relevance"
    threshold = max(policy.minimum_relevance_score, best_score * policy.high_score_ratio)
    selected: list[dict[str, Any]] = []
    for index, item in enumerate(ranked):
        score = float(item["score"])
        if index == 0:
            item["selection_reason"] = "best_local_candidate"
        elif score >= threshold and len(selected) < policy.max_selected_candidates:
            item["selection_reason"] = "same_high_score_platform"
        else:
            break
        item["local_rank"] = index + 1
        item["normalized_score"] = round(score / best_score if best_score else 0.0, 12)
        selected.append(item)
    return selected, ""


def _entity_store_specs(
    root: Path,
    primary_store: RetrievalIndexStore,
    effective_scope: str,
    *,
    snapshot: QueryCorpusSnapshot,
    raw_snapshot: QueryCorpusSnapshot | None,
    raw_store: RetrievalIndexStore | None,
    raw_availability: RawAvailability = "missing",
) -> list[_EntityStoreSpec]:
    del root
    specs = [_EntityStoreSpec(primary_store, effective_scope, snapshot)]
    if effective_scope in {"knowledge", "all"} and raw_availability == "fresh":
        entity_raw_store = raw_store
        if entity_raw_store is None:
            return specs
        if raw_snapshot is None:
            raise RuntimeError("raw entity store requires the invocation snapshot")
        specs.append(_EntityStoreSpec(entity_raw_store, "raw", raw_snapshot))
    return specs


def _search_entity_store(
    store: RetrievalIndexStore,
    entity: dict[str, Any],
    query_text: str,
    *,
    project: str | None,
    filters: QueryFilters,
    limit: int,
) -> tuple[list[PassageHit], dict[str, int]]:
    counters = {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0}
    identifier = entity.get("_identifier")
    common_kwargs = {
        "project": project,
        "page_type": filters.type,
        "tags": list(filters.tags),
    }
    if isinstance(identifier, QualifiedIdentifier):
        def search_qualified(
            current_store: RetrievalIndexStore,
            _step: LadderStep,
            current_limit: int,
        ) -> list[PassageHit]:
            return current_store.search_qualified_identifier(
                identifier,
                limit=current_limit,
                project=project,
                page_type=filters.type,
                tags=list(filters.tags),
            )

        first_step = LadderStep(
            "qualified_hits",
            "qualified_code",
            identifier.canonical_id,
            common_kwargs,
            searcher=search_qualified,
        )
    else:
        first_step = LadderStep(
            "fts_hits",
            "strict",
            str(entity["canonical_id"]),
            common_kwargs,
        )
    steps = [
        first_step,
        LadderStep("fts_hits", "strict", query_text, common_kwargs, run_if_empty=True),
        LadderStep("relaxed_hits", "relaxed", query_text, common_kwargs, run_if_empty=True),
    ]
    hits, _mode = search_ladder(
        store,
        steps=steps,
        merge="descend_if_empty",
        limit=limit,
        swallow_index_errors=False,
        counts=counters,
    )
    return hits, counters


def _run_one_entity_batch_query(
    root: Path,
    entity: dict[str, Any],
    all_entities: Sequence[dict[str, Any]],
    base_question: str,
    *,
    store_specs: Sequence[_EntityStoreSpec],
    project: str | None,
    filters: QueryFilters,
    policy: AdaptiveCandidateScorePolicy,
) -> tuple[dict[str, Any], dict[str, int]]:
    del root
    query_text = _entity_query_text(entity, base_question, all_entities)
    counters = {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0}
    try:
        for spec in store_specs:
            store = spec.store
            store_scope = spec.scope
            metadata = spec.snapshot.metadata
            hits, local_counters = _search_entity_store(
                store,
                entity,
                query_text,
                project=project,
                filters=filters,
                limit=policy.candidate_pool_limit,
            )
            for key, value in local_counters.items():
                counters[key] += value
            if store_scope == "raw":
                counters["raw_hits"] += len(hits)
            eligible_hits = [
                hit
                for hit in hits
                if _eligible(hit, metadata, scope=store_scope)
                and _matches_request(hit, metadata, project=project, filters=filters)
            ]
            if not eligible_hits:
                continue
            candidates = _batch_candidate_items(entity, eligible_hits)
            selected, selection_error = _adaptive_select_candidates(candidates, policy)
            if selection_error:
                return (
                    {
                        "entity": entity["canonical_id"],
                        "aliases": list(entity.get("aliases", ())),
                        "status": "unresolved",
                        "primary": None,
                        "alternatives": [],
                        "needs_review": False,
                        "shared_source": False,
                        "selection_confidence": 0.0,
                        "evidence": entity["evidence"],
                        "reason": selection_error,
                    },
                    counters,
                )
            source_conflict = len({str(item["hit"].source_kind) for item in selected}) > 1
            needs_review = len(selected) > 1 and (
                float(selected[1]["score"]) >= float(selected[0]["score"]) * policy.ambiguity_ratio or source_conflict
            )
            confidence = 1.0 if len(selected) == 1 else float(selected[0]["score"]) / max(float(selected[0]["score"]) + float(selected[1]["score"]), 1e-9)
            result = {
                "entity": entity["canonical_id"],
                "aliases": list(entity.get("aliases", ())),
                "status": "ambiguous" if needs_review else "ok",
                "primary": _batch_public_candidate(selected[0], include_context=True),
                "alternatives": [_batch_public_candidate(item, include_context=False) for item in selected[1:]],
                "needs_review": needs_review,
                "shared_source": False,
                "selection_confidence": round(confidence if not needs_review else min(confidence, 0.55), 4),
                "evidence": entity["evidence"],
            }
            return result, counters
        return (
            {
                "entity": entity["canonical_id"],
                "aliases": list(entity.get("aliases", ())),
                "status": "unresolved",
                "primary": None,
                "alternatives": [],
                "needs_review": False,
                "shared_source": False,
                "selection_confidence": 0.0,
                "evidence": entity["evidence"],
                "reason": "unresolved:no_eligible_candidates",
            },
            counters,
        )
    except RetrievalIndexError as exc:
        return (
            {
                "entity": entity["canonical_id"],
                "aliases": list(entity.get("aliases", ())),
                "status": "error",
                "primary": None,
                "alternatives": [],
                "needs_review": False,
                "shared_source": False,
                "selection_confidence": 0.0,
                "evidence": entity["evidence"],
                "error": {"code": exc.code, "message": str(exc)},
            },
            counters,
        )
    except Exception as exc:  # noqa: BLE001 - isolate one entity failure from the batch
        return (
            {
                "entity": entity["canonical_id"],
                "aliases": list(entity.get("aliases", ())),
                "status": "error",
                "primary": None,
                "alternatives": [],
                "needs_review": False,
                "shared_source": False,
                "selection_confidence": 0.0,
                "evidence": entity["evidence"],
                "error": {"code": "batch_query_failed", "message": str(exc)},
            },
            counters,
        )


def _batch_public_candidate(item: dict[str, Any], *, include_context: bool) -> dict[str, Any]:
    hit = item["hit"]
    evidence = {
        "path": hit.page_path,
        "passage_id": hit.passage_id,
        "heading": _heading(hit),
        "excerpt": hit.text[:360],
    }
    candidate = {
        "path": hit.page_path,
        "heading": _heading(hit),
        "passage_id": hit.passage_id,
        "snippet": hit.text[:240],
        "score": item["score"],
        "normalized_score": item.get("normalized_score", 0.0),
        "local_rank": item.get("local_rank"),
        "selection_reason": item.get("selection_reason", ""),
        "source_kind": hit.source_kind,
        "evidence": evidence,
    }
    if include_context:
        candidate["context"] = hit.text
    return candidate


def _batch_token(fingerprint: str, offset: int) -> str:
    return f"qualified-batch:v1:{fingerprint}:{offset}"


def _batch_fingerprint(
    question: str,
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    retrieval_mode: str,
    hard_budget_tokens: int,
    entities: Sequence[dict[str, Any]],
) -> str:
    payload = {
        "question": question,
        "scope": scope,
        "project": project or "",
        "filters": {"type": filters.type or "", "tags": list(filters.tags)},
        "retrieval_mode": retrieval_mode,
        "hard_budget_tokens": hard_budget_tokens,
        "entities": [str(entity["canonical_id"]) for entity in entities],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_batch_token(token: str | None, fingerprint: str) -> int | None:
    if not token:
        return None
    prefix = f"qualified-batch:v1:{fingerprint}:"
    if not token.startswith(prefix):
        return None
    try:
        offset = int(token.removeprefix(prefix))
    except ValueError:
        return None
    return offset if offset >= 0 else None


def _run_entity_batch(
    root: Path,
    entities: Sequence[dict[str, Any]],
    base_question: str,
    *,
    primary_store: RetrievalIndexStore,
    effective_scope: str,
    project: str | None,
    filters: QueryFilters,
    retrieval_mode: str,
    hard_budget_tokens: int,
    confirmation_token: str | None,
    snapshot: QueryCorpusSnapshot,
    raw_snapshot: QueryCorpusSnapshot | None,
    raw_store: RetrievalIndexStore | None,
    raw_availability: RawAvailability = "missing",
    cancellation: QueryCancellationContext | None = None,
) -> dict[str, Any]:
    if cancellation is not None:
        cancellation.checkpoint("fallback")
    policy = AdaptiveCandidateScorePolicy()
    fingerprint = _batch_fingerprint(
        base_question,
        scope=effective_scope,
        project=project,
        filters=filters,
        retrieval_mode=retrieval_mode,
        hard_budget_tokens=hard_budget_tokens,
        entities=entities,
    )
    offset = _parse_batch_token(confirmation_token, fingerprint)
    if confirmation_token and offset is None:
        return {
            "status": "confirmation_required",
            "entity_count": len(entities),
            "max_batch_items": BATCH_MAX_ITEMS,
            "pending_entities": [entity["canonical_id"] for entity in entities],
            "confirmation_token": _batch_token(fingerprint, 0),
            "entities": [],
            "failed_entities": [],
            "unresolved": [],
            "ambiguous": [],
            "error": {"code": "invalid_confirmation_token", "message": "confirmation token does not match the discovered entity list"},
            "counters": {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0, "queries": 0},
        }
    if len(entities) > BATCH_MAX_ITEMS and offset is None:
        token = _batch_token(fingerprint, 0)
        return {
            "status": "confirmation_required",
            "entity_count": len(entities),
            "max_batch_items": BATCH_MAX_ITEMS,
            "pending_entities": [entity["canonical_id"] for entity in entities],
            "confirmation_token": token,
            "entities": [],
            "failed_entities": [],
            "unresolved": [],
            "ambiguous": [],
            "counters": {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0, "queries": 0},
        }
    if offset is None:
        offset = 0
    if offset >= len(entities) or offset % BATCH_MAX_ITEMS != 0:
        return {
            "status": "confirmation_required",
            "entity_count": len(entities),
            "max_batch_items": BATCH_MAX_ITEMS,
            "pending_entities": [entity["canonical_id"] for entity in entities[offset:] if offset < len(entities)],
            "confirmation_token": _batch_token(fingerprint, 0),
            "entities": [],
            "failed_entities": [],
            "unresolved": [],
            "ambiguous": [],
            "error": {"code": "invalid_confirmation_token", "message": "confirmation token does not match the discovered entity list"},
            "counters": {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0, "queries": 0},
        }
    current_entities = list(entities[offset : offset + BATCH_MAX_ITEMS])
    specs = _entity_store_specs(
        root,
        primary_store,
        effective_scope,
        snapshot=snapshot,
        raw_snapshot=raw_snapshot,
        raw_store=raw_store,
        raw_availability=raw_availability,
    )
    results: list[dict[str, Any]] = []
    counter_totals = {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0, "queries": len(current_entities)}
    for index, entity in enumerate(current_entities):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=1, stage="fallback")
        result, counters = _run_one_entity_batch_query(
            root,
            entity,
            entities,
            base_question,
            store_specs=specs,
            project=project,
            filters=filters,
            policy=policy,
        )
        results.append(result)
        for key, value in counters.items():
            counter_totals[key] += value
    path_counts: dict[str, int] = {}
    for result in results:
        for candidate in [result.get("primary"), *result.get("alternatives", [])]:
            if isinstance(candidate, dict) and candidate.get("path"):
                path_counts[str(candidate["path"])] = path_counts.get(str(candidate["path"]), 0) + 1
    for result in results:
        paths = [result.get("primary"), *result.get("alternatives", [])]
        result["shared_source"] = any(
            isinstance(candidate, dict) and path_counts.get(str(candidate.get("path")), 0) > 1
            for candidate in paths
        )
        for candidate in paths:
            if isinstance(candidate, dict):
                candidate["shared_source"] = path_counts.get(str(candidate.get("path")), 0) > 1
    pending = list(entities[offset + len(current_entities) :])
    continuation_token = _batch_token(fingerprint, offset + len(current_entities)) if pending else None
    statuses = [str(result.get("status")) for result in results]
    if pending or any(status in {"error", "unresolved", "ambiguous"} for status in statuses):
        if any(status in {"ok", "ambiguous"} for status in statuses):
            status = "partial_success"
        elif any(status == "error" for status in statuses):
            status = "error"
        else:
            status = "unresolved"
    else:
        status = "success"
    return {
        "status": status,
        "entity_count": len(entities),
        "max_batch_items": BATCH_MAX_ITEMS,
        "batch_index": offset // BATCH_MAX_ITEMS,
        "pending_entities": [entity["canonical_id"] for entity in pending],
        "confirmation_token": confirmation_token if len(entities) > BATCH_MAX_ITEMS else None,
        "continuation_token": continuation_token,
        "entities": results,
        "failed_entities": [result["entity"] for result in results if result.get("status") == "error"],
        "unresolved": [result["entity"] for result in results if result.get("status") == "unresolved"],
        "ambiguous": [result["entity"] for result in results if result.get("status") == "ambiguous"],
        "counters": counter_totals,
        "candidate_pool_limit": policy.candidate_pool_limit,
        "score_policy": {
            "minimum_relevance_score": policy.minimum_relevance_score,
            "high_score_ratio": policy.high_score_ratio,
            "ambiguity_ratio": policy.ambiguity_ratio,
        },
    }


def _stage_one_fts_hits(
    store: RetrievalIndexStore,
    question: str,
    *,
    effective_scope: str,
    project: str | None,
    filters: QueryFilters,
) -> tuple[list[PassageHit], str, int]:
    """Run stage-one lexical recall, including raw qualified aliases directly."""

    common_kwargs = {
        "project": project,
        "page_type": filters.type,
        "tags": list(filters.tags),
    }
    steps: list[LadderStep] = [LadderStep("strict", "strict", question, common_kwargs)]
    if effective_scope == "raw" and has_qualified_identifier(question):
        steps.append(LadderStep("qualified_code", "qualified_code", question, common_kwargs))
    counts: dict[str, int] = {}
    hits, mode = search_ladder(
        store,
        steps=steps,
        merge="merge_by_passage",
        limit=50,
        swallow_index_errors=False,
        counts=counts,
    )
    return hits, mode, counts.get("qualified_code", 0)


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


def _thaw_value(value: Any) -> Any:
    """Copy an immutable outcome projection back into public JSON containers."""

    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw_value(item) for item in value}
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
            selected = _adaptive_expand(merged, top_k)[:top_k]
            candidates = [*candidate_pool, *raw_candidate_items]
            coverage_fallback = any(item["hit"].source_kind == "raw" for item in selected)
        else:
            selected = select_best_per_page(raw_candidate_items)
            selected = _adaptive_expand(selected, top_k)[:top_k]
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
                self.selected = _adaptive_expand(select_best_per_page(wiki_relaxed_items), top_k)
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
        """Discover structured entities and run the optional batch query."""

        self._ensure_open()
        discovery_requested = _discovery_requested(question)
        discovery_source_items: list[dict[str, Any]] = []
        discovery, discovery_entities = _discover_enumerated_entities(self.selected, self.context_items)
        discovery, discovery_entities = _constrain_discovery_entities(
            question, discovery, discovery_entities
        )
        if not discovery_entities and discovery_requested:
            # Stage one may be dominated by a generic product passage (for
            # example ``NetSuite``) or may have no FTS term at all for ``N/*``.
            # Locate a bounded catalog candidate from the active projection, then
            # validate same-page structure before exposing any entity.
            discovery_source_items = _discovery_source_items(
                self.store,
                metadata,
                question,
                scope=effective_scope,
                project=project,
                filters=filters,
                snapshot=snapshot,
                cancellation=self.cancellation,
            )
            discovery, discovery_entities = _discover_enumerated_entities(
                self.selected,
                [*self.context_items, *discovery_source_items],
            )
            discovery, discovery_entities = _constrain_discovery_entities(
                question, discovery, discovery_entities
            )
        if not discovery_entities and discovery_requested and effective_scope in {"knowledge", "all"}:
            # Raw reference projections are a second discovery source, subject to
            # the same active→raw boundary and request filters as entity queries.
            if self.raw_availability() == "fresh":
                candidate_raw_store = self.get_raw_store()
                raw_snapshot = self.capture_raw_snapshot()
                raw_metadata = _store_metadata(
                    candidate_raw_store,
                    snapshot=raw_snapshot,
                    cancellation=self.cancellation,
                )
                raw_discovery_items = _discovery_source_items(
                    candidate_raw_store,
                    raw_metadata,
                    question,
                    scope="raw",
                    project=project,
                    filters=filters,
                    snapshot=raw_snapshot,
                    cancellation=self.cancellation,
                )
                discovery, discovery_entities = _discover_enumerated_entities(
                    self.selected,
                    [*self.context_items, *discovery_source_items, *raw_discovery_items],
                )
                discovery, discovery_entities = _constrain_discovery_entities(
                    question, discovery, discovery_entities
                )
        discovery["requested"] = discovery_requested
        if not discovery_entities:
            discovery["reason"] = "structured_enumeration_evidence_insufficient"

        batch_payload: dict[str, Any] = {
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
            batch_payload = _run_entity_batch(
                self.root,
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
        self.discovery = discovery
        self.discovery_entities = discovery_entities
        self.discovery_source_items = discovery_source_items
        self.batch_payload = batch_payload
        self.discovery_requested = discovery_requested

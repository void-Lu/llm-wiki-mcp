"""Query V2: typed, page-first retrieval behind the canonical MCP response."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from retrieval.context_packer import ContextPassage, pack_context
from codegraph.codegraph_policy import is_codegraph_raw_path
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
from retrieval.query_telemetry import QueryTelemetry
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_recovery import RecoveryCondition, assemble_recovery
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from retrieval.metadata_filters import QUERY_METADATA_FILTERS, normalize_metadata_filters, page_matches_filters
from runtime.runtime_config import EmbeddingSettings, TelemetrySettings
from retrieval.vector_index import VectorIndexError, VectorIndexStore, vector_settings_from_embedding
from retrieval.vector_provider import LocalBgeM3Provider, VectorProviderError
from retrieval.graph_retrieval import QueryCandidate, apply_graph_expansion, build_graph


DEFAULT_TOP_K = 10
RANKING_POLICY_VERSION = "query-v2-passage-rrf-10"
RRF_K = 60
RAW_FALLBACK_LIMIT = 20
RAW_FALLBACK_CANDIDATE_LIMIT = 160
IDENTIFIER_PHRASE_BONUS = 20.0
IDENTIFIER_PHRASE_CANDIDATES = 200
PAGE_FILL_LIMIT = 500
FRESHNESS_BONUS_MAX = 12.0
FRESHNESS_DECAY_DAYS = 90
STEP_BONUS_MAX = 4.0
STEP_COUNT_FULL = 5
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
        probe = PassageHit(
            "",
            path,
            title,
            (),
            "",
            0.0,
            str(page.get("corpus") or "active"),
            str(page.get("authority") or ""),
            str(page.get("source_kind") or ""),
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
        for hit in store.passages_for_pages(probe_paths, limit_per_page=20):
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

    passages = store.passages_for_pages(page_paths, limit_per_page=PAGE_FILL_LIMIT)
    return [
        {
            "hit": hit,
            "fts_rank": None,
            "title_rank": None,
            "vector_rank": None,
            "vector_score": 0.0,
            "rrf": 0.0,
            "exact": False,
            "graph_score": 0.0,
            "graph_reasons": [],
            "score": hit.score,
        }
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
    snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
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
    if snapshot is None:
        snapshot = QueryCorpusSnapshot.capture(
            raw_store,
            cancellation=cancellation or QueryCancellationContext.unbounded(),
        )
    raw_metadata: dict[str, dict[str, Any]] = {}
    pages = snapshot.pages if snapshot is not None else raw_store.page_candidates()
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="snapshot")
        frontmatter = page.get("frontmatter")
        raw_metadata[str(page["path"])] = dict(frontmatter) if isinstance(frontmatter, Mapping) else {}

    raw_items: list[dict[str, Any]] = []
    try:
        raw_hits = raw_store.search_fts(
            question,
            limit=candidate_limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
        )
        qualified_code_hits = (
            raw_store.search_fts(
                question,
                limit=candidate_limit,
                project=project,
                page_type=filters.type,
                tags=list(filters.tags),
                mode="qualified_code",
            )
            if has_qualified_identifier(question)
            else []
        )
        identifier_phrase_hits: list[PassageHit] = []
        if not raw_hits and not qualified_code_hits:
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
        if qualified_code_hits:
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
    *,
    rrf_k: int = RRF_K,
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
        source_rrf = (rrf_k + 1) / (rrf_k + source_rank)
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
        provider = LocalBgeM3Provider(settings.model_path, device=settings.device, batch_size=settings.batch_size, max_sequence_length=settings.max_sequence_length)
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
) -> list[_EntityStoreSpec]:
    specs = [_EntityStoreSpec(primary_store, effective_scope, snapshot)]
    if effective_scope in {"knowledge", "all"}:
        entity_raw_store = raw_store or RetrievalIndexStore(root, scope="raw")
        raw_status = entity_raw_store.status()
        if raw_status.get("ok") and raw_status.get("state") == "fresh":
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
    if isinstance(identifier, QualifiedIdentifier):
        hits = store.search_qualified_identifier(
            identifier,
            limit=limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
        )
        counters["qualified_hits"] += len(hits)
    else:
        hits = store.search_fts(
            str(entity["canonical_id"]),
            limit=limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
        )
        counters["fts_hits"] += len(hits)
    if not hits:
        hits = store.search_fts(
            query_text,
            limit=limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
        )
        counters["fts_hits"] += len(hits)
    if not hits:
        hits = store.search_fts(
            query_text,
            limit=limit,
            project=project,
            page_type=filters.type,
            tags=list(filters.tags),
            mode="relaxed",
        )
        counters["relaxed_hits"] += len(hits)
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

    strict_hits = store.search_fts(
        question,
        limit=50,
        project=project,
        page_type=filters.type,
        tags=list(filters.tags),
    )
    if effective_scope != "raw" or not has_qualified_identifier(question):
        return strict_hits, "strict", 0
    qualified_hits = store.search_fts(
        question,
        limit=50,
        project=project,
        page_type=filters.type,
        tags=list(filters.tags),
        mode="qualified_code",
    )
    by_passage = {hit.passage_id: hit for hit in strict_hits}
    for hit in qualified_hits:
        current = by_passage.get(hit.passage_id)
        if current is None or hit.score > current.score:
            by_passage[hit.passage_id] = hit
    hits = sorted(by_passage.values(), key=lambda hit: (-hit.score, hit.page_path, hit.passage_id))
    return hits, "qualified_code" if qualified_hits else "strict", len(qualified_hits)


def run_query_v2(
    vault_root: str | Path,
    question: str,
    *,
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"] = "auto",
    project: str | None = None,
    filters: QueryFilters | None = None,
    top_k: int = DEFAULT_TOP_K,
    hard_budget_tokens: int = 16_000,
    embedding: EmbeddingSettings | None = None,
    telemetry: TelemetrySettings | None = None,
    debug: bool = False,
    include_context_pack: bool = True,
    lexical_enabled: bool = True,
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    expansion_terms: dict[str, list[str]] | None = None,
    confirmation_token: str | None = None,
    cancellation: QueryCancellationContext | None = None,
    telemetry_recorder: QueryTelemetry | None = None,
) -> dict[str, Any]:
    """Read existing projections and return one canonical result payload.

    The internal packer still assembles page-ordered evidence, but its body is
    emitted once on each public ``results`` item; the old ``context_pack`` and
    legacy adapter are intentionally not part of the response contract.
    """
    started = time.perf_counter()
    cancellation = cancellation or QueryCancellationContext.unbounded()
    cancellation.checkpoint("status")
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
    effective_rrf_k = _effective_rrf_k(embedding) if retrieval_mode != "lexical" else RRF_K
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
            "additional_results": [],
            "budget": {"total": k_budget, "used": 0},
            "pipeline": {"ranking_version": RANKING_POLICY_VERSION, "warnings": [*index_warnings, str(status.get("code"))], "fallback": {"level": "none", "reasons": ["index_unavailable"], "allowed_source_paths": []}},
        }

    snapshot = QueryCorpusSnapshot.capture(store, cancellation=cancellation)
    metadata = {path: dict(frontmatter) for path, frontmatter in snapshot.metadata.items()}
    provenance = {path: dict(values) for path, values in snapshot.provenance.items()}
    stage_lexical_mode = "strict"
    qualified_fts_hits = 0
    cancellation.checkpoint("fts")
    try:
        fts, stage_lexical_mode, qualified_fts_hits = (
            _stage_one_fts_hits(
                store,
                question,
                effective_scope=effective_scope,
                project=project,
                filters=filters,
            )
            if retrieval_mode != "vector"
            else ([], "strict", 0)
        )
    except RetrievalIndexError as exc:
        fts = []
        status = {**status, "code": exc.code}
    allowed_vector_paths: set[str] = set()
    for index, item in enumerate(snapshot.pages):
        cancellation.checkpoint_batch(index, every=16, stage="snapshot")
        frontmatter = item.get("frontmatter")
        if not isinstance(frontmatter, Mapping):
            frontmatter = {}
        if not page_matches_filters(
            frontmatter,
            str(item.get("source_kind") or ""),
            project=project,
            page_type=filters.type,
            tags=filters.tags,
            path_prefix=filters.path_prefix,
            page_path=str(item.get("path") or ""),
        ):
            continue
        if not _eligible(
            PassageHit("", str(item["path"]), str(item["title"]), (), "", 0.0, str(item.get("corpus") or "active"), str(item.get("authority") or ""), str(item.get("source_kind") or "")),
            metadata,
            scope=effective_scope,
        ):
            continue
        allowed_vector_paths.add(str(item["path"]))
    cancellation.checkpoint("vector")
    vector, vector_warnings = (
        _vector_hits(root, question, embedding, scope=effective_scope, allowed_paths=allowed_vector_paths, cancellation=cancellation)
        if retrieval_mode != "lexical" and effective_scope != "raw"
        else ({}, [])
    )
    ranked: dict[str, dict[str, Any]] = {}
    for rank, hit in enumerate(fts, 1):
        cancellation.checkpoint_batch(rank - 1, every=16, stage="fts")
        if not _eligible(hit, metadata, scope=effective_scope) or not _matches_request(hit, metadata, project=project, filters=filters):
            continue
        item = ranked.setdefault(hit.passage_id, {"hit": hit, "fts_rank": rank, "title_rank": None, "vector_rank": None, "vector_score": 0.0})
        item["fts_rank"] = rank
    # A vector hit is identified by passage ID.  Load its existing retrieval
    # projection rather than scanning Markdown, so vector-only recall remains
    # available without adding query-time corpus reads.
    for index, hit in enumerate(store.load_passages(vector)):
        cancellation.checkpoint_batch(index, every=16, stage="vector")
        if not _eligible(hit, metadata, scope=effective_scope) or not _matches_request(hit, metadata, project=project, filters=filters):
            continue
        ranked.setdefault(hit.passage_id, {"hit": hit, "fts_rank": None, "title_rank": None, "vector_rank": None, "vector_score": 0.0})
    # A title-only match is deliberately not primary retrieval.  A generic
    # title overlap (such as "script") must not prevent a natural-language
    # question, in any language, from using relaxed lexical recovery.
    has_primary_recall = bool(ranked)
    expansion_suggestions: list[str] = []
    for rank, hit in enumerate(_title_candidates(store, metadata, question, scope=effective_scope, project=project, filters=filters, snapshot=snapshot, cancellation=cancellation), 1):
        cancellation.checkpoint_batch(rank - 1, every=16, stage="vector")
        ranked.setdefault(hit.passage_id, {"hit": hit, "fts_rank": None, "title_rank": rank, "vector_rank": None, "vector_score": 0.0})
        ranked[hit.passage_id]["title_rank"] = rank
    for index, item in enumerate(ranked.values()):
        cancellation.checkpoint_batch(index, every=16, stage="vector")
        vector_data = vector.get(item["hit"].passage_id)
        if vector_data:
            item["vector_rank"], item["vector_score"] = vector_data
    scored: list[dict[str, Any]] = []
    for index, item in enumerate(ranked.values()):
        cancellation.checkpoint_batch(index, every=16, stage="graph")
        hit = item["hit"]
        rrf = (
            (1 / (effective_rrf_k + item["fts_rank"]) if item["fts_rank"] else 0.0)
            + (1 / (effective_rrf_k + item["title_rank"]) if item["title_rank"] else 0.0)
            + (1 / (effective_rrf_k + item["vector_rank"]) if item["vector_rank"] else 0.0)
        )
        exact = int(question.casefold() in {hit.title.casefold(), hit.page_path.casefold()})
        total = round(
            rrf * (effective_rrf_k + 1)
            + _authority_bonus(hit, intent, effective_scope, metadata)
            + _title_overlap_bonus(hit, question)
            + exact * 0.5,
            12,
        )
        scored.append({**item, "score": total, "rrf": rrf, "exact": bool(exact), "graph_score": 0.0, "graph_reasons": []})
    cancellation.checkpoint("graph")
    graph_candidates, graph_passages = (
        _graph_expand(
            root, store, metadata, scope=effective_scope, project=project, filters=filters,
            seed_scores={item["hit"].page_path: item["score"] for item in scored}, debug=debug, snapshot=snapshot, cancellation=cancellation,
        )
        if effective_scope != "raw"
        else ({}, [])
    )
    existing_by_path = {item["hit"].page_path: item for item in scored}
    for index, (path, candidate) in enumerate(graph_candidates.items()):
        cancellation.checkpoint_batch(index, every=16, stage="graph")
        if path in existing_by_path:
            existing_by_path[path]["graph_score"] = candidate.graph_score
            existing_by_path[path]["graph_reasons"] = list(candidate.rank_breakdown.graph_reasons)
            existing_by_path[path]["score"] = round(existing_by_path[path]["score"] + candidate.graph_score, 12)
    for index, hit in enumerate(graph_passages):
        cancellation.checkpoint_batch(index, every=16, stage="graph")
        candidate = graph_candidates[hit.page_path]
        scored.append({"hit": hit, "fts_rank": None, "title_rank": None, "vector_rank": None, "vector_score": 0.0, "rrf": 0.0, "exact": False, "graph_score": candidate.graph_score, "graph_reasons": list(candidate.rank_breakdown.graph_reasons), "score": candidate.total_score})
    scored.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    # Retrieval is page-first: the public result list keeps one best passage
    # per page, while the internal pack is assembled page-by-page in reading
    # order so multi-section answers (fix steps, install checklists) survive
    # regardless of which sections carried the highest BM25 scores.
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    for index, item in enumerate(scored):
        cancellation.checkpoint_batch(index, every=16, stage="context")
        page_path = item["hit"].page_path
        if page_path not in selected_paths:
            selected.append(item)
            selected_paths.add(page_path)
    selected = _adaptive_expand(selected, top_k)
    selected_paths = {item["hit"].page_path for item in selected}
    recovery = assemble_recovery(
        selected,
        condition=RecoveryCondition(),
        candidates=scored,
        store=store,
        cancellation=cancellation,
    )
    context_items = recovery.context_items
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
    coverage_fallback = False
    lexical_mode = stage_lexical_mode
    raw_store: RetrievalIndexStore | None = None
    raw_snapshot: QueryCorpusSnapshot | None = None
    raw_store_for_snapshot: RetrievalIndexStore | None = None

    def capture_raw_snapshot() -> QueryCorpusSnapshot:
        nonlocal raw_snapshot, raw_store_for_snapshot
        if raw_snapshot is None:
            raw_store_for_snapshot = raw_store_for_snapshot or raw_store or RetrievalIndexStore(root, scope="raw")
            raw_status = raw_store_for_snapshot.status()
            if not raw_status.get("ok") or raw_status.get("state") != "fresh":
                raw_snapshot = QueryCorpusSnapshot.empty("raw")
            else:
                raw_snapshot = QueryCorpusSnapshot.capture(raw_store_for_snapshot, cancellation=cancellation)
        return raw_snapshot
    query_extra_terms: list[str] = []
    query_term_variants: dict[str, list[str]] = {}
    wiki_relaxed_answered = False
    uncovered_latin_terms = (
        _uncovered_latin_terms(question, selected)
        if has_primary_recall and effective_scope in {"knowledge", "all"}
        else []
    )

    cancellation.checkpoint("fallback")
    if uncovered_latin_terms:
        cancellation.checkpoint("fallback")
        raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, _raw_lexical_mode = _raw_recovery_candidates(
            root,
            question,
            project=project,
            filters=filters,
            scope="raw",
            extra_terms=[],
            term_variants={},
            top_k=top_k,
            snapshot=capture_raw_snapshot(),
            cancellation=cancellation,
        )
        raw_candidate_items = [
            item for item in raw_candidate_items if _coverage_terms(item, uncovered_latin_terms)
        ]
        if raw_candidate_items:
            merged = _merge_coverage_items(selected, raw_candidate_items, uncovered_latin_terms, rrf_k=effective_rrf_k)
            selected = _adaptive_expand(merged, top_k)[:top_k]
            selected_paths = {item["hit"].page_path for item in selected}
            combined_items = [*scored, *raw_candidate_items]
            recovery = assemble_recovery(
                selected,
                condition=RecoveryCondition("raw", ("wiki_primary_missing_latin_coverage",), "item"),
                candidates=combined_items,
                store=store,
                raw_store=raw_store,
                cancellation=cancellation,
            )
            context_items = recovery.context_items
            coverage_fallback = any(item["hit"].source_kind == "raw" for item in selected)
    if not has_primary_recall and effective_scope in {"knowledge", "all"}:
        cancellation.checkpoint("fallback")
        # Wiki is the primary corpus.  Try its bounded relaxed projection
        # before opening the independent raw store; a successful Wiki answer
        # must not be mixed with raw evidence or even query the raw DB.
        query_extra_terms, query_term_variants, expansion_suggestions = _query_expansion(
            question,
            store,
            None,
            expansion_terms,
            project,
            snapshot=snapshot,
            raw_snapshot=raw_snapshot,
            cancellation=cancellation,
        )
        cancellation.checkpoint("fallback")
        wiki_relaxed_items, relaxed_fts_hits, relaxed_warning = _relaxed_recovery_items(
            store,
            metadata,
            question,
            scope=effective_scope,
            project=project,
            filters=filters,
            extra_terms=query_extra_terms,
            cancellation=cancellation,
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
            recovery = assemble_recovery(
                selected,
                condition=RecoveryCondition(),
                candidates=wiki_relaxed_items,
                store=store,
                cancellation=cancellation,
            )
            context_items = recovery.context_items
            lexical_mode = "relaxed"
            wiki_relaxed_answered = True

            # ``scope=all`` may use the relaxed Wiki result as the primary
            # answer, but it can still leave an explicit Latin term uncovered.
            # Extend the same bounded coverage path used after strict recall;
            # the default knowledge scope keeps its Wiki-first isolation.
            if effective_scope == "all":
                uncovered_latin_terms = _uncovered_latin_terms(question, selected)
                if uncovered_latin_terms:
                    cancellation.checkpoint("fallback")
                    raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, _raw_lexical_mode = _raw_recovery_candidates(
                        root,
                        question,
                        project=project,
                        filters=filters,
                        scope="raw",
                        extra_terms=query_extra_terms,
                        term_variants=query_term_variants,
                        top_k=top_k,
                        snapshot=capture_raw_snapshot(),
                        cancellation=cancellation,
                    )
                    raw_candidate_items = [
                        item for item in raw_candidate_items if _coverage_terms(item, uncovered_latin_terms)
                    ]
                    if raw_candidate_items:
                        merged = _merge_coverage_items(selected, raw_candidate_items, uncovered_latin_terms, rrf_k=effective_rrf_k)
                        selected = _adaptive_expand(merged, top_k)[:top_k]
                        selected_paths = {item["hit"].page_path for item in selected}
                        combined_items = [*wiki_relaxed_items, *raw_candidate_items]
                        recovery = assemble_recovery(
                            selected,
                            condition=RecoveryCondition("raw", ("wiki_primary_missing_latin_coverage",), "item"),
                            candidates=combined_items,
                            store=store,
                            raw_store=raw_store,
                            cancellation=cancellation,
                        )
                        context_items = recovery.context_items
                        coverage_fallback = any(item["hit"].source_kind == "raw" for item in selected)

    if not has_primary_recall and not wiki_relaxed_answered and effective_scope in {"knowledge", "all"}:
        cancellation.checkpoint("fallback")
        raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, raw_lexical_mode = _raw_recovery_candidates(
            root,
            question,
            project=project,
            filters=filters,
            scope="raw",
            extra_terms=query_extra_terms,
            term_variants=query_term_variants,
            top_k=top_k,
            snapshot=capture_raw_snapshot(),
            cancellation=cancellation,
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
            recovery = assemble_recovery(
                selected,
                condition=RecoveryCondition("raw", ("wiki_zero_results",), "item"),
                candidates=raw_candidate_items,
                store=store,
                raw_store=raw_store,
                cancellation=cancellation,
            )
            context_items = recovery.context_items
            if raw_lexical_mode == "qualified_code" and has_qualified_identifier(question):
                lexical_mode = "qualified_code"
            elif raw_lexical_mode == "identifier_phrase":
                lexical_mode = "identifier_phrase"
            elif raw_lexical_mode == "raw_prefix":
                lexical_mode = "raw_prefix"
            elif raw_lexical_mode == "relaxed":
                lexical_mode = "relaxed"
    cancellation.checkpoint("graph")
    discovery_requested = _discovery_requested(question)
    discovery_source_items: list[dict[str, Any]] = []
    discovery, discovery_entities = _discover_enumerated_entities(selected, context_items)
    discovery, discovery_entities = _constrain_discovery_entities(
        question, discovery, discovery_entities
    )
    if not discovery_entities and discovery_requested:
        # Stage one may be dominated by a generic product passage (for
        # example ``NetSuite``) or may have no FTS term at all for ``N/*``.
        # Locate a bounded catalog candidate from the active projection, then
        # validate same-page structure before exposing any entity.
        discovery_source_items = _discovery_source_items(
            store,
            metadata,
            question,
            scope=effective_scope,
            project=project,
            filters=filters,
            snapshot=snapshot,
            cancellation=cancellation,
        )
        discovery, discovery_entities = _discover_enumerated_entities(
            selected,
            [*context_items, *discovery_source_items],
        )
        discovery, discovery_entities = _constrain_discovery_entities(
            question, discovery, discovery_entities
        )
    if not discovery_entities and discovery_requested and effective_scope in {"knowledge", "all"}:
        # Raw reference projections are a second discovery source, subject to
        # the same active→raw boundary and request filters as entity queries.
        candidate_raw_store = raw_store or RetrievalIndexStore(root, scope="raw")
        raw_status = candidate_raw_store.status()
        if raw_status.get("ok") and raw_status.get("state") == "fresh":
            raw_snapshot = raw_snapshot or capture_raw_snapshot()
            raw_metadata = _store_metadata(
                candidate_raw_store,
                snapshot=raw_snapshot,
                cancellation=cancellation,
            )
            raw_discovery_items = _discovery_source_items(
                candidate_raw_store,
                raw_metadata,
                question,
                scope="raw",
                project=project,
                filters=filters,
                snapshot=raw_snapshot,
                cancellation=cancellation,
            )
            discovery, discovery_entities = _discover_enumerated_entities(
                selected,
                [*context_items, *discovery_source_items, *raw_discovery_items],
            )
            discovery, discovery_entities = _constrain_discovery_entities(
                question, discovery, discovery_entities
            )
            if discovery_entities:
                raw_store = candidate_raw_store
    discovery["requested"] = discovery_requested
    if not discovery_entities:
        discovery["reason"] = "structured_enumeration_evidence_insufficient"
    batch_payload: dict[str, Any] = {
        "status": "not_triggered",
        "reason": "structured_enumeration_evidence_insufficient",
        "entities": [],
    }
    if discovery_entities:
        cancellation.checkpoint("fallback")
        batch_raw_snapshot = raw_snapshot
        if effective_scope in {"knowledge", "all"}:
            batch_raw_snapshot = capture_raw_snapshot()
        batch_raw_store = raw_store or raw_store_for_snapshot
        batch_payload = _run_entity_batch(
            root,
            discovery_entities,
            question,
            primary_store=store,
            effective_scope=effective_scope,
            project=project,
            filters=filters,
            retrieval_mode=retrieval_mode,
            hard_budget_tokens=hard_budget_tokens,
            confirmation_token=confirmation_token,
            snapshot=snapshot,
            raw_snapshot=batch_raw_snapshot,
            raw_store=batch_raw_store,
            cancellation=cancellation,
        )
    public_selected = selected[:top_k]
    additional_selected = selected[top_k:]
    public_paths = {item["hit"].page_path for item in public_selected}
    public_context_items = [
        item for item in context_items if item["hit"].page_path in public_paths
    ]
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
        for item in public_context_items
    ]
    k_budget = min(hard_budget_tokens, 400 * top_k)
    cancellation.checkpoint("context")
    packed: dict[str, Any] = (
        pack_context(passages, hard_limit=hard_budget_tokens, intent=intent, budget_scale=k_budget)
        if include_context_pack
        else {"passages": [], "citations": [], "budget": {"total": k_budget, "used": 0, "omitted": 0}}
    )
    packed_by_path: dict[str, dict[str, Any]] = {}
    for item in packed.get("passages", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        previous = packed_by_path.get(path)
        if previous is None:
            packed_by_path[path] = dict(item)
            continue
        previous["content"] = f"{previous.get('content', '')}\n\n{item.get('content', '')}".strip()
        previous["tokens"] = int(previous.get("tokens") or 0) + int(item.get("tokens") or 0)
    contains_raw = any(item["hit"].source_kind == "raw" for item in selected)
    # Recovery owns the final fallback envelope as well as the intermediate
    # context state.  Keep the public payload projection here, but do not
    # reconstruct level/reasons/path allowlists a second time.
    fallback_payload = dict(recovery.fallback)
    def _result_item(item: dict[str, Any], *, citation: str, include_content: bool) -> dict[str, Any]:
        hit = item["hit"]
        result_metadata = (
            {"type": hit.source_kind, "tags": []}
            if hit.source_kind == "raw"
            else {"type": metadata.get(hit.page_path, {}).get("type"), "tags": metadata.get(hit.page_path, {}).get("tags", [])}
        )
        citation_metadata = _citation_metadata(hit, provenance)
        if citation_metadata:
            result_metadata["provenance"] = citation_metadata
        result: dict[str, Any] = {
            "citation": citation,
            "path": hit.page_path,
            "heading": _heading(hit),
            "score": item["score"],
            "scores": {"fts": hit.score, "vector": item["vector_score"], "rrf": item["rrf"], "graph": item["graph_score"]},
            "source_kind": hit.source_kind,
            "metadata": result_metadata,
        }
        if include_content:
            context = packed_by_path.get(hit.page_path)
            if context:
                result["content"] = context.get("content", "")
                result["tokens"] = context.get("tokens", 0)
                result["evidence_kind"] = context.get("evidence_kind", "")
        return result

    results = [
        _result_item(item, citation=f"[{index}]", include_content=include_context_pack)
        for index, item in enumerate(public_selected, 1)
    ]
    additional_results = [
        _result_item(item, citation=f"[{len(public_selected) + index}]", include_content=False)
        for index, item in enumerate(additional_selected, 1)
    ]
    warnings = list(dict.fromkeys([*filter(None, scope_rules), *vector_warnings, *index_warnings]))
    if raw_index_warning:
        warnings = list(dict.fromkeys([*warnings, raw_index_warning]))
    pipeline: dict[str, Any] = {
        "ranking_version": RANKING_POLICY_VERSION,
        "scope": scope,
        "corpus": "raw" if contains_raw else "archive" if effective_scope == "archive" else "active",
        "authority": "active:formal>project>raw_chat;fallback:wiki_relaxed>raw",
        "intent": intent,
        "retrieval_mode": retrieval_mode,
        "lexical_enabled": lexical_enabled,
        "lexical": {"mode": lexical_mode},
        "coverage": {"uncovered_latin_terms": uncovered_latin_terms, "triggered": coverage_fallback},
        "counters": {
            "fts_hits": len(fts),
            "qualified_fts_hits": qualified_fts_hits,
            "relaxed_fts_hits": relaxed_fts_hits,
            "raw_fts_hits": raw_fts_hits,
            "vector_hits": len(vector),
            "graph_hits": sum(1 for item in selected if item["graph_score"] > 0),
            "selected": len(selected),
            "returned": len(results),
            "additional": len(additional_results),
        },
        "warnings": warnings,
        "fallback": fallback_payload,
    }
    if discovery_requested or discovery_entities or discovery_source_items:
        pipeline["discovery"] = discovery
    if batch_payload.get("status") != "not_triggered":
        pipeline["batch"] = batch_payload
    if debug:
        pipeline["debug"] = [
            {
                "passage_id": (hit := cast(PassageHit, item["hit"])).passage_id,
                "path": hit.page_path,
                "fts_rank": item["fts_rank"],
                "vector_rank": item["vector_rank"],
                "rrf": item["rrf"],
                "graph": item["graph_score"],
                "graph_reasons": item["graph_reasons"],
                "exact_match": item["exact"],
                "final_score": item["score"],
                "coverage_terms": item.get("coverage_terms", []),
                "coverage_ratio": item.get("coverage_ratio"),
                "source_local_rank": item.get("source_local_rank"),
                "source_local_rrf": item.get("source_local_rrf"),
                "fusion_score": item.get("fusion_score"),
                "fusion_source": item.get("fusion_source"),
                "fusion_local_position": item.get("fusion_local_position"),
            }
            for item in selected
        ]
    cancellation.checkpoint("telemetry")
    elapsed = (time.perf_counter() - started) * 1_000
    if telemetry is None or telemetry.enabled:
        (telemetry_recorder or QueryTelemetry(root)).finish_once(
            question=question,
            scope=scope,
            project=project,
            passage_ids=[item["hit"].passage_id for item in selected],
            fallback_level=str(fallback_payload["level"]),
            token_count=int(packed["budget"]["used"]),
            latency_ms=elapsed,
            retention_days=(telemetry.retention_days if telemetry else 90),
            outcome="completed",
        )
    response = {
        "ok": True,
        "question": question,
        "scope": scope,
        "project": project or "",
        "results": results,
        "additional_results": additional_results,
        "expansion_suggestions": expansion_suggestions,
        "budget": packed["budget"],
        "pipeline": pipeline,
    }
    if not results:
        has_discovery_entities = (
            bool(discovery_entities)
            or (isinstance(discovery, dict) and bool(discovery.get("candidate_entities")))
        )
        has_batch_outcome = (
            batch_payload.get("status") not in {None, "not_triggered"}
        )
        if has_discovery_entities or has_batch_outcome:
            response.update(
                code="discovery_only",
                message=(
                    "Structured discovery found entities; inspect "
                    "pipeline.discovery and pipeline.batch."
                ),
            )
        else:
            response.update(
                code="no_results",
                message="No indexed documentation matched the query.",
            )
    return response

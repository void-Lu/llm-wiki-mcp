from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netsuite_llm_wiki_mcp.lexical_analyzer import tokens as lexical_tokens
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore
from netsuite_llm_wiki_mcp.vector_index import DEFAULT_RRF_K, VectorIndexError, VectorIndexStore, VectorRecord, VectorSettings, parse_vector_settings
from netsuite_llm_wiki_mcp.vector_provider import LocalBgeM3Provider, VectorProviderError
from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page, split_frontmatter
from netsuite_llm_wiki_mcp.wikilinks import wikilink_targets

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_STRUCTURAL_PAGE_NAMES = {"index.md", "log.md", "overview.md"}
DEFAULT_TOP_K = 10
_PAGED_NAVIGATION_PAGE_RE = re.compile(r"^(?:index-\d{2,}|_entries(?:-\d{2,})?)\.md$")
RANKING_VERSION = "lexical-vector-rrf-graph-capped-v2"
_SCORE_PRECISION = 12
_GRAPH_SCORE_RATIO_CAP = 0.15
_PURE_GRAPH_SCORE_CAP = 0.75
_MAX_GRAPH_EXPANSIONS_PER_SEED = 64
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


@dataclass(frozen=True)
class CandidateSignals:
    """Lexical contributions kept separate from graph and optional vector signals."""

    total: float
    fields: dict[str, float]
    exact_matches: tuple[dict[str, str], ...]


@dataclass
class RankBreakdown:
    """Internal explanation data exposed only by ``wiki_query_debug``."""

    lexical_fields: dict[str, float] = field(default_factory=dict)
    lexical_exact_matches: list[dict[str, str]] = field(default_factory=list)
    graph_reasons: list[dict[str, Any]] = field(default_factory=list)
    lexical_rank: int | None = None
    vector_rank: int | None = None
    rrf_contribution: float = 0.0
    fusion_total: float = 0.0
    rank: int = 0

    def as_debug_dict(self, candidate: QueryCandidate) -> dict[str, Any]:
        return {
            "lexical": {
                "total": candidate.keyword_score,
                "rank": self.lexical_rank,
                "fields": dict(self.lexical_fields),
                "exact_matches": list(self.lexical_exact_matches),
            },
            "vector": {
                "score": candidate.vector_score,
                "rank": self.vector_rank,
            },
            "graph": {
                "total": candidate.graph_score,
                "reasons": list(self.graph_reasons),
            },
            "fusion": {
                "total": self.fusion_total,
                "rrf_contribution": self.rrf_contribution,
            },
            "rank": self.rank,
        }


@dataclass
class QueryCandidate:
    path: Path
    rel: str
    title: str
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    source_kind: str = "wiki"
    keyword_score: float = 0.0
    vector_score: float = 0.0
    fusion_score: float = 0.0
    graph_score: float = 0.0
    rank_breakdown: RankBreakdown = field(default_factory=RankBreakdown)

    @property
    def total_score(self) -> float:
        return _stable_score(self.fusion_score + self.graph_score)


@dataclass(frozen=True)
class Graph:
    neighbors: dict[str, set[str]]
    sources: dict[str, set[str]]
    types: dict[str, str]


@dataclass
class QueryExecution:
    result: dict[str, Any]
    selected: list[QueryCandidate]
    candidate_count: int
    filtered_out: int


def wiki_query(
    vault_root: str | Path,
    question: str,
    project: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    include_content: bool = True,
    context_window_tokens: int = 16_000,
    include_context_pack: bool = True,
    chat_history: list[dict[str, str]] | None = None,
    language: str = "zh-CN",
    enable_vector: bool = False,
    vector_config: dict[str, Any] | None = None,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
    filter_type: str | None = None,
    filter_tags: list[str] | None = None,
    retrieval_mode: str = "hybrid",
    vector_settings: VectorSettings | None = None,
) -> dict[str, Any]:
    return _execute_query(
        vault_root=vault_root,
        question=question,
        project=project,
        top_k=top_k,
        include_content=include_content,
        context_window_tokens=context_window_tokens,
        include_context_pack=include_context_pack,
        chat_history=chat_history,
        language=language,
        enable_vector=enable_vector,
        vector_config=vector_config,
        retrieval_mode=retrieval_mode,
        max_graph_hops=max_graph_hops,
        include_raw_sources=include_raw_sources,
        filter_type=filter_type,
        filter_tags=filter_tags,
        vector_settings=vector_settings,
        collect_debug=False,
    ).result


def _execute_query(
    vault_root: str | Path,
    question: str,
    project: str | None,
    top_k: int,
    include_content: bool,
    context_window_tokens: int,
    include_context_pack: bool,
    chat_history: list[dict[str, str]] | None,
    language: str,
    enable_vector: bool,
    vector_config: dict[str, Any] | None,
    retrieval_mode: str,
    max_graph_hops: int,
    include_raw_sources: bool,
    filter_type: str | None,
    filter_tags: list[str] | None,
    vector_settings: VectorSettings | None,
    collect_debug: bool,
) -> QueryExecution:
    if retrieval_mode not in {"lexical", "vector", "hybrid"}:
        raise ValueError("retrieval_mode must be lexical, vector, or hybrid")
    root = Path(vault_root).expanduser().resolve()
    tokens = _tokens(question)
    all_candidates = _candidate_pages(root, include_raw_sources=include_raw_sources)
    candidates = all_candidates
    if project:
        candidates = [candidate for candidate in candidates if _in_project_scope(candidate.rel, project)]
    if filter_type:
        candidates = [candidate for candidate in candidates if str(candidate.frontmatter.get("type") or "") == filter_type]
    if filter_tags:
        tag_set = set(filter_tags)
        candidates = [candidate for candidate in candidates if tag_set & set(_as_list(candidate.frontmatter.get("tags")))]
    graph = _build_graph(root, all_candidates)
    token_weights = _token_weights(candidates, tokens)

    scored: dict[str, QueryCandidate] = {}
    for candidate in candidates:
        signals = _keyword_signals(candidate, tokens, question, token_weights)
        if project and candidate.rel.startswith(f"wiki/projects/{project}/"):
            signals = _with_lexical_adjustment(signals, "project_scope", 5.0)
        candidate.keyword_score = signals.total
        candidate.rank_breakdown.lexical_fields = dict(signals.fields)
        candidate.rank_breakdown.lexical_exact_matches = list(signals.exact_matches)
        if candidate.keyword_score > 0 and retrieval_mode != "vector":
            candidate.fusion_score = candidate.keyword_score
            scored[candidate.rel] = candidate

    vector_enabled = enable_vector and retrieval_mode != "lexical"
    vector_warnings, vector_status = _apply_optional_vector_stage(
        root,
        all_candidates,
        candidates,
        scored,
        vector_enabled,
        vector_config,
        question,
        vector_settings=vector_settings,
        include_raw_sources=include_raw_sources,
    )
    _apply_graph_expansion(
        scored,
        candidates,
        graph,
        max_graph_hops=max_graph_hops,
        collect_reasons=collect_debug,
    )

    selected = sorted(scored.values(), key=lambda item: (-item.total_score, item.rel))[:top_k]
    for rank, candidate in enumerate(selected, 1):
        candidate.rank_breakdown.rank = rank
        candidate.rank_breakdown.fusion_total = candidate.fusion_score
    results = [_result_item(candidate, tokens) for candidate in selected]
    context = _legacy_context(selected, tokens, include_content)
    context_pack = _context_pack(root, selected, question, context_window_tokens, chat_history or [], language) if include_context_pack else None

    result = {
        "ok": True,
        "question": question,
        "project": project or "",
        "results": results,
        "context": context,
        "context_pack": context_pack,
        "budget": context_pack.get("budget") if context_pack else {},
        "citations": context_pack.get("citations") if context_pack else [item["citation"] for item in context],
        "pipeline": {
            "stage_0_candidate_count": len(all_candidates),
            "stage_0_filtered_out": len(all_candidates) - len(candidates),
            "stage_1_keyword_hits": sum(1 for item in scored.values() if item.keyword_score > 0),
            "stage_1_ranking_version": RANKING_VERSION,
            "stage_1_raw_sources_included": include_raw_sources,
            "stage_1_5_vector_enabled": vector_enabled,
            "stage_1_5_retrieval_mode": retrieval_mode,
            "stage_1_5_vector_warnings": vector_warnings,
            "stage_1_5_vector_status": vector_status,
            "stage_2_graph_hops": max_graph_hops,
            "stage_2_graph_hits": sum(1 for item in scored.values() if item.graph_score > 0),
            "stage_3_context_window_tokens": context_window_tokens,
            "stage_4_context_pack_enabled": include_context_pack,
        },
        "policy": "Answer from the numbered context pages and cite sources as [1], [2], etc.",
    }
    return QueryExecution(
        result=result,
        selected=selected,
        candidate_count=len(all_candidates),
        filtered_out=len(all_candidates) - len(candidates),
    )


def wiki_query_debug(
    vault_root: str | Path,
    question: str,
    project: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    max_graph_hops: int = 2,
    include_raw_sources: bool = False,
    enable_vector: bool = False,
    vector_config: dict[str, Any] | None = None,
    retrieval_mode: str = "hybrid",
    filter_type: str | None = None,
    filter_tags: list[str] | None = None,
) -> dict[str, Any]:
    execution = _execute_query(
        vault_root=vault_root,
        question=question,
        project=project,
        top_k=top_k,
        include_content=False,
        context_window_tokens=16_000,
        include_context_pack=False,
        chat_history=None,
        language="zh-CN",
        enable_vector=enable_vector,
        vector_config=vector_config,
        retrieval_mode=retrieval_mode,
        max_graph_hops=max_graph_hops,
        include_raw_sources=include_raw_sources,
        filter_type=filter_type,
        filter_tags=filter_tags,
        vector_settings=None,
        collect_debug=True,
    )
    graph_reasons = {
        candidate.rel: list(candidate.rank_breakdown.graph_reasons)
        for candidate in execution.selected
        if candidate.rank_breakdown.graph_reasons
    }
    return {
        **execution.result,
        "graph_reasons": graph_reasons,
        "ranking_debug": {
            "ranking_version": RANKING_VERSION,
            "stage_candidates": execution.candidate_count,
            "filter_rejections": execution.filtered_out,
            "parameters": {
                "max_graph_hops": max_graph_hops,
                "max_graph_expansions_per_seed": _MAX_GRAPH_EXPANSIONS_PER_SEED,
                "graph_score_ratio_cap": _GRAPH_SCORE_RATIO_CAP,
                "pure_graph_score_cap": _PURE_GRAPH_SCORE_CAP,
                "rrf_k": _debug_rrf_k(vector_config),
            },
            "results": [
                {"path": candidate.rel, **candidate.rank_breakdown.as_debug_dict(candidate)}
                for candidate in execution.selected
            ],
        },
    }


def _relationship_reasons(left: str, right: str, graph: Graph) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if right in graph.neighbors.get(left, set()):
        reasons.append({"kind": "direct_wikilink", "source": left, "target": right, "score": 3.0})
    shared_sources = sorted(graph.sources.get(left, set()) & graph.sources.get(right, set()))
    for source in shared_sources:
        reasons.append({"kind": "shared_source", "source": left, "target": right, "value": source, "score": 4.0})
    common = sorted(graph.neighbors.get(left, set()) & graph.neighbors.get(right, set()))
    for neighbor in common:
        degree = len(graph.neighbors.get(neighbor, set()))
        if degree > 1:
            reasons.append({"kind": "common_neighbor", "source": left, "target": right, "value": neighbor, "score": 1.5 / math.log(degree + 1)})
    if graph.types.get(left) and graph.types.get(left) == graph.types.get(right):
        reasons.append({"kind": "same_type", "source": left, "target": right, "value": graph.types[left], "score": 1.0})
    return reasons


def _candidate_pages(root: Path, include_raw_sources: bool = False) -> list[QueryCandidate]:
    store = RetrievalIndexStore(root)
    if store.status().get("ok"):
        candidates = [
            QueryCandidate(
                path=root / str(item["path"]),
                rel=str(item["path"]),
                title=str(item["title"]),
                body=str(item["body"]),
                frontmatter={
                    str(key): value
                    for key, value in (item["frontmatter"] if isinstance(item["frontmatter"], dict) else {}).items()
                },
                source_kind="raw" if str(item["source_kind"]) == "raw_chat" else "wiki",
            )
            for item in store.page_candidates()
            if include_raw_sources or not str(item["path"]).startswith("raw/")
        ]
        return candidates
    # Compatibility fallback for a vault that has not received its first
    # explicit maintenance build. It is deliberately not used once a store is
    # present, so normal query traffic never walks the corpus.
    candidates: list[QueryCandidate] = []
    wiki = root / "wiki"
    if wiki.exists():
        for path in sorted(wiki.rglob("*.md")):
            if _is_structural_page(path):
                continue
            if path.relative_to(root).parts[:2] == ("wiki", "archives"):
                continue
            candidates.append(_wiki_candidate(path, root))
    raw_sources = root / "raw" / "sources"
    if include_raw_sources and raw_sources.exists():
        for path in sorted(raw_sources.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}:
                candidates.append(_raw_candidate(path, root))
    return candidates


def _is_structural_page(path: Path) -> bool:
    """Exclude generated navigation without hiding source-index content leaves."""

    if path.name in _STRUCTURAL_PAGE_NAMES:
        return True
    if not _PAGED_NAVIGATION_PAGE_RE.match(path.name):
        return False
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return frontmatter.get("generated") is True and frontmatter.get("navigation") is True


def _in_project_scope(rel: str, project: str) -> bool:
    parts = Path(rel).parts
    if rel.startswith(f"wiki/projects/{project}/"):
        return True
    if rel.startswith(("wiki/concepts/", "wiki/chatlog/", "wiki/sources/", "wiki/queries/", "wiki/entities/")):
        return True
    if len(parts) >= 5 and parts[0] == "raw" and parts[1] == "sources" and parts[3] == project:
        return True
    return False


def _wiki_candidate(path: Path, root: Path) -> QueryCandidate:
    page = read_markdown_page(path, root)
    return QueryCandidate(
        path=path,
        rel=path.relative_to(root).as_posix(),
        title=page.title,
        body=page.body,
        frontmatter=page.frontmatter,
        source_kind="wiki",
    )


def _raw_candidate(path: Path, root: Path) -> QueryCandidate:
    text = path.read_text(encoding="utf-8", errors="ignore")
    title = path.stem
    if path.suffix.lower() == ".md":
        frontmatter, body = split_frontmatter(text)
        title = str(frontmatter.get("title") or _first_heading(body) or path.stem)
        text = body
    else:
        frontmatter = {}
    return QueryCandidate(
        path=path,
        rel=path.relative_to(root).as_posix(),
        title=title,
        body=text,
        frontmatter=frontmatter,
        source_kind="raw",
    )


def _tokens(text: str) -> list[str]:
    return [token for token in lexical_tokens(text) if token not in _STOPWORDS]


def _keyword_score(candidate: QueryCandidate, tokens: list[str], query: str = "", token_weights: dict[str, float] | None = None) -> float:
    """Compatibility helper for callers that only need the lexical total."""

    return _keyword_signals(candidate, tokens, query, token_weights).total


def _keyword_signals(
    candidate: QueryCandidate,
    tokens: list[str],
    query: str = "",
    token_weights: dict[str, float] | None = None,
) -> CandidateSignals:
    """Score lexical fields without letting repeated long-body terms dominate.

    This is ranking experiment 1 from task 05.  Paths, titles, stems, and
    frontmatter retain their established field weights.  Only body token
    counts are divided by the square root of their character length, so an
    exact title/path still outranks a term repeated across a generated source
    index page.
    """
    if not tokens:
        return CandidateSignals(total=0.0, fields={}, exact_matches=())
    weights = token_weights or {token: 1.0 for token in tokens}
    body = candidate.body.casefold()
    frontmatter = str(candidate.frontmatter).casefold()
    rel = candidate.rel.casefold()
    title = candidate.title.casefold()
    stem = candidate.path.stem.casefold().replace("-", " ").replace("_", " ")
    fields = {"body": 0.0, "path": 0.0, "frontmatter": 0.0, "title": 0.0, "stem": 0.0, "phrase": 0.0}
    exact_matches: list[dict[str, str]] = []
    for token in tokens:
        weight = weights.get(token, 1.0)
        body_matches = body.count(token)
        fields["body"] += _length_normalized_count(body_matches, len(body)) * weight
        fields["path"] += rel.count(token) * weight
        fields["frontmatter"] += frontmatter.count(token) * 0.5 * weight
        if token in title:
            fields["title"] += 15 * weight
            exact_matches.append({"field": "title", "value": token})
        if token in stem:
            fields["stem"] += 8 * weight
            exact_matches.append({"field": "stem", "value": token})
    phrase = query.casefold().strip()
    phrase_weight = sum(weights.get(token, 1.0) for token in tokens) / len(tokens)
    if phrase:
        if title == phrase:
            fields["phrase"] += 120 * phrase_weight
            exact_matches.append({"field": "title", "value": phrase})
        elif phrase in title:
            fields["phrase"] += 50 * phrase_weight
            exact_matches.append({"field": "title_contains", "value": phrase})
        if stem == phrase:
            fields["phrase"] += 80 * phrase_weight
            exact_matches.append({"field": "stem", "value": phrase})
        elif phrase in stem:
            fields["phrase"] += 25 * phrase_weight
            exact_matches.append({"field": "stem_contains", "value": phrase})
        phrase_count = body.count(phrase)
        if phrase_count:
            fields["phrase"] += min(phrase_count, 5) * 4 * phrase_weight
            exact_matches.append({"field": "body_phrase", "value": phrase})
    stable_fields = {field: _stable_score(value) for field, value in fields.items()}
    return CandidateSignals(
        total=_stable_score(sum(stable_fields.values())),
        fields=stable_fields,
        exact_matches=tuple(exact_matches),
    )


def _length_normalized_count(matches: int, field_length: int) -> float:
    """Use a deterministic body-length adjustment for token frequency."""

    if matches <= 0:
        return 0.0
    return matches / math.sqrt(max(field_length, 1))


def _with_lexical_adjustment(signals: CandidateSignals, field: str, adjustment: float) -> CandidateSignals:
    fields = {**signals.fields, field: _stable_score(signals.fields.get(field, 0.0) + adjustment)}
    return CandidateSignals(
        total=_stable_score(sum(fields.values())),
        fields=fields,
        exact_matches=signals.exact_matches,
    )


def _stable_score(score: float) -> float:
    return round(score, _SCORE_PRECISION)


def _token_weights(candidates: list[QueryCandidate], tokens: list[str]) -> dict[str, float]:
    if not tokens or not candidates:
        return {}
    total = len(candidates)
    weights: dict[str, float] = {}
    for token in tokens:
        document_frequency = sum(1 for candidate in candidates if token in _search_text(candidate))
        weights[token] = 1.0 + math.log((total + 1) / (document_frequency + 1))
    return weights


def _search_text(candidate: QueryCandidate) -> str:
    return "\n".join([candidate.rel, candidate.title, str(candidate.frontmatter), candidate.body]).casefold()


def _apply_optional_vector_stage(
    root: Path,
    index_candidates: list[QueryCandidate],
    candidates: list[QueryCandidate],
    scored: dict[str, QueryCandidate],
    enable_vector: bool,
    vector_config: dict[str, Any] | None,
    question: str,
    *,
    vector_settings: VectorSettings | None = None,
    include_raw_sources: bool,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Run independent vector recall without allowing it to mutate an index."""

    if not enable_vector:
        return [], {"state": "disabled"}
    if vector_settings is None and not vector_config:
        return [
            {"code": "vector_config_missing", "message": "vector search was requested but vector_config was not provided"}
        ], {"state": "unconfigured"}
    try:
        settings = vector_settings or parse_vector_settings(root, vector_config)
        if settings.model_path is None:
            raise VectorIndexError("model_missing", "a local model_path is required when vector search is enabled")
        store = VectorIndexStore(root, settings.index_path)
        index_records = vector_index_records(root, include_raw_sources=include_raw_sources)
        status = store.status(index_records, include_raw_sources=include_raw_sources)
        if not status.get("ok") or status.get("state") != "fresh":
            code = str(status.get("code") or "index_stale")
            return [{"code": code, "message": "vector index is unavailable; keyword and graph stages were used"}], status
        provider = LocalBgeM3Provider(
            settings.model_path,
            device=settings.device,
            batch_size=settings.batch_size,
            max_sequence_length=settings.max_sequence_length,
        )
        store.validate_provider(provider.identity(), include_raw_sources=include_raw_sources)
    except (VectorIndexError, VectorProviderError) as exc:
        code = exc.code
        return [{"code": code, "message": "vector retrieval is unavailable; keyword and graph stages were used"}], {"state": "unavailable", "code": code}

    return _vector_recall(
        question,
        candidates,
        scored,
        settings=settings,
        store=store,
        provider=provider,
    )


def _vector_recall(
    question: str,
    candidates: list[QueryCandidate],
    scored: dict[str, QueryCandidate],
    *,
    settings: Any,
    store: VectorIndexStore,
    provider: LocalBgeM3Provider,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    results = store.search(
        provider.embed_query(question),
        allowed_paths={candidate.rel for candidate in candidates},
        limit=settings.candidate_limit,
    )
    # Drop weak vector matches so that nonsensical queries do not produce
    # false-positive results.  Keyword and graph candidates are unaffected.
    results = [result for result in results if result.score >= settings.min_vector_score]
    candidates_by_rel = {candidate.rel: candidate for candidate in candidates}
    lexical_ranked = sorted(scored.values(), key=lambda candidate: (-candidate.keyword_score, candidate.rel))
    for rank, candidate in enumerate(lexical_ranked, 1):
        candidate.rank_breakdown.lexical_rank = rank
    for result in results:
        candidate = candidates_by_rel[result.path]
        candidate.vector_score = result.score
        candidate.rank_breakdown.vector_rank = result.rank
        scored.setdefault(candidate.rel, candidate)
    for candidate in scored.values():
        lexical = candidate.rank_breakdown.lexical_rank
        vector = candidate.rank_breakdown.vector_rank
        contribution = (1.0 / (settings.rrf_k + lexical) if lexical else 0.0) + (1.0 / (settings.rrf_k + vector) if vector else 0.0)
        candidate.rank_breakdown.rrf_contribution = _stable_score(contribution)
        # Scale RRF to a 0–2 range comparable to keyword scores (max raw RRF
        # is 2/(rrf_k+1); multiplying by (rrf_k+1) maps it to 2.0), then add
        # it to the existing fusion_score. In hybrid mode this preserves the
        # keyword signal that graph expansion depends on, while in vector-only
        # mode the base is 0 so fusion_score becomes the scaled RRF alone.
        scaled_rrf = _stable_score(contribution * (settings.rrf_k + 1))
        candidate.fusion_score = _stable_score(candidate.fusion_score + scaled_rrf)
    return [], {"state": "ready", "candidate_count": len(results), "rrf_k": settings.rrf_k}


def _vector_records(candidates: list[QueryCandidate]) -> list[VectorRecord]:
    records: list[VectorRecord] = []
    for candidate in candidates:
        content = {"title": candidate.title, "body": candidate.body, "frontmatter": candidate.frontmatter}
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
        records.append(
            VectorRecord(
                path=candidate.rel,
                content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                text=f"{candidate.title}\n{candidate.body}",
                source_kind=candidate.source_kind,
            )
        )
    return records


def vector_index_records(vault_root: str | Path, *, include_raw_sources: bool = False) -> list[VectorRecord]:
    """Return the complete eligible corpus for explicit index lifecycle actions."""

    root = Path(vault_root).expanduser().resolve()
    store = RetrievalIndexStore(root)
    if store.status().get("ok"):
        return [
            VectorRecord(
                path=record["page_path"],
                page_path=record["page_path"],
                passage_id=record["passage_id"],
                content_hash=record["content_hash"],
                text=record["text"],
                source_kind=record["source_kind"],
                corpus="active",
            )
            for record in store.vector_records()
            if include_raw_sources or record["corpus"] != "history"
        ]
    return _vector_records(_candidate_pages(root, include_raw_sources=include_raw_sources))


def _debug_rrf_k(vector_config: dict[str, Any] | None) -> int:
    if not vector_config or isinstance(vector_config.get("rrf_k"), bool):
        return DEFAULT_RRF_K
    try:
        value = int(vector_config["rrf_k"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_RRF_K
    return value if 1 <= value <= 10_000 else DEFAULT_RRF_K


def _build_graph(root: Path, candidates: list[QueryCandidate] | None = None) -> Graph:
    if candidates is None:
        candidates = _candidate_pages(root)
    wiki_candidates = [candidate for candidate in candidates if candidate.rel.startswith("wiki/")]
    by_rel = {candidate.rel: candidate.path for candidate in wiki_candidates}
    by_candidate = {candidate.rel: candidate for candidate in wiki_candidates}
    by_stem: dict[str, list[str]] = {}
    for rel, path in by_rel.items():
        by_stem.setdefault(path.stem.casefold(), []).append(rel)

    neighbors = {rel: set() for rel in by_rel}
    sources: dict[str, set[str]] = {}
    types: dict[str, str] = {}
    for rel, path in by_rel.items():
        candidate = by_candidate[rel]
        sources[rel] = {str(item) for item in _as_list(candidate.frontmatter.get("sources"))}
        types[rel] = str(candidate.frontmatter.get("type") or _path_type(rel))
        for target in _wikilink_targets(candidate.body, path, root, by_rel, by_stem):
            neighbors[rel].add(target)
            neighbors.setdefault(target, set()).add(rel)
    return Graph(neighbors=neighbors, sources=sources, types=types)


def _wikilink_targets(body: str, path: Path, root: Path, by_rel: dict[str, Path], by_stem: dict[str, list[str]]) -> list[str]:
    targets = []
    for target in wikilink_targets(body):
        target_path = Path(target)
        candidates: list[Path] = []
        if target_path.suffix != ".md":
            target_path = target_path.with_suffix(".md")
        candidates.extend([(path.parent / target_path).resolve(), (root / "wiki" / target_path).resolve(), (root / target_path).resolve()])
        matched = ""
        for candidate in candidates:
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in by_rel:
                matched = rel
                break
        if not matched:
            stem_matches = by_stem.get(Path(target).stem.casefold(), [])
            if len(stem_matches) == 1:
                matched = stem_matches[0]
        if matched:
            targets.append(matched)
    return targets


def _apply_graph_expansion(
    scored: dict[str, QueryCandidate],
    all_candidates: list[QueryCandidate],
    graph: Graph,
    max_graph_hops: int,
    *,
    collect_reasons: bool,
) -> None:
    """Add bounded graph evidence without bypassing public query filters."""

    candidates_by_rel = {candidate.rel: candidate for candidate in all_candidates}
    seeds = sorted(rel for rel, candidate in scored.items() if candidate.source_kind == "wiki")
    for seed in seeds:
        frontier = {seed: seed}
        visited = {seed}
        expansions = 0
        for hop in range(1, max_graph_hops + 1):
            next_frontier: dict[str, str] = {}
            for via in sorted(frontier):
                for rel in sorted(graph.neighbors.get(via, set()) - visited):
                    # ``all_candidates`` is already scoped by project/type/tag
                    # filters.  Do not permit an excluded graph page to bridge
                    # to a result that would otherwise be unreachable.
                    if rel in candidates_by_rel and rel not in next_frontier:
                        next_frontier[rel] = via
            remaining = _MAX_GRAPH_EXPANSIONS_PER_SEED - expansions
            if remaining <= 0:
                break
            expanded_paths = sorted(next_frontier)[:remaining]
            decay = 1 / hop
            for rel in expanded_paths:
                candidate = candidates_by_rel.get(rel)
                if candidate is None:
                    continue
                relationship_reasons = _relationship_reasons(seed, rel, graph) if collect_reasons else []
                relationship_score = sum(float(reason["score"]) for reason in relationship_reasons)
                if not collect_reasons:
                    relationship_score = _relationship_score(seed, rel, graph)
                raw_contribution = relationship_score * decay
                graph_cap = _graph_score_cap(candidate)
                applied = max(0.0, min(raw_contribution, graph_cap - candidate.graph_score))
                if collect_reasons:
                    if not relationship_reasons:
                        relationship_reasons = [{"kind": "graph_path", "source": seed, "target": rel, "score": 0.0}]
                    candidate.rank_breakdown.graph_reasons.extend(
                        {**reason, "hop": hop, "via": next_frontier[rel]}
                        for reason in relationship_reasons
                    )
                    candidate.rank_breakdown.graph_reasons.append(
                        {
                            "kind": "graph_expansion",
                            "source": seed,
                            "target": rel,
                            "via": next_frontier[rel],
                            "hop": hop,
                            "score": _stable_score(applied),
                            "raw_contribution": _stable_score(raw_contribution),
                            "cap": _stable_score(graph_cap),
                        }
                    )
                if applied > 0:
                    candidate.graph_score = _stable_score(candidate.graph_score + applied)
                    scored.setdefault(rel, candidate)
            expansions += len(expanded_paths)
            visited.update(expanded_paths)
            frontier = {rel: next_frontier[rel] for rel in expanded_paths}
            if not frontier:
                break


def _graph_score_cap(candidate: QueryCandidate) -> float:
    # Use the strongest relevance signal as the graph cap base. In lexical
    # mode fusion_score equals keyword_score; in hybrid mode fusion_score
    # is keyword_score + scaled_rrf. Considering keyword_score and
    # vector_score explicitly keeps graph expansion proportional even when
    # the fusion_score has been reduced by rank-fusion scaling.
    base = max(candidate.fusion_score, candidate.keyword_score, candidate.vector_score)
    if base > 0:
        return _stable_score(base * _GRAPH_SCORE_RATIO_CAP)
    return _PURE_GRAPH_SCORE_CAP


def _relationship_score(left: str, right: str, graph: Graph) -> float:
    score = 0.0
    if right in graph.neighbors.get(left, set()):
        score += 3.0
    shared_sources = graph.sources.get(left, set()) & graph.sources.get(right, set())
    if shared_sources:
        score += 4.0 * len(shared_sources)
    common = graph.neighbors.get(left, set()) & graph.neighbors.get(right, set())
    for neighbor in common:
        degree = len(graph.neighbors.get(neighbor, set()))
        if degree > 1:
            score += 1.5 / math.log(degree + 1)
    if graph.types.get(left) and graph.types.get(left) == graph.types.get(right):
        score += 1.0
    return score


def _result_item(candidate: QueryCandidate, tokens: list[str]) -> dict[str, Any]:
    return {
        "path": candidate.rel,
        "title": candidate.title,
        "snippet": _snippet(candidate.body, tokens),
        "title_match": _title_match(candidate, tokens),
        "score": candidate.total_score,
        "scores": {
            "keyword": candidate.keyword_score,
            "vector": candidate.vector_score,
            "graph": candidate.graph_score,
        },
        "images": _images(candidate.body),
        "frontmatter": candidate.frontmatter,
        "source_kind": candidate.source_kind,
    }


def _title_match(candidate: QueryCandidate, tokens: list[str]) -> bool:
    title = candidate.title.casefold()
    return any(token in title for token in tokens)


def _images(body: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    images: list[dict[str, str]] = []
    for alt, url in _IMAGE_RE.findall(body):
        if url in seen:
            continue
        seen.add(url)
        images.append({"url": url, "alt": alt})
    return images


def _legacy_context(candidates: list[QueryCandidate], tokens: list[str], include_content: bool) -> list[dict[str, Any]]:
    context = []
    for index, candidate in enumerate(candidates, 1):
        snippet = _snippet(candidate.body, tokens)
        context.append({
            "citation": f"[{index}] {candidate.rel}",
            "path": candidate.rel,
            "title": candidate.title,
            "content": candidate.body if include_content else snippet,
            "frontmatter": candidate.frontmatter,
        })
    return context


def _context_pack(
    root: Path,
    candidates: list[QueryCandidate],
    question: str,
    context_window_tokens: int,
    chat_history: list[dict[str, str]],
    language: str,
) -> dict[str, Any]:
    total = max(4_000, min(1_000_000, context_window_tokens))
    budgets = {
        "wiki_pages": int(total * 0.60),
        "chat_history": int(total * 0.20),
        "index": int(total * 0.05),
        "system": total - int(total * 0.60) - int(total * 0.20) - int(total * 0.05),
    }
    system_text = _system_prompt(root, question, language)
    index_text = _read_optional(root / "wiki" / "index.md")
    chat_text = _chat_history_text(chat_history)

    used = {
        "system": _token_count(_truncate_tokens(system_text, budgets["system"])),
        "index": _token_count(_truncate_tokens(index_text, budgets["index"])),
        "chat_history": _token_count(_truncate_tokens(chat_text, budgets["chat_history"])),
        "wiki_pages": 0,
    }
    pages = []
    remaining = budgets["wiki_pages"]
    citations = []
    for index, candidate in enumerate(candidates, 1):
        text = _numbered_page(index, candidate)
        chunk = _truncate_tokens(text, remaining)
        if not chunk.strip():
            break
        page_tokens = _token_count(chunk)
        used["wiki_pages"] += page_tokens
        remaining -= page_tokens
        pages.append({"citation": f"[{index}]", "path": candidate.rel, "title": candidate.title, "content": chunk})
        citations.append({"citation": f"[{index}]", "path": candidate.rel, "title": candidate.title})
        if remaining <= 0:
            break

    return {
        "system": _truncate_tokens(system_text, budgets["system"]),
        "index": _truncate_tokens(index_text, budgets["index"]),
        "chat_history": _truncate_tokens(chat_text, budgets["chat_history"]),
        "pages": pages,
        "citations": citations,
        "budget": {"total": total, "allocated": budgets, "used": used},
    }


def _system_prompt(root: Path, question: str, language: str) -> str:
    purpose = _read_optional(root / "purpose.md")
    schema = _read_optional(root / "schema.md")
    return "\n".join([
        "You answer using the numbered LLM Wiki context pages.",
        f"Language: {language}",
        "Cite sources with [1], [2], etc. Do not cite pages that are not in the context pack.",
        f"Question: {question}",
        "\n# purpose.md",
        purpose,
        "\n# schema.md",
        schema,
    ])


def _numbered_page(index: int, candidate: QueryCandidate) -> str:
    return "\n".join([
        f"[{index}] {candidate.title}",
        f"Path: {candidate.rel}",
        f"Frontmatter: {candidate.frontmatter}",
        "",
        candidate.body,
    ])


def _chat_history_text(chat_history: list[dict[str, str]]) -> str:
    lines = []
    for message in chat_history:
        role = str(message.get("role", "message"))
        content = str(message.get("content", ""))
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _truncate_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    return text if len(text) <= max_chars else text[:max_chars].rstrip()


def _token_count(text: str) -> int:
    return math.ceil(len(text) / 4)


def _snippet(body: str, tokens: list[str], length: int = 180) -> str:
    folded = body.casefold()
    first = min((folded.find(token) for token in tokens if token in folded), default=-1)
    if first < 0:
        return body[:length]
    start = max(0, first - 40)
    return body[start : start + length]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _path_type(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "wiki":
        if parts[1] == "projects" and len(parts) >= 4:
            return parts[3]
        return parts[1]
    return "page"


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""

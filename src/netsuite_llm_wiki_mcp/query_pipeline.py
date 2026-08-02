"""Query V2: typed, passage-first retrieval behind the compact MCP response."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from netsuite_llm_wiki_mcp.context_packer import ContextPassage, pack_context
from netsuite_llm_wiki_mcp.content_redaction import redact_for_index
from netsuite_llm_wiki_mcp.fallback_policy import decide_fallback
from netsuite_llm_wiki_mcp.passage_chunker import chunk_markdown
from netsuite_llm_wiki_mcp.query_telemetry import QueryTelemetry
from netsuite_llm_wiki_mcp.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from netsuite_llm_wiki_mcp.runtime_config import EmbeddingSettings, TelemetrySettings
from netsuite_llm_wiki_mcp.vector_index import VectorIndexError, VectorIndexStore, vector_settings_from_embedding
from netsuite_llm_wiki_mcp.vector_provider import LocalBgeM3Provider, VectorProviderError
from netsuite_llm_wiki_mcp.wiki_query import QueryCandidate, _apply_graph_expansion, _build_graph
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter


RANKING_POLICY_VERSION = "query-v2-passage-rrf-1"
RRF_K = 60
_HISTORY_RE = re.compile(r"(?:之前|上次|讨论|会话|当时|历史|previous|last\s+(?:time|session)|history)", re.I)
_EXACT_RE = re.compile(r"(?:原文|逐字|代码|字段|field\s+id|api|record|script|exact|verbatim)", re.I)
_COMPARE_RE = re.compile(r"(?:比较|区别|差异|对比|compare|versus|vs\.?|difference)", re.I)
_RESEARCH_RE = re.compile(r"(?:研究|深入|调研|research|deep\s+dive)", re.I)


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
        return 0.0
    return 0.15 * len(question_terms & title_terms) / len(question_terms)


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


def _fallback_passages(
    root: Path,
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    question: str,
    decision_level: str,
    allowed_source_paths: tuple[str, ...],
) -> list[ContextPassage]:
    """Append only explicitly-authorized capsules/raw chunks, never full raw."""
    allowed = set(allowed_source_paths)
    result: list[ContextPassage] = []
    if decision_level in {"capsule", "raw"} and allowed:
        capsule_paths = [
            path for path, frontmatter in metadata.items()
            if "/capsules/" in path
            and bool(set(frontmatter.get("sources", []) if isinstance(frontmatter.get("sources"), list) else [frontmatter.get("sources")]) & allowed)
        ]
        for hit in store.passages_for_pages(capsule_paths, limit_per_page=1):
            result.append(ContextPassage(hit.passage_id, hit.page_path, _heading(hit), hit.text, 0.0, "source_capsule"))
    if decision_level != "raw":
        return result
    terms = {term.casefold() for term in re.findall(r"[\w一-鿿]+", question) if len(term) > 1}
    for relative in sorted(allowed):
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root / "raw") or not candidate.is_file():
            continue
        try:
            _frontmatter, body = split_frontmatter(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        chunks = chunk_markdown(relative, redact_for_index(body).text)
        ranked = sorted(chunks, key=lambda chunk: (-sum(chunk.text.casefold().count(term) for term in terms), chunk.ordinal))
        if ranked and (not terms or sum(ranked[0].text.casefold().count(term) for term in terms) > 0):
            chunk = ranked[0]
            result.append(ContextPassage(chunk.passage_id, relative, " / ".join(chunk.heading_path) or Path(relative).name, chunk.text, 0.0, "raw_evidence"))
    return result


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
        return {"ok": True, "question": question, "scope": scope, "results": [], "context_pack": {"passages": [], "citations": [], "budget": {"total": min(hard_budget_tokens, 4_000), "used": 0}}, "pipeline": {"ranking_version": RANKING_POLICY_VERSION, "warnings": [str(status.get("code"))], "fallback": {"level": "none", "reasons": ["index_unavailable"], "allowed_source_paths": []}}}

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
        total = round(rrf * (RRF_K + 1) + _authority_bonus(hit, intent, effective_scope, metadata) + _title_overlap_bonus(hit, question) + exact * 0.5, 12)
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
    # Retrieval is passage-first, but the public result list and compact pack
    # must not repeat the same document merely because several of its passages
    # matched.  Keep the highest ranked passage for each page deterministically.
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    for item in scored:
        page_path = item["hit"].page_path
        if page_path in selected_paths:
            continue
        selected.append(item)
        selected_paths.add(page_path)
        if len(selected) >= top_k:
            break
    passages = [
        ContextPassage(
            item["hit"].passage_id,
            item["hit"].page_path,
            _heading(item["hit"]),
            item["hit"].text,
            item["score"],
            "history_evidence" if item["hit"].corpus == "history" else "formal_knowledge",
            _citation_metadata(item["hit"], provenance),
        )
        for item in selected
    ]
    packed: dict[str, Any] = pack_context(passages, hard_limit=hard_budget_tokens, intent=intent) if include_context_pack else {"passages": [], "citations": [], "budget": {"total": min(hard_budget_tokens, 16_000), "used": 0, "omitted": 0}}
    stale = any(str(metadata.get(item["hit"].page_path, {}).get("freshness") or "") in {"stale", "review_required"} for item in selected)
    sources: list[str] = []
    for item in selected:
        raw_sources = metadata.get(item["hit"].page_path, {}).get("sources", [])
        values = raw_sources if isinstance(raw_sources, (list, tuple)) else [raw_sources]
        sources.extend(str(path) for path in values if isinstance(path, str))
    fallback = decide_fallback(intent=intent, top_score=selected[0]["score"] if selected else 0.0, eligible_formal_count=sum(item["hit"].corpus != "history" for item in selected), citation_count=len(packed["citations"]), stale=stale, source_paths=sources)
    fallback_evidence = _fallback_passages(root, store, metadata, question, fallback.level, fallback.allowed_source_paths) if include_context_pack else []
    if fallback_evidence:
        before = int(packed["budget"]["used"])
        packed = pack_context([*passages, *fallback_evidence], hard_limit=hard_budget_tokens, intent=intent)
        fallback_payload = fallback.as_dict()
        fallback_payload["added_token_usage"] = max(0, int(packed["budget"]["used"]) - before)
    else:
        fallback_payload = fallback.as_dict()
        fallback_payload["added_token_usage"] = 0
    results = [{"path": item["hit"].page_path, "heading": _heading(item["hit"]), "snippet": item["hit"].text[:240], "score": item["score"], "scores": {"fts": item["hit"].score, "vector": item["vector_score"], "rrf": item["rrf"], "graph": item["graph_score"]}, "source_kind": item["hit"].source_kind, "metadata": {"type": metadata.get(item["hit"].page_path, {}).get("type"), "tags": metadata.get(item["hit"].page_path, {}).get("tags", [])}} for item in selected]
    pipeline: dict[str, Any] = {"ranking_version": RANKING_POLICY_VERSION, "scope": scope, "corpus": "archive" if effective_scope == "archive" else "active", "authority": "formal>capsule>raw>history", "intent": intent, "retrieval_mode": retrieval_mode, "counters": {"fts_hits": len(fts), "vector_hits": len(vector), "graph_hits": sum(1 for item in selected if item["graph_score"] > 0), "selected": len(selected)}, "warnings": [*filter(None, scope_rules), *vector_warnings], "fallback": fallback_payload}
    if debug:
        pipeline["debug"] = [{"passage_id": item["hit"].passage_id, "path": item["hit"].page_path, "fts_rank": item["fts_rank"], "vector_rank": item["vector_rank"], "rrf": item["rrf"], "graph": item["graph_score"], "graph_reasons": item["graph_reasons"], "exact_match": item["exact"], "final_score": item["score"]} for item in selected]
    elapsed = (time.perf_counter() - started) * 1_000
    if telemetry is None or telemetry.enabled:
        QueryTelemetry(root).record(question=question, scope=scope, project=project, passage_ids=[item["hit"].passage_id for item in selected], fallback_level=fallback.level, token_count=int(packed["budget"]["used"]), latency_ms=elapsed, retention_days=(telemetry.retention_days if telemetry else 90))
    return {"ok": True, "question": question, "scope": scope, "project": project or "", "results": results, "context_pack": packed, "pipeline": pipeline}


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

"""Query V2: typed, page-first retrieval behind the canonical MCP response."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from retrieval.body_budget import result_floor_budget
from retrieval.candidate_items import candidate_item
from retrieval.context_packer import ContextPassage, pack_context
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_recovery import (
    DEFAULT_RECOVERY_CONDITION,
    assemble_recovery,
    fallback_envelope,
    fusion_score,
    select_best_per_page,
)
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.query_telemetry import QueryTelemetry
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from retrieval.metadata_filters import page_matches_filters
from runtime.runtime_config import EmbeddingSettings, TelemetrySettings
from retrieval.query_execution_context import (
    DEFAULT_TOP_K,
    PASSAGE_PROBE_LIMIT as _PASSAGE_PROBE_LIMIT,
    PASSAGE_SCAN_LIMIT as _PASSAGE_SCAN_LIMIT,
    RANKING_POLICY_VERSION,
    RRF_K,
    QueryExecutionContext,
    QueryFilters,
    _adaptive_expand,
    _citation_metadata,
    _effective_rrf_k,
    _eligible,
    _effective_scope,
    _graph_expand,
    _heading,
    _matches_request,
    _stage_one_fts_hits,
    _thaw_value,
    _title_candidates,
    classify_intent,
    _vector_hits as _context_vector_hits,
    probe_hit,
)
from retrieval.vector_provider import LocalBgeM3Provider


PASSAGE_SCAN_LIMIT = _PASSAGE_SCAN_LIMIT
PASSAGE_PROBE_LIMIT = _PASSAGE_PROBE_LIMIT


def _vector_hits(*args: Any, **kwargs: Any) -> tuple[dict[str, tuple[int, float]], list[str]]:
    """Keep the legacy pipeline patch seam while delegating to the context owner."""

    return _context_vector_hits(
        *args,
        provider_factory=LocalBgeM3Provider,
        **kwargs,
    )



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
        k_budget = result_floor_budget(top_k, hard_budget_tokens)
        return {
            "ok": True,
            "code": str(status.get("code") or "index_unavailable"),
            "message": "The retrieval index is unavailable; the query was not executed.",
            "question": question,
            "scope": scope,
            "results": [],
            "additional_results": [],
            "budget": {"total": k_budget, "used": 0},
            "pipeline": {"ranking_version": RANKING_POLICY_VERSION, "warnings": [*index_warnings, str(status.get("code"))], "fallback": fallback_envelope("none", ("index_unavailable",), ())},
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
            probe_hit(
                str(item["path"]),
                str(item["title"]),
                corpus=str(item.get("corpus") or "active"),
                authority=str(item.get("authority") or ""),
                source_kind=str(item.get("source_kind") or ""),
            ),
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
        item = ranked.setdefault(hit.passage_id, candidate_item(hit, score=0.0, fts_rank=rank))
        item["fts_rank"] = rank
    # A vector hit is identified by passage ID.  Load its existing retrieval
    # projection rather than scanning Markdown, so vector-only recall remains
    # available without adding query-time corpus reads.
    for index, hit in enumerate(store.load_passages(vector)):
        cancellation.checkpoint_batch(index, every=16, stage="vector")
        if not _eligible(hit, metadata, scope=effective_scope) or not _matches_request(hit, metadata, project=project, filters=filters):
            continue
        ranked.setdefault(hit.passage_id, candidate_item(hit, score=0.0))
    # A title-only match is deliberately not primary retrieval.  A generic
    # title overlap (such as "script") must not prevent a natural-language
    # question, in any language, from using relaxed lexical recovery.
    has_primary_recall = bool(ranked)
    expansion_suggestions: list[str] = []
    for rank, hit in enumerate(_title_candidates(store, metadata, question, scope=effective_scope, project=project, filters=filters, snapshot=snapshot, cancellation=cancellation), 1):
        cancellation.checkpoint_batch(rank - 1, every=16, stage="vector")
        ranked.setdefault(hit.passage_id, candidate_item(hit, score=0.0, title_rank=rank))
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
        scored.append(
            {
                **item,
                **fusion_score(
                    hit,
                    question,
                    intent,
                    effective_scope,
                    metadata,
                    item,
                    effective_rrf_k=effective_rrf_k,
                ),
                "graph_score": 0.0,
                "graph_reasons": [],
            }
        )
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
        scored.append(
            candidate_item(
                hit,
                score=candidate.total_score,
                graph_score=candidate.graph_score,
                graph_reasons=list(candidate.rank_breakdown.graph_reasons),
            )
        )
    scored.sort(key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id))
    # Retrieval is page-first: the public result list keeps one best passage
    # per page, while the internal pack is assembled page-by-page in reading
    # order so multi-section answers (fix steps, install checklists) survive
    # regardless of which sections carried the highest BM25 scores.
    for index in range(0, len(scored), 16):
        cancellation.checkpoint_batch(index, every=16, stage="context")
    selected = select_best_per_page(scored)
    selected = _adaptive_expand(selected, top_k)
    recovery = assemble_recovery(
        selected,
        condition=DEFAULT_RECOVERY_CONDITION,
        candidates=scored,
        store=store,
        cancellation=cancellation,
    )
    execution = QueryExecutionContext(
        root=root,
        store=store,
        cancellation=cancellation,
        status=status,
    )
    execution._run_fallback_recovery(
        question=question,
        effective_scope=effective_scope,
        project=project,
        filters=filters,
        top_k=top_k,
        metadata=metadata,
        snapshot=snapshot,
        expansion_terms=expansion_terms,
        intent=intent,
        effective_rrf_k=effective_rrf_k,
        has_primary_recall=has_primary_recall,
        selected=selected,
        scored=scored,
        recovery=recovery,
        stage_lexical_mode=stage_lexical_mode,
    )
    cancellation.checkpoint("graph")
    execution._run_discovery_and_batch(
        question=question,
        metadata=metadata,
        effective_scope=effective_scope,
        project=project,
        filters=filters,
        snapshot=snapshot,
        retrieval_mode=retrieval_mode,
        hard_budget_tokens=hard_budget_tokens,
        confirmation_token=confirmation_token,
    )
    outcome = execution.outcome()
    selected = _thaw_value(outcome.selected)
    context_items = _thaw_value(outcome.context_items)
    recovery = outcome.recovery
    raw_fts_hits = outcome.raw_fts_hits
    relaxed_fts_hits = outcome.relaxed_fts_hits
    raw_index_warning = outcome.raw_index_warning
    coverage_fallback = outcome.coverage_fallback
    lexical_mode = outcome.lexical_mode
    expansion_suggestions = _thaw_value(outcome.expansion_suggestions)
    uncovered_latin_terms = _thaw_value(outcome.uncovered_latin_terms)
    discovery = _thaw_value(outcome.discovery)
    discovery_entities = _thaw_value(outcome.discovery_entities)
    discovery_source_items = _thaw_value(outcome.discovery_source_items)
    discovery_requested = outcome.discovery_requested
    batch_payload = _thaw_value(outcome.batch_payload)
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
        )
        for item in public_context_items
    ]
    k_budget = result_floor_budget(top_k, hard_budget_tokens)
    cancellation.checkpoint("context")
    packed: dict[str, Any] = (
        pack_context(passages, hard_limit=hard_budget_tokens, intent=intent, budget_scale=k_budget)
        if include_context_pack
        else {"passages": [], "budget": {"total": k_budget, "used": 0, "omitted": 0}}
    )
    packed_passages = [
        item
        for item in packed.get("passages", [])
        if isinstance(item, dict)
    ]
    contains_raw = any(item["hit"].source_kind == "raw" for item in selected)
    # Recovery owns the final fallback envelope as well as the intermediate
    # context state.  Keep the public payload projection here, but do not
    # reconstruct level/reasons/path allowlists a second time.
    fallback_payload = _thaw_value(recovery.fallback)
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
            context = next(
                (
                    packed_item
                    for packed_item in packed_passages
                    if str(packed_item.get("path") or "") == hit.page_path
                ),
                None,
            )
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

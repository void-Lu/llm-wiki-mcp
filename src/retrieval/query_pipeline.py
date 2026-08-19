"""Query V2: typed, page-first retrieval behind the canonical MCP response."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from retrieval.body_budget import result_floor_budget
from retrieval.candidate_items import candidate_item
from retrieval.context_packer import ContextPassage, pack_context
from retrieval.graph_retrieval import QueryCandidate, apply_graph_expansion, build_graph
from retrieval.lexical_analyzer import has_qualified_identifier
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_recovery import (
    DEFAULT_RECOVERY_CONDITION,
    LadderStep,
    assemble_recovery,
    fallback_envelope,
    fusion_score,
    search_ladder,
    select_best_per_page,
)
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.query_shared import (
    QueryFilters,
    eligible,
    heading,
    matches_request,
    probe_hit,
)
from retrieval.query_telemetry import QueryTelemetry
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore
from retrieval.metadata_filters import page_matches_filters
from retrieval.query_quality_calibration import load_calibration_artifact_once
from runtime.runtime_config import EmbeddingSettings, QualityGateSettings, TelemetrySettings
from retrieval.query_quality_policy import (
    GATE_FAIL_OPEN_ERROR,
    GATE_WOULD_SUPPRESS_ALL,
    QUALITY_POLICY_VERSION,
    QualityGateResult,
    build_candidate_features,
    evaluate_quality_gate,
    is_gate_reason_code,
    is_score_family,
)
from retrieval.vector_index import VectorIndexError, VectorIndexStore, vector_settings_from_embedding
from retrieval.vector_provider import LocalBgeM3Provider, VectorProviderError
from retrieval.query_execution_context import QueryExecutionContext
from retrieval.query_recall_policy import (
    DEFAULT_TOP_K,
    RANKING_POLICY_VERSION,
    RRF_K,
    adaptive_expand,
    classify_intent,
)


_QUALITY_GATE_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ENFORCED_RANKING_POLICY_VERSION = f"{RANKING_POLICY_VERSION}-quality-gate"


@dataclass(frozen=True)
class _QualityGateEvaluation:
    """The bounded public summary plus the private decisions for projection."""

    summary: dict[str, Any]
    result: QualityGateResult | None


def _quality_gate_branch(*, coverage_fallback: bool, fallback_level: str, lexical_mode: str) -> str | None:
    if coverage_fallback:
        return "coverage"
    if lexical_mode in {"relaxed", "wiki_relaxed", "active_relaxed"}:
        return "wiki_relaxed"
    if fallback_level == "raw" or lexical_mode.startswith("raw"):
        return "raw"
    return None


def _resolve_quality_gate_artifact_path(vault_root: Path, artifact_path: str | Path | None) -> Path | None:
    """Resolve the configured artifact at the runtime consumer boundary."""

    if artifact_path is None or not str(artifact_path).strip():
        return None
    configured = Path(artifact_path).expanduser()
    return configured if configured.is_absolute() else vault_root / configured


def _quality_gate_evaluation(
    candidates: tuple[Mapping[str, Any], ...],
    *,
    vault_root: Path,
    settings: QualityGateSettings | None,
    effective_scope: str,
    retrieval_mode: str,
    fallback_level: str,
    coverage_fallback: bool,
    lexical_mode: str,
) -> _QualityGateEvaluation | None:
    """Run the pure gate after outcome freeze and keep decisions private."""

    if settings is None or settings.mode == "off":
        return None
    if settings.mode not in {"shadow", "enforce"}:
        return None

    policy_version_value = settings.policy_version
    policy_version = (
        policy_version_value
        if isinstance(policy_version_value, str) and _QUALITY_GATE_SAFE_TOKEN.fullmatch(policy_version_value)
        else QUALITY_POLICY_VERSION
    )
    branch = _quality_gate_branch(
        coverage_fallback=coverage_fallback,
        fallback_level=fallback_level,
        lexical_mode=lexical_mode,
    )
    artifact_path = _resolve_quality_gate_artifact_path(vault_root, getattr(settings, "artifact_path", None))
    threshold_view = (
        load_calibration_artifact_once(artifact_path, expected_policy_version=policy_version)
        if artifact_path is not None
        else None
    )

    def fail_open() -> _QualityGateEvaluation:
        return _QualityGateEvaluation(
            {
                "policy_version": policy_version,
                "mode": settings.mode,
                "status": "gate_unavailable",
                "candidate_count": len(candidates),
                "accepted_count": len(candidates),
                "rejected_count": 0,
                "score_family_counts": {},
                "reason_counts": {GATE_FAIL_OPEN_ERROR: 1},
                "low_sample_buckets": [],
                "fail_open": True,
            },
            None,
        )

    try:
        features = build_candidate_features(
            candidates,
            effective_scope=effective_scope,
            retrieval_mode=retrieval_mode,
            branch=branch,
        )
        if threshold_view is None:
            result = evaluate_quality_gate(features, policy_version=policy_version)
        else:
            result = evaluate_quality_gate(
                features,
                policy_version=policy_version,
                threshold_view=threshold_view,
            )
        summary = result.summary
        score_family_counts = {
            str(key): int(value)
            for key, value in summary.score_family_counts.items()
            if is_score_family(key) and type(value) is int and value >= 0
        }
        reason_counts = {
            str(key): int(value)
            for key, value in summary.reason_counts.items()
            if is_gate_reason_code(key) and type(value) is int and value >= 0
        }
        low_sample_buckets = [
            value[:64]
            for value in summary.low_sample_buckets
            if isinstance(value, str) and _QUALITY_GATE_SAFE_TOKEN.fullmatch(value)
        ][:32]
        public_summary: dict[str, Any] = {
            "policy_version": policy_version,
            "mode": settings.mode,
            "status": (
                "gate_shadow"
                if settings.mode == "shadow" or summary.fail_open
                else "gate_enforced"
            ),
            "candidate_count": int(summary.candidate_count),
            "accepted_count": int(summary.accepted_count),
            "rejected_count": int(summary.rejected_count),
            "score_family_counts": score_family_counts,
            "reason_counts": reason_counts,
            "low_sample_buckets": low_sample_buckets,
            "fail_open": bool(summary.fail_open),
        }
        if summary.calibration_revision:
            public_summary["calibration_revision"] = summary.calibration_revision
        if summary.threshold_selection_counts:
            public_summary["selection_counts"] = {
                str(key): int(value)
                for key, value in summary.threshold_selection_counts.items()
                if key in {"exact", "backoff", "fail_open"}
                and type(value) is int
                and value >= 0
            }
        return _QualityGateEvaluation(public_summary, result)
    except Exception:
        return fail_open()


def _normalise_gate_path(value: object) -> str:
    return str(value or "").replace("\\", "/").casefold()


def _accepted_selected(
    selected: Sequence[Mapping[str, Any]],
    result: QualityGateResult,
) -> list[dict[str, Any]]:
    """Project decisions back onto the already page-deduplicated selection."""

    accepted_paths = {
        decision.feature.normalized_page_path
        for decision in result.accepted
    }
    return [
        dict(item)
        for item in selected
        if _normalise_gate_path(item["hit"].page_path) in accepted_paths
    ]


def _mark_gate_all_rejected(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Record an all-rejected observation while preserving the baseline."""

    result = dict(summary)
    reason_counts = dict(summary.get("reason_counts", {}))
    reason_counts[GATE_WOULD_SUPPRESS_ALL] = (
        int(reason_counts.get(GATE_WOULD_SUPPRESS_ALL, 0)) + 1
    )
    result["status"] = "gate_all_rejected"
    result["reason_counts"] = dict(sorted(reason_counts.items()))
    result["fail_open"] = True
    return result



def _effective_scope(scope: str, intent: str) -> tuple[str, tuple[str, ...]]:
    if scope not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        raise ValueError("scope must be auto, knowledge, history, all, archive, or raw")
    if scope == "auto":
        return ("history" if intent == "history" else "knowledge"), (("history_intent",) if intent == "history" else ())
    return scope, ()


def _citation_metadata(hit: PassageHit, provenance: dict[str, dict[str, str]]) -> dict[str, str]:
    """Expose the minimum traceability fields for low-authority chat evidence."""
    if hit.corpus != "history" and hit.source_kind not in {"raw_chat", "legacy_chatlog"}:
        return {}
    return {
        key: value
        for key, value in provenance.get(hit.page_path, {}).items()
        if key in {"session_id", "occurred_at", "project", "content_hash"} and value
    }


def _effective_rrf_k(embedding: EmbeddingSettings | None) -> int:
    """Resolve one query invocation's immutable RRF scale."""

    value = getattr(embedding, "rrf_k", RRF_K)
    return value if isinstance(value, int) and value > 0 else RRF_K


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
        if eligible(probe, metadata, scope=scope) and matches_request(probe, metadata, project=project, filters=filters):
            candidates.append((overlap, path))
    paths = [path for _overlap, path in sorted(candidates, key=lambda item: (-item[0], item[1]))[:limit]]
    return store.passages_for_pages(paths, limit_per_page=1)


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
        results = store.search(
            provider.embed_query(question, context=cancellation),
            allowed_paths=allowed_paths,
            limit=settings.candidate_limit,
        )
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
        if (
            not path.startswith("wiki/")
            or not eligible(probe, metadata, scope=scope)
            or not matches_request(probe, metadata, project=project, filters=filters)
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


def _thaw_value(value: Any) -> Any:
    """Copy an immutable outcome projection back into public JSON containers."""

    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw_value(item) for item in value}
    return value


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
    quality_gate: QualityGateSettings | None = None,
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
        if not eligible(
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
        if not eligible(hit, metadata, scope=effective_scope) or not matches_request(hit, metadata, project=project, filters=filters):
            continue
        item = ranked.setdefault(hit.passage_id, candidate_item(hit, score=0.0, fts_rank=rank))
        item["fts_rank"] = rank
    # A vector hit is identified by passage ID.  Load its existing retrieval
    # projection rather than scanning Markdown, so vector-only recall remains
    # available without adding query-time corpus reads.
    for index, hit in enumerate(store.load_passages(vector)):
        cancellation.checkpoint_batch(index, every=16, stage="vector")
        if not eligible(hit, metadata, scope=effective_scope) or not matches_request(hit, metadata, project=project, filters=filters):
            continue
        ranked.setdefault(hit.passage_id, candidate_item(hit, score=0.0))
    # A title-only match is deliberately not primary retrieval.  A generic
    # title overlap (such as "script") must not prevent a natural-language
    # question, in any language, from using relaxed lexical recovery.
    has_primary_recall = bool(ranked)
    expansion_suggestions: list[str] = []
    for rank, hit in enumerate(_title_candidates(store, metadata, question, scope=effective_scope, project=project, filters=filters, snapshot=snapshot, cancellation=cancellation), 1):
        cancellation.checkpoint_batch(rank - 1, every=16, stage="ranking")
        ranked.setdefault(hit.passage_id, candidate_item(hit, score=0.0, title_rank=rank))
        ranked[hit.passage_id]["title_rank"] = rank
    for index, item in enumerate(ranked.values()):
        cancellation.checkpoint_batch(index, every=16, stage="ranking")
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
        cancellation.checkpoint_batch(index, every=16, stage="ranking")
    selected = select_best_per_page(scored)
    selected = adaptive_expand(selected, top_k)
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
    cancellation.checkpoint("fallback")
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
    fallback_value = outcome.recovery.fallback
    fallback_level = str(fallback_value.get("level") or "none") if isinstance(fallback_value, Mapping) else "none"
    quality_gate_evaluation = _quality_gate_evaluation(
        outcome.selected,
        vault_root=root,
        settings=quality_gate,
        effective_scope=effective_scope,
        retrieval_mode=retrieval_mode,
        fallback_level=fallback_level,
        coverage_fallback=outcome.coverage_fallback,
        lexical_mode=outcome.lexical_mode,
    )
    baseline_selected = _thaw_value(outcome.selected)
    selected = baseline_selected
    enforce_projection_changed = False
    if (
        quality_gate_evaluation is not None
        and quality_gate is not None
        and quality_gate.mode == "enforce"
        and quality_gate_evaluation.result is not None
    ):
        gate_result = quality_gate_evaluation.result
        if not gate_result.summary.fail_open:
            accepted_selected = _accepted_selected(baseline_selected, gate_result)
            if baseline_selected and not accepted_selected:
                quality_gate_evaluation = _QualityGateEvaluation(
                    _mark_gate_all_rejected(quality_gate_evaluation.summary),
                    gate_result,
                )
            elif accepted_selected != baseline_selected:
                selected = accepted_selected
                enforce_projection_changed = True
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
            heading(item["hit"]),
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
    # Corpus/authority telemetry describes the frozen retrieval outcome, not
    # the later public admission projection.
    contains_raw = any(item["hit"].source_kind == "raw" for item in baseline_selected)
    # Recovery owns the final fallback envelope as well as the intermediate
    # context state.  Keep the public payload projection here, but do not
    # reconstruct level/reasons/path allowlists a second time.
    fallback_payload = _thaw_value(recovery.fallback)
    if enforce_projection_changed and isinstance(fallback_payload, dict):
        accepted_raw_paths = {
            _normalise_gate_path(item["hit"].page_path)
            for item in selected
            if item["hit"].source_kind == "raw"
        }
        allowed_source_paths = fallback_payload.get("allowed_source_paths")
        if isinstance(allowed_source_paths, list):
            fallback_payload = {
                **fallback_payload,
                "allowed_source_paths": [
                    path
                    for path in allowed_source_paths
                    if _normalise_gate_path(path) in accepted_raw_paths
                ],
            }
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
            "heading": heading(hit),
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
        "ranking_version": (
            ENFORCED_RANKING_POLICY_VERSION
            if enforce_projection_changed
            else RANKING_POLICY_VERSION
        ),
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
            "graph_hits": sum(1 for item in baseline_selected if item["graph_score"] > 0),
            "selected": len(baseline_selected),
            "returned": len(results),
            "additional": len(additional_results),
        },
        "warnings": warnings,
        "fallback": fallback_payload,
    }
    if quality_gate_evaluation is not None:
        pipeline["quality_gate"] = quality_gate_evaluation.summary
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
            for item in baseline_selected
        ]
    cancellation.checkpoint("telemetry")
    elapsed = (time.perf_counter() - started) * 1_000
    if telemetry is None or telemetry.enabled:
        (telemetry_recorder or QueryTelemetry(root)).finish_once(
            question=question,
            scope=scope,
            project=project,
            passage_ids=[item["hit"].passage_id for item in baseline_selected],
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

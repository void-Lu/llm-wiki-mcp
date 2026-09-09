"""Pure bounded entity-batch owner for Query V2.

The owner receives the active/raw snapshots captured by the invocation and
uses the supplied read-only retrieval adapters for per-entity search.  It
does not own stores, cancellation state, workers, or query execution state.
Its public result is frozen before it returns to the context.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Literal

from retrieval.lexical_analyzer import QualifiedIdentifier, tokens
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_recovery import LadderStep, search_ladder
from retrieval.query_shared import QueryFilters, eligible, heading, matches_request
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore


RawAvailability = Literal["fresh", "stale", "missing"]
BATCH_MAX_ITEMS = 40
BATCH_CANDIDATE_POOL_LIMIT = 80
BATCH_MIN_RELEVANCE_SCORE = 0.05
BATCH_HIGH_SCORE_RATIO = 0.82
BATCH_AMBIGUITY_RATIO = 0.93
BATCH_MAX_SELECTED_CANDIDATES = 12


@dataclass(frozen=True)
class AdaptiveCandidateScorePolicy:
    """Local score policy used only by the entity batch fan-out."""

    candidate_pool_limit: int = BATCH_CANDIDATE_POOL_LIMIT
    minimum_relevance_score: float = BATCH_MIN_RELEVANCE_SCORE
    high_score_ratio: float = BATCH_HIGH_SCORE_RATIO
    ambiguity_ratio: float = BATCH_AMBIGUITY_RATIO
    max_selected_candidates: int = BATCH_MAX_SELECTED_CANDIDATES


@dataclass(frozen=True)
class EntityStoreSpec:
    """One entity-batch store paired with its invocation snapshot."""

    store: RetrievalIndexStore
    scope: str
    snapshot: QueryCorpusSnapshot


@dataclass(frozen=True)
class EntityBatchResult:
    """Frozen result object returned by :func:`run_entity_batch`."""

    payload: Mapping[str, Any]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def build_store_specs(
    primary_store: RetrievalIndexStore,
    effective_scope: str,
    *,
    snapshot: QueryCorpusSnapshot,
    raw_snapshot: QueryCorpusSnapshot | None,
    raw_store: RetrievalIndexStore | None,
    raw_availability: RawAvailability = "missing",
) -> tuple[EntityStoreSpec, ...]:
    """Pair each read-only store with the snapshot captured for that store."""

    specs = [EntityStoreSpec(primary_store, effective_scope, snapshot)]
    if effective_scope in {"knowledge", "all"} and raw_availability == "fresh":
        if raw_store is None:
            return tuple(specs)
        if raw_snapshot is None:
            raise RuntimeError("raw entity store requires the invocation snapshot")
        specs.append(EntityStoreSpec(raw_store, "raw", raw_snapshot))
    return tuple(specs)


def _entity_query_text(entity: Mapping[str, Any], base_question: str, all_entities: Sequence[Mapping[str, Any]]) -> str:
    identifier = entity.get("_identifier")
    canonical = identifier.canonical_id if isinstance(identifier, QualifiedIdentifier) else str(entity["canonical_id"])
    excluded: set[str] = set()
    for item in all_entities:
        excluded.update(tokens(" ".join(str(alias) for alias in item.get("aliases", ()))))
    remaining = [term for term in tokens(base_question) if term not in excluded]
    return " ".join([canonical, *remaining])


def _alias_pattern(alias: str) -> str:
    pieces = [piece for piece in re.split(r"[/\s_-]+", alias.casefold()) if piece]
    if not pieces:
        return ""
    return r"(?<![a-z0-9_])" + r"[/\s_-]+".join(re.escape(piece) for piece in pieces) + r"(?![a-z0-9_])"


def _entity_match_signals(entity: Mapping[str, Any], hit: PassageHit) -> tuple[float, bool, dict[str, bool]]:
    aliases = [str(alias) for alias in entity.get("aliases", ())]
    identifier = entity.get("_identifier")
    if isinstance(identifier, QualifiedIdentifier):
        aliases = [*aliases, identifier.canonical_id, "".join((*identifier.prefix_segments, *identifier.name_segments))]
    title = hit.title.casefold()
    heading_text = " ".join(hit.heading_path).casefold()
    path = hit.page_path.casefold()
    body = hit.text.casefold()
    title_match = any(alias.casefold() in title or re.search(_alias_pattern(alias), title) for alias in aliases if alias)
    heading_match = any(alias.casefold() in heading_text or re.search(_alias_pattern(alias), heading_text) for alias in aliases if alias)
    path_match = any(alias.casefold() in path or re.search(_alias_pattern(alias), path) for alias in aliases if alias)
    body_match = any(alias.casefold() in body or re.search(_alias_pattern(alias), body) for alias in aliases if alias)
    exact_match = title_match or heading_match or path_match or body_match
    score = max(float(hit.score), 0.0)
    score += 3.0 if title_match else 0.0
    score += 2.0 if heading_match else 0.0
    score += 1.5 if path_match else 0.0
    score += 1.0 if body_match else 0.0
    return round(score, 12), exact_match, {"title": title_match, "heading": heading_match, "path": path_match, "body": body_match}


def _batch_candidate_items(entity: Mapping[str, Any], hits: Sequence[PassageHit]) -> list[dict[str, Any]]:
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


def select_candidates(
    items: Sequence[dict[str, Any]],
    policy: AdaptiveCandidateScorePolicy | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Keep a score platform and stop at the first meaningful drop."""

    policy = policy or AdaptiveCandidateScorePolicy()
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


def _search_entity_store(
    store: RetrievalIndexStore,
    entity: Mapping[str, Any],
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
    entity: Mapping[str, Any],
    all_entities: Sequence[Mapping[str, Any]],
    base_question: str,
    *,
    store_specs: Sequence[EntityStoreSpec],
    project: str | None,
    filters: QueryFilters,
    policy: AdaptiveCandidateScorePolicy,
) -> tuple[dict[str, Any], dict[str, int]]:
    query_text = _entity_query_text(entity, base_question, all_entities)
    counters = {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0}
    try:
        for spec in store_specs:
            hits, local_counters = _search_entity_store(
                spec.store,
                entity,
                query_text,
                project=project,
                filters=filters,
                limit=policy.candidate_pool_limit,
            )
            for key, value in local_counters.items():
                counters[key] += value
            if spec.scope == "raw":
                counters["raw_hits"] += len(hits)
            eligible_hits = [
                hit
                for hit in hits
                if eligible(hit, spec.snapshot.metadata, scope=spec.scope)
                and matches_request(hit, spec.snapshot.metadata, project=project, filters=filters)
            ]
            if not eligible_hits:
                continue
            candidates = _batch_candidate_items(entity, eligible_hits)
            selected, selection_error = select_candidates(candidates, policy)
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
            return {
                "entity": entity["canonical_id"],
                "aliases": list(entity.get("aliases", ())),
                "status": "ambiguous" if needs_review else "ok",
                "primary": _batch_public_candidate(selected[0], include_context=True),
                "alternatives": [_batch_public_candidate(item, include_context=False) for item in selected[1:]],
                "needs_review": needs_review,
                "shared_source": False,
                "selection_confidence": round(confidence if not needs_review else min(confidence, 0.55), 4),
                "evidence": entity["evidence"],
            }, counters
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


def _batch_public_candidate(item: Mapping[str, Any], *, include_context: bool) -> dict[str, Any]:
    hit = item["hit"]
    evidence = {
        "path": hit.page_path,
        "passage_id": hit.passage_id,
        "heading": heading(hit),
        "excerpt": hit.text[:360],
    }
    candidate = {
        "path": hit.page_path,
        "heading": heading(hit),
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
    entities: Sequence[Mapping[str, Any]],
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


def run_entity_batch(
    entities: Sequence[Mapping[str, Any]],
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
) -> EntityBatchResult:
    """Run one bounded entity batch against invocation-owned snapshots."""

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
        return EntityBatchResult(_freeze({
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
        }))
    if len(entities) > BATCH_MAX_ITEMS and offset is None:
        return EntityBatchResult(_freeze({
            "status": "confirmation_required",
            "entity_count": len(entities),
            "max_batch_items": BATCH_MAX_ITEMS,
            "pending_entities": [entity["canonical_id"] for entity in entities],
            "confirmation_token": _batch_token(fingerprint, 0),
            "entities": [],
            "failed_entities": [],
            "unresolved": [],
            "ambiguous": [],
            "counters": {"fts_hits": 0, "qualified_hits": 0, "relaxed_hits": 0, "raw_hits": 0, "queries": 0},
        }))
    if offset is None:
        offset = 0
    if offset >= len(entities) or offset % BATCH_MAX_ITEMS != 0:
        return EntityBatchResult(_freeze({
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
        }))

    current_entities = list(entities[offset : offset + BATCH_MAX_ITEMS])
    specs = build_store_specs(
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
    return EntityBatchResult(_freeze({
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
    }))


__all__ = [
    "AdaptiveCandidateScorePolicy",
    "BATCH_MAX_ITEMS",
    "EntityBatchResult",
    "EntityStoreSpec",
    "build_store_specs",
    "run_entity_batch",
    "select_candidates",
]

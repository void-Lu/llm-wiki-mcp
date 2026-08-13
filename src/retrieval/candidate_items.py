"""Factories for the canonical Query V2 candidate-item mapping."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from retrieval.retrieval_index import PassageHit


CANDIDATE_CORE_KEYS: frozenset[str] = frozenset(
    {
        "hit",
        "score",
        "fts_rank",
        "title_rank",
        "vector_rank",
        "vector_score",
        "rrf",
        "exact",
        "graph_score",
        "graph_reasons",
    }
)
"""The ten keys guaranteed on every candidate item returned by ``candidate_item``."""

FUSION_KEYS: frozenset[str] = frozenset(
    {
        "coverage_terms",
        "coverage_ratio",
        "source_local_rank",
        "source_local_rrf",
        "fusion_score",
        "fusion_source",
        "fusion_local_position",
    }
)
"""The seven keys added by ``with_fusion`` to a candidate item."""


def candidate_item(
    hit: PassageHit,
    *,
    score: float,
    fts_rank: int | None = None,
    title_rank: int | None = None,
    vector_rank: int | None = None,
    vector_score: float = 0.0,
    rrf: float = 0.0,
    exact: bool = False,
    graph_score: float = 0.0,
    graph_reasons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical ten-key candidate view.

    The core keys are ``hit`` (the :class:`PassageHit` evidence), ``score``
    (the current candidate score), ``fts_rank``/``title_rank``/``vector_rank``
    (source-local ranks), ``vector_score`` (the vector similarity), ``rrf``
    (the reciprocal-rank contribution), ``exact`` (the main-path exact-match
    flag), ``graph_score`` (the graph contribution), and ``graph_reasons``
    (the graph debug evidence). ``score`` may represent the main-path RRF
    fusion score, relaxed BM25 plus bonuses, raw BM25 plus raw bonuses,
    coverage fusion, discovery BM25, or an entity signal score; entity batch
    items are a separate shape and are not owned by this factory.

    ``exact`` is only meaningful as ``True`` on the main query path. The
    entity-batch shape intentionally uses ``exact_match`` instead; that
    naming split is deliberate and keeps the independent entity contract out
    of the candidate-item core shape.
    """

    return {
        "hit": hit,
        "score": score,
        "fts_rank": fts_rank,
        "title_rank": title_rank,
        "vector_rank": vector_rank,
        "vector_score": vector_score,
        "rrf": rrf,
        "exact": exact,
        "graph_score": graph_score,
        "graph_reasons": list(graph_reasons) if graph_reasons is not None else [],
    }


def with_fusion(
    item: Mapping[str, Any],
    *,
    coverage_terms: list[str],
    coverage_ratio: float,
    source_local_rank: int,
    source_local_rrf: float,
    fusion_score: float,
    fusion_source: str,
    fusion_local_position: int,
) -> dict[str, Any]:
    """Return ``item`` decorated with the seven coverage-fusion keys."""

    return {
        **item,
        "coverage_terms": coverage_terms,
        "coverage_ratio": coverage_ratio,
        "source_local_rank": source_local_rank,
        "source_local_rrf": source_local_rrf,
        "fusion_score": fusion_score,
        "fusion_source": fusion_source,
        "fusion_local_position": fusion_local_position,
    }

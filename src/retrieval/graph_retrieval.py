"""Bounded graph expansion shared by Query V2 retrieval stages.

The graph operates on already indexed candidate projections and the link
edges persisted at index time (see ``retrieval.graph_edges``).  It does not
read Wiki source files during a query and it never widens the candidate set
past the caller-provided eligibility boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from retrieval.graph_edges import WIKILINK, PageLink, extract_page_links, frontmatter_values, resolve_wikilink

# Evidence weights for typed frontmatter relations, alongside the existing
# direct wikilink (3.0), shared source (4.0 each), common neighbour
# (1.5 / ln(degree + 1)) and same type (1.0) weights.  ``derived_from`` is an
# explicit, validated origin declaration and counts like a body wikilink;
# ``related_objects`` is a looser association.  ``applies_to`` holds
# applicability labels rather than page targets and is not scored.
TYPED_RELATION_WEIGHTS = {"derived_from": 3.0, "related_objects": 2.0}
_GRAPH_SCORE_RATIO_CAP = 0.15
_PURE_GRAPH_SCORE_CAP = 0.75
# Raw evidence at which a graph-only page reaches half of the pure-graph cap.
# One direct wikilink from one seed (weight 3.0) lands at 0.375; stronger or
# repeated evidence approaches, but never reaches, 0.75.
_PURE_GRAPH_HALF_SATURATION = 3.0
_MAX_GRAPH_EXPANSIONS_PER_SEED = 64
_SCORE_PRECISION = 12


@dataclass
class RankBreakdown:
    graph_reasons: list[dict[str, Any]] = field(default_factory=list)
    lexical_rank: int | None = None
    vector_rank: int | None = None
    rrf_contribution: float = 0.0


@dataclass(frozen=True)
class RelationshipEvidence:
    reasons: tuple[dict[str, Any], ...]
    score: float


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
    graph_evidence: float = 0.0
    rank_breakdown: RankBreakdown = field(default_factory=RankBreakdown)

    @property
    def total_score(self) -> float:
        return _stable_score(self.fusion_score + self.graph_score)


@dataclass(frozen=True)
class Graph:
    neighbors: dict[str, set[str]]
    sources: dict[str, set[str]]
    types: dict[str, str]
    # Symmetric typed frontmatter relations: page -> related page -> kinds.
    typed: dict[str, dict[str, set[str]]] = field(default_factory=dict)

    def adjacent(self, rel: str) -> set[str]:
        """Pages one hop away through a wikilink or a typed relation."""

        related = self.typed.get(rel)
        if not related:
            return self.neighbors.get(rel, set())
        return self.neighbors.get(rel, set()) | set(related)


def build_graph(
    root: Path,
    candidates: list[QueryCandidate] | None = None,
    *,
    edges: Mapping[str, Sequence[PageLink]] | None = None,
) -> Graph:
    """Build graph relationships over the caller's candidate boundary.

    ``edges`` are the persisted index-time links keyed by page path.  Pages
    missing from ``edges`` (or every page, when ``edges`` is omitted) are
    parsed from their candidate body with the same extractor the index uses.
    Wikilink targets are resolved against the current candidates only, so a
    filtered-out page can never become a graph neighbour.
    """

    if candidates is None:
        candidates = []
    wiki_candidates = [candidate for candidate in candidates if candidate.rel.startswith("wiki/")]
    by_rel = {candidate.rel: candidate for candidate in wiki_candidates}
    by_stem: dict[str, list[str]] = {}
    for rel, candidate in by_rel.items():
        by_stem.setdefault(candidate.path.stem.casefold(), []).append(rel)

    neighbors = {rel: set() for rel in by_rel}
    typed: dict[str, dict[str, set[str]]] = {}
    sources: dict[str, set[str]] = {}
    types: dict[str, str] = {}
    for rel, candidate in by_rel.items():
        # Snapshot metadata freezes YAML lists into tuples; normalise both.
        sources[rel] = {str(item) for item in frontmatter_values(candidate.frontmatter.get("sources"))}
        types[rel] = str(candidate.frontmatter.get("type") or _path_type(rel))
        page_links = edges.get(rel) if edges is not None else None
        if page_links is None:
            page_links = extract_page_links(rel, candidate.body, candidate.frontmatter, root=root)
        for link in page_links:
            if link.kind == WIKILINK:
                target = resolve_wikilink(link, by_rel, by_stem)
                if target:
                    neighbors[rel].add(target)
                    neighbors.setdefault(target, set()).add(rel)
            elif link.kind in TYPED_RELATION_WEIGHTS:
                target = resolve_wikilink(link, by_rel, by_stem)
                if target and target != rel:
                    typed.setdefault(rel, {}).setdefault(target, set()).add(link.kind)
                    typed.setdefault(target, {}).setdefault(rel, set()).add(link.kind)
    return Graph(neighbors=neighbors, sources=sources, types=types, typed=typed)


def apply_graph_expansion(
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
                for rel in sorted(graph.adjacent(via) - visited):
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
                evidence = relationship_evidence_for(seed, rel, graph)
                relationship_reasons = list(evidence.reasons) if collect_reasons else []
                relationship_score = evidence.score
                raw_contribution = relationship_score * decay
                graph_cap = _graph_score_cap(candidate)
                if _has_relevance_signal(candidate):
                    applied = max(0.0, min(raw_contribution, graph_cap - candidate.graph_score))
                else:
                    # Graph-only pages accumulate uncapped evidence and map it
                    # monotonically into [0, cap), so stronger evidence keeps
                    # ranking above weaker evidence instead of every page
                    # saturating at the cap and falling back to path order.
                    candidate.graph_evidence += raw_contribution
                    applied = max(0.0, _pure_graph_score(candidate.graph_evidence) - candidate.graph_score)
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


def relationship_evidence_for(left: str, right: str, graph: Graph) -> RelationshipEvidence:
    reasons: list[dict[str, Any]] = []
    if right in graph.neighbors.get(left, set()):
        reasons.append({"kind": "direct_wikilink", "source": left, "target": right, "score": 3.0})
    for kind in sorted(graph.typed.get(left, {}).get(right, set())):
        reasons.append({"kind": "typed_relation", "source": left, "target": right, "value": kind, "score": TYPED_RELATION_WEIGHTS[kind]})
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
    return RelationshipEvidence(tuple(reasons), _stable_score(sum(float(reason["score"]) for reason in reasons)))


def relationship_reasons_for(left: str, right: str, graph: Graph) -> list[dict[str, Any]]:
    """Compatibility projection of the shared relationship evidence owner."""

    return [dict(reason) for reason in relationship_evidence_for(left, right, graph).reasons]


def relationship_score_for(left: str, right: str, graph: Graph) -> float:
    """Compatibility projection of the shared relationship evidence owner."""

    return relationship_evidence_for(left, right, graph).score


def _has_relevance_signal(candidate: QueryCandidate) -> bool:
    return max(candidate.fusion_score, candidate.keyword_score, candidate.vector_score) > 0


def _pure_graph_score(evidence: float) -> float:
    if evidence <= 0:
        return 0.0
    return _stable_score(_PURE_GRAPH_SCORE_CAP * evidence / (evidence + _PURE_GRAPH_HALF_SATURATION))


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


def _path_type(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "wiki":
        if parts[1] == "projects" and len(parts) >= 4:
            return parts[3]
        return parts[1]
    return "page"


def _stable_score(score: float) -> float:
    return round(score, _SCORE_PRECISION)

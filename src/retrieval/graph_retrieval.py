"""Bounded graph expansion shared by Query V2 retrieval stages.

The graph operates on already indexed candidate projections.  It does not
read Wiki source files during a query and it never widens the candidate set
past the caller-provided eligibility boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wiki.wikilinks import wikilink_targets

_GRAPH_SCORE_RATIO_CAP = 0.15
_PURE_GRAPH_SCORE_CAP = 0.75
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
    rank_breakdown: RankBreakdown = field(default_factory=RankBreakdown)

    @property
    def total_score(self) -> float:
        return _stable_score(self.fusion_score + self.graph_score)


@dataclass(frozen=True)
class Graph:
    neighbors: dict[str, set[str]]
    sources: dict[str, set[str]]
    types: dict[str, str]


def build_graph(root: Path, candidates: list[QueryCandidate] | None = None) -> Graph:
    """Build graph relationships from indexed candidate projections."""

    if candidates is None:
        candidates = []
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
                evidence = relationship_evidence_for(seed, rel, graph)
                relationship_reasons = list(evidence.reasons) if collect_reasons else []
                relationship_score = evidence.score
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


def relationship_evidence_for(left: str, right: str, graph: Graph) -> RelationshipEvidence:
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
    return RelationshipEvidence(tuple(reasons), _stable_score(sum(float(reason["score"]) for reason in reasons)))


def relationship_reasons_for(left: str, right: str, graph: Graph) -> list[dict[str, Any]]:
    """Compatibility projection of the shared relationship evidence owner."""

    return [dict(reason) for reason in relationship_evidence_for(left, right, graph).reasons]


def relationship_score_for(left: str, right: str, graph: Graph) -> float:
    """Compatibility projection of the shared relationship evidence owner."""

    return relationship_evidence_for(left, right, graph).score


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


def _stable_score(score: float) -> float:
    return round(score, _SCORE_PRECISION)

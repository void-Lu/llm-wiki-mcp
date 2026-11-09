"""SAG2-style multi-hop lexical expansion (Algorithm 1, local judge)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from retrieval.retrieval_index import PassageHit
from retrieval.sag2_evidence import EvidenceJudge
from retrieval.sag2_terms import TermSelector


@dataclass(frozen=True)
class Sag2HopConfig:
    initial_pool_size: int = 100
    seeds_per_hop: int = 10
    terms_per_seed: int = 20
    max_hops: int = 2
    max_judge_calls: int = 400
    expand_search_limit: int = 20


@dataclass
class Sag2HopResult:
    candidates: list[PassageHit]
    evidence_scores: dict[str, float] = field(default_factory=dict)
    hops_run: int = 0
    judge_calls: int = 0
    expansion_terms: tuple[str, ...] = ()
    truncated: bool = False
    stop_reason: str = "completed"


class LexicalSearcher(Protocol):
    def search(self, query: str, *, limit: int = 20) -> list[PassageHit]:
        """Return FTS hits for one expansion query."""


class Sag2HopController:
    def __init__(
        self,
        judge: EvidenceJudge,
        term_selector: TermSelector,
        searcher: LexicalSearcher,
        config: Sag2HopConfig | None = None,
    ) -> None:
        self._judge = judge
        self._terms = term_selector
        self._searcher = searcher
        self._config = config or Sag2HopConfig()

    def run(self, question: str, initial_hits: Sequence[PassageHit]) -> Sag2HopResult:
        cfg = self._config
        pool: dict[str, PassageHit] = {}
        for hit in list(initial_hits)[: cfg.initial_pool_size]:
            pool[hit.passage_id] = hit
        evidence: dict[str, float] = {}
        expanded_seeds: set[str] = set()
        used_terms: list[str] = []
        judge_calls = 0
        hops_run = 0
        stop_reason = "completed"
        truncated = False

        def ensure_scored(hit: PassageHit, context: Sequence[str]) -> float:
            nonlocal judge_calls, truncated, stop_reason
            if hit.passage_id in evidence:
                return evidence[hit.passage_id]
            if judge_calls >= cfg.max_judge_calls:
                truncated = True
                stop_reason = "judge_budget"
                evidence[hit.passage_id] = 0.0
                return 0.0
            try:
                score = float(
                    self._judge.score(
                        question,
                        hit.text,
                        context=context,
                        title=hit.title,
                        page_path=hit.page_path,
                    )
                )
            except Exception:
                score = 0.0
            judge_calls += 1
            evidence[hit.passage_id] = max(0.0, min(1.0, score))
            return evidence[hit.passage_id]

        context_texts = [h.text for h in pool.values()]
        for hit in list(pool.values()):
            ensure_scored(hit, context_texts)
            if truncated:
                break

        for _hop in range(cfg.max_hops):
            if truncated:
                break
            hops_run += 1
            ranked_seeds = sorted(
                (
                    hit
                    for pid, hit in pool.items()
                    if pid not in expanded_seeds
                ),
                key=lambda h: (-evidence.get(h.passage_id, 0.0), h.passage_id),
            )[: cfg.seeds_per_hop]
            if not ranked_seeds:
                stop_reason = "no_new_candidates"
                break
            new_hits: list[PassageHit] = []
            hop_terms: list[str] = []
            for seed in ranked_seeds:
                expanded_seeds.add(seed.passage_id)
                selected = self._terms.select(
                    seed.text,
                    question=question,
                    evidence_score=evidence.get(seed.passage_id, 0.0),
                    limit=cfg.terms_per_seed,
                )
                for term in selected:
                    if term.term in used_terms:
                        continue
                    hop_terms.append(term.term)
                    used_terms.append(term.term)
                    for hit in self._searcher.search(term.term, limit=cfg.expand_search_limit):
                        if hit.passage_id not in pool:
                            new_hits.append(hit)
            if not hop_terms:
                stop_reason = "no_expansion_terms"
                # Normalize: if first hop finds no terms, hops_run stays 0 conceptually.
                if not used_terms:
                    hops_run = 0
                break
            if not new_hits:
                stop_reason = "no_new_candidates"
                break
            # Interleave by discovery order; dedupe by passage_id.
            for hit in new_hits:
                pool.setdefault(hit.passage_id, hit)
            context_texts = [h.text for h in pool.values()]
            for hit in new_hits:
                ensure_scored(hit, context_texts)
                if truncated:
                    break
        else:
            stop_reason = "max_hops"

        ordered = sorted(
            pool.values(),
            key=lambda h: (-evidence.get(h.passage_id, 0.0), -h.score, h.passage_id),
        )
        return Sag2HopResult(
            candidates=ordered,
            evidence_scores=evidence,
            hops_run=hops_run,
            judge_calls=judge_calls,
            expansion_terms=tuple(dict.fromkeys(used_terms)),
            truncated=truncated,
            stop_reason=stop_reason,
        )

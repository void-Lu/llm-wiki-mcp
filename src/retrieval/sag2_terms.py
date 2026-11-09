"""Term / phrase selection for SAG2 hop expansion."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]+", re.I)
_QUESTION_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "with", "does", "do", "did", "吗", "呢", "的",
        "了", "是", "什么", "怎么", "如何",
    }
)


@dataclass(frozen=True)
class ExpansionTerm:
    term: str
    weight: float


def _df_for_term(term: str, df_map: Mapping[str, int]) -> int:
    """Resolve document frequency; multiword uses min component df when absent."""
    if term in df_map:
        return int(df_map[term])
    if " " not in term:
        return 0
    part_dfs = [int(df_map.get(part, 0)) for part in term.split()]
    if not part_dfs or any(df <= 0 for df in part_dfs):
        return 0
    return min(part_dfs)


class TermSelector:
    def select(
        self,
        seed_text: str,
        *,
        question: str,
        evidence_score: float,
        document_frequency: Mapping[str, int] | None = None,
        corpus_size: int = 1,
        limit: int = 20,
    ) -> list[ExpansionTerm]:
        df_map = document_frequency or {}
        q_tokens = {
            t.casefold()
            for t in _TOKEN.findall(question)
            if t.casefold() not in _QUESTION_STOP
        }
        ordered = [
            t.casefold()
            for t in _TOKEN.findall(seed_text)
            if t.casefold() not in _QUESTION_STOP and len(t) > 1
        ]
        counts: Counter[str] = Counter(ordered)
        for i in range(len(ordered) - 1):
            phrase = f"{ordered[i]} {ordered[i + 1]}"
            counts[phrase] += 1
        scored: list[ExpansionTerm] = []
        n = max(int(corpus_size), 1)
        evidence = max(0.0, min(1.0, float(evidence_score)))
        for term, freq in counts.items():
            if term in q_tokens and " " not in term:
                # Prefer novel expansion terms; allow multiword even if overlapping.
                continue
            if document_frequency is not None:
                df = _df_for_term(term, df_map)
                if df <= 0:
                    continue
            else:
                df = 1
            rarity = math.log(1.0 + (n / max(df, 1)))
            link = 1.0 + 0.25 * len(set(term.split()) & q_tokens)
            weight = float(freq) * rarity * link * (0.2 + 0.8 * evidence)
            scored.append(ExpansionTerm(term=term, weight=weight))
        scored.sort(key=lambda item: (-item.weight, item.term))
        return scored[: max(0, int(limit))]

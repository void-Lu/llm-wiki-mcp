"""Local-only evidence judges for SAG2 hop (no hosted JEV, no vault egress)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]+", re.I)
_QUESTION_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "with", "吗", "呢", "的", "了", "是", "什么",
        "怎么", "如何",
    }
)


class EvidenceJudge(Protocol):
    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
    ) -> float:
        """Return evidence score in ``[0, 1]`` for ``passage_text`` given ``question``."""


def _tokens(text: str) -> set[str]:
    return {
        tok.casefold()
        for tok in _TOKEN.findall(text)
        if tok.casefold() not in _QUESTION_STOP and len(tok) > 1
    }


class HeuristicEvidenceJudge:
    """Coverage / phrase / context overlap heuristic (default local judge)."""

    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
    ) -> float:
        q = _tokens(question)
        if not q:
            return 0.0
        p = _tokens(passage_text)
        covered = len(q & p) / len(q)
        phrase_hit = 0.0
        lowered_q = " ".join(sorted(q))
        lowered_p = passage_text.casefold()
        # Cheap multi-token presence: reward if ≥2 query tokens appear as a window-ish join.
        if len(q) >= 2 and sum(1 for t in q if t in lowered_p) >= 2:
            phrase_hit = 0.15
        ctx_bonus = 0.0
        if context:
            ctx_toks: set[str] = set()
            for block in context:
                ctx_toks |= _tokens(block)
            if ctx_toks:
                ctx_bonus = 0.1 * (len(q & ctx_toks) / len(q))
        # Ignore unused lowered_q lint-friendly keep for future weighting hooks.
        _ = lowered_q
        return max(0.0, min(1.0, covered + phrase_hit + ctx_bonus))


ScorerFn = Callable[[str, str, Sequence[str]], float]


class LocalLlmEvidenceJudge:
    """Optional local scorer with mandatory heuristic fallback; never HTTP."""

    def __init__(
        self,
        *,
        fallback: EvidenceJudge,
        scorer: ScorerFn | None = None,
    ) -> None:
        self._fallback = fallback
        self._scorer = scorer

    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
    ) -> float:
        if self._scorer is None:
            return self._fallback.score(question, passage_text, context=context)
        try:
            value = float(self._scorer(question, passage_text, context))
        except Exception:
            return self._fallback.score(question, passage_text, context=context)
        if value != value:  # NaN
            return 0.0
        return max(0.0, min(1.0, value))

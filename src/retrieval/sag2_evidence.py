"""Local-only evidence judges for SAG2 hop (no hosted JEV, no vault egress)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]+", re.I)
_CJK_SPAN = re.compile(r"[一-鿿]{2,}")
_CAMEL = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_IDENT_LOOSE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b|\b[A-Za-z]+[0-9][A-Za-z0-9_]*\b")
_QUESTION_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "with", "吗", "呢", "的", "了", "是", "什么",
        "怎么", "如何",
    }
)

_TITLE_BONUS_WEIGHT = 0.25
_CJK_BONUS_PER_HIT = 0.10
_CJK_BONUS_CAP = 0.20
_IDENTIFIER_BONUS = 0.15


class EvidenceJudge(Protocol):
    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
        title: str = "",
        page_path: str = "",
    ) -> float:
        """Return evidence score in ``[0, 1]`` for ``passage_text`` given ``question``."""


def _tokens(text: str) -> set[str]:
    return {
        tok.casefold()
        for tok in _TOKEN.findall(text)
        if tok.casefold() not in _QUESTION_STOP and len(tok) > 1
    }


def _path_tokens(page_path: str) -> set[str]:
    stem = page_path.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    pieces = re.split(r"[/_\\-]+", page_path)
    pieces.append(stem)
    out: set[str] = set()
    for piece in pieces:
        out |= _tokens(piece)
    return out


def _identifier_terms(question: str) -> set[str]:
    found = {m.group(0) for m in _CAMEL.finditer(question)}
    found |= {m.group(0) for m in _IDENT_LOOSE.finditer(question)}
    return {term for term in found if len(term) > 1}


class HeuristicEvidenceJudge:
    """Coverage / phrase / context overlap heuristic (default local judge)."""

    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
        title: str = "",
        page_path: str = "",
    ) -> float:
        q = _tokens(question)
        if not q and not _CJK_SPAN.search(question) and not _identifier_terms(question):
            return 0.0
        p = _tokens(passage_text)
        covered = (len(q & p) / len(q)) if q else 0.0
        phrase_hit = 0.0
        lowered_p = passage_text.casefold()
        if len(q) >= 2 and sum(1 for t in q if t in lowered_p) >= 2:
            phrase_hit = 0.15
        ctx_bonus = 0.0
        if context:
            ctx_toks: set[str] = set()
            for block in context:
                ctx_toks |= _tokens(block)
            if ctx_toks and q:
                ctx_bonus = 0.1 * (len(q & ctx_toks) / len(q))

        title_toks = _tokens(title) | _path_tokens(page_path)
        title_bonus = 0.0
        if q and title_toks:
            title_bonus = _TITLE_BONUS_WEIGHT * (len(q & title_toks) / len(q))

        haystack = f"{title}\n{page_path}\n{passage_text}"
        cjk_hits = 0
        for span in dict.fromkeys(_CJK_SPAN.findall(question)):
            if span in haystack:
                cjk_hits += 1
        cjk_bonus = min(_CJK_BONUS_CAP, _CJK_BONUS_PER_HIT * cjk_hits)

        id_bonus = 0.0
        id_terms = _identifier_terms(question)
        if id_terms:
            lowered_hay = haystack.casefold()
            if any(term.casefold() in lowered_hay for term in id_terms):
                id_bonus = _IDENTIFIER_BONUS

        return max(0.0, min(1.0, covered + phrase_hit + ctx_bonus + title_bonus + cjk_bonus + id_bonus))


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
        title: str = "",
        page_path: str = "",
    ) -> float:
        if self._scorer is None:
            return self._fallback.score(
                question, passage_text, context=context, title=title, page_path=page_path
            )
        try:
            value = float(self._scorer(question, passage_text, context))
        except Exception:
            return self._fallback.score(
                question, passage_text, context=context, title=title, page_path=page_path
            )
        if value != value:  # NaN
            return 0.0
        return max(0.0, min(1.0, value))

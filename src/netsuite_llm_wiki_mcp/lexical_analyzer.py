"""Deterministic lexical normalization shared by FTS and retrieval callers."""

from __future__ import annotations

import re
from collections.abc import Iterable

_LATIN_OR_CODE = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_STOPWORDS = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "of", "on", "or", "the", "to", "with"})


def tokens(text: str) -> list[str]:
    """Return stable English/code tokens and CJK unigrams/bigrams.

    SQLite's default tokenizer does not provide a portable Chinese analyser.
    Pre-tokenising keeps FTS behaviour stable across all supported platforms.
    """

    lowered = text.casefold()
    latin = [value for value in _LATIN_OR_CODE.findall(lowered) if value not in _STOPWORDS]
    cjk: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        cjk.append(run) if len(run) == 1 else cjk.extend(run[index : index + 2] for index in range(len(run) - 1))
    return latin + cjk


def normalize(text: str | Iterable[str]) -> str:
    values = tokens(text) if isinstance(text, str) else [part for value in text for part in tokens(value)]
    return " ".join(values)


def fts_query(text: str) -> str:
    """Produce a safe AND query for the pre-tokenised FTS column."""

    return " AND ".join(f'"{value.replace(chr(34), chr(34) * 2)}"' for value in tokens(text))

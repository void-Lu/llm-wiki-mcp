"""Deterministic lexical normalization shared by FTS and retrieval callers."""

from __future__ import annotations

import re
from collections.abc import Iterable

_LATIN_OR_CODE = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_QUALIFIED_CODE = re.compile(r"(?<![a-z0-9_])([a-z][a-z0-9_]*)/([a-z][a-z0-9_]*)(?![a-z0-9_])", re.I)
_MULTIWORD_RUN = re.compile(r"[a-z0-9_]+(?:\s+[a-z0-9_]+)+", re.I)
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


def _fts_expression(values: Iterable[str], operator: str) -> str:
    return f" {operator} ".join(f'"{value.replace(chr(34), chr(34) * 2)}"' for value in values)


def fts_query(text: str) -> str:
    """Produce a safe, precise AND query for the pre-tokenised FTS column."""

    return _fts_expression(tokens(text), "AND")


def relaxed_fts_query(text: str) -> str:
    """Produce a bounded OR recovery query after a natural-language miss."""

    return _fts_expression(tokens(text), "OR")


def qualified_code_fts_query(text: str) -> str:
    """Extract slash-qualified identifiers and keep their verbatim slash term.

    Slash-qualified identifiers (for example ``N/record``) are recovered with
    their parsed segments, and the verbatim slash term (for example
    ``List/Record``) is preserved as an adjacent phrase.  Both forms are part
    of the query so a generic English slash term keeps its original meaning
    instead of being reduced to two disconnected tokens.
    """

    matches = _QUALIFIED_CODE.findall(text)
    if not matches:
        return ""
    parsed = [segment.casefold() for match in matches for segment in match]
    clauses = [_fts_expression(parsed, "AND")]
    phrases = [" ".join(segment.casefold() for segment in match) for match in matches]
    clauses.append(" OR ".join(f'"{phrase}"' for phrase in phrases))
    return " OR ".join(clauses)


def module_qualified(text: str) -> bool:
    """True when every slash-qualified prefix is a single-letter namespace.

    ``N/record`` is a SuiteScript module identifier and deserves the dedicated
    qualified-code recovery path; ``List/Record`` is a NetSuite field-type
    label and must fall back to ordinary multilingual recovery instead.
    """

    matches = _QUALIFIED_CODE.findall(text)
    return bool(matches) and all(len(prefix.casefold()) == 1 for prefix, _ in matches)


def identifier_phrases(text: str) -> list[str]:
    """Return space-separated English runs such as ``ai connector``.

    A genuine multi-word English run signals a product or feature name (for
    example ``NetSuite AI Connector``).  Slash-qualified identifiers such as
    ``List/Record`` and ``N/record`` do not form a run and keep their own
    dedicated handling.
    """

    return [match.casefold() for match in _MULTIWORD_RUN.findall(text)]


def identifier_phrase_tokens(text: str) -> list[str]:
    """Return Latin content tokens only when the text has a multi-word run.

    Without a run the Latin words are incidental vocabulary mixed into a
    Chinese question; with a run they name a precise identifier and deserve a
    strict AND lookup instead of being diluted by relaxed bigram noise.
    Single-letter tokens (for example the ``N`` in ``N/record``) are dropped
    because they carry no identifier signal of their own.
    """

    if not _MULTIWORD_RUN.search(text):
        return []
    return [value for value in tokens(text) if re.fullmatch(r"[a-z0-9_]+", value) and len(value) > 1]


def identifier_phrase_fts_query(text: str) -> str:
    """Produce a precise AND query over the identifier phrase's Latin tokens."""

    values = identifier_phrase_tokens(text)
    return _fts_expression(values, "AND") if len(values) >= 2 else ""

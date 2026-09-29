"""Compact, single-body context packing for passage query results."""

from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from retrieval.body_budget import DEFAULT_INTENT_TARGET, INTENT_TARGETS
from retrieval.token_units import _PASSAGE_UNIT_RE as _TOKEN_UNIT_PASSAGE_RE, count_response_tokens, passage_token_units

# The chunker repeats up to ``DEFAULT_OVERLAP_TOKENS`` (64) passage units of
# the previous chunk at the start of the next one, re-joined with spaces, so
# the repeat is compared on passage units rather than on raw text.  Shorter
# matches than MIN_OVERLAP_UNITS are treated as coincidence.
MAX_OVERLAP_UNITS = 128
MIN_OVERLAP_UNITS = 8
# A paragraph that already appeared earlier in the pack (any page) is
# dropped -- template boilerplate repeated on many pages.  Paragraphs are
# compared on their letters/digits only; CJK characters count twice, so the
# threshold is about eight Latin words or two dozen ideographs.
MIN_REPEATED_PARAGRAPH_WEIGHT = 48
_NON_WORD_RE = re.compile(r"[\W_]+")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_PASSAGE_UNIT_RE = _TOKEN_UNIT_PASSAGE_RE


@dataclass(frozen=True)
class ContextPassage:
    passage_id: str
    path: str
    heading: str
    content: str
    score: float
    evidence_kind: str
    citation_metadata: Mapping[str, str] = field(default_factory=dict)
    ordinal: int = -1


def estimate_response_tokens(value: str) -> int:
    """Count response-budget units; this scale is not a passage chunk limit."""

    return max(1, count_response_tokens(value)) if value else 0


def _deduplicate_overlap(previous: str, current: str) -> str:
    if not previous or not current:
        return current
    previous_words = previous.split()
    current_words = current.split()
    maximum = min(len(previous_words), len(current_words), 48)
    for length in range(maximum, 0, -1):
        if previous_words[-length:] == current_words[:length]:
            return " ".join(current_words[length:])
    return current


_WINDOW_CHARS_PER_UNIT = 16


def _head_units(text: str) -> list[str]:
    window = MAX_OVERLAP_UNITS * _WINDOW_CHARS_PER_UNIT
    units = _PASSAGE_UNIT_RE.findall(text[:window])
    # The window may cut the last unit short; it is never needed.
    return (units[:-1] if len(text) > window else units)[:MAX_OVERLAP_UNITS]


def _tail_units(text: str) -> list[str]:
    window = MAX_OVERLAP_UNITS * _WINDOW_CHARS_PER_UNIT
    units = _PASSAGE_UNIT_RE.findall(text[-window:])
    return (units[1:] if len(text) > window else units)[-MAX_OVERLAP_UNITS:]


def _overlap_length(left: Sequence[str], right: Sequence[str]) -> int:
    """Longest k (MIN..MAX units) with ``left[-k:] == right[:k]``, else 0."""

    if not left or not right:
        return 0
    maximum = min(len(left), len(right), MAX_OVERLAP_UNITS)
    first = right[0]
    for start in range(len(left) - maximum, len(left) - MIN_OVERLAP_UNITS + 1):
        if left[start] == first:
            length = len(left) - start
            if list(left[start:]) == list(right[:length]):
                return length
    return 0


def _offset_after_units(text: str, count: int) -> int:
    for index, match in enumerate(_PASSAGE_UNIT_RE.finditer(text), 1):
        if index == count:
            return match.end()
    return len(text)


def _offset_before_last_units(text: str, count: int) -> int:
    window = max(0, len(text) - (count + 1) * _WINDOW_CHARS_PER_UNIT)
    starts = [window + match.start() for match in _PASSAGE_UNIT_RE.finditer(text[window:])]
    if window and starts:
        starts = starts[1:]
    return starts[-count] if len(starts) >= count else 0


def strip_chunk_overlap(text: str, *, previous: str | None, following: str | None) -> str:
    """Remove the prefix repeated from *previous* and the suffix repeated in *following*.

    *previous*/*following* are the already-packed reading-order neighbours of
    the same page.  Only one side of each adjacent pair is ever stripped (the
    one packed later), so stitching the page in reading order never loses
    text.
    """

    start, end = 0, len(text)
    if previous:
        cut = _overlap_length(_cached_tail_units(previous), _head_units(text))
        if cut:
            start = _offset_after_units(text, cut)
    if following:
        cut = _overlap_length(_tail_units(text), _cached_head_units(following))
        if cut:
            end = _offset_before_last_units(text, cut)
    return text[start:end].strip() if start < end else ""


@lru_cache(maxsize=512)
def _cached_tail_units(text: str) -> tuple[str, ...]:
    return tuple(_tail_units(text))


@lru_cache(maxsize=512)
def _cached_head_units(text: str) -> tuple[str, ...]:
    return tuple(_head_units(text))


def _paragraph_key(paragraph: str) -> str | None:
    key = _NON_WORD_RE.sub("", paragraph.casefold())
    weight = len(key) + len(_CJK_RE.findall(key))
    return key if weight >= MIN_REPEATED_PARAGRAPH_WEIGHT else None


def drop_repeated_paragraphs(text: str, seen: set[str]) -> tuple[str, list[str]]:
    """Drop paragraphs whose key is in *seen*; return the text and the kept keys.

    The caller adds the returned keys to *seen* only if the passage is packed.
    """

    kept: list[str] = []
    keys: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        key = _paragraph_key(paragraph)
        if key is not None:
            if key in seen or key in keys:
                continue
            keys.append(key)
        kept.append(paragraph)
    return "\n\n".join(part for part in kept if part.strip()).strip(), keys


def pack_context(
    passages: Iterable[ContextPassage],
    *,
    hard_limit: int,
    intent: str,
    budget_scale: int | None = None,
) -> dict[str, object]:
    """Pack page-ordered bodies for the canonical query response.

    Context is aggregated by path in one pass; ``seen`` and
    ``previous_by_path`` record budget-gated and overlap-deduplicated content,
    not grouping behavior.

    ``budget_scale`` lets callers grow the budget with the number of requested
    results (for example 400 tokens per result).  The intent target remains a
    floor: a ``research`` question keeps its deep budget even with a small
    result count, while a wider result set scales the pack accordingly.
    """

    target = INTENT_TARGETS.get(intent, DEFAULT_INTENT_TARGET)
    if budget_scale is not None:
        target = max(target, budget_scale)
    budget = min(max(1, hard_limit), target)
    candidates = list(passages)
    aggregated: dict[str, dict[str, object]] = {}
    order: list[str] = []
    used = 0
    previous_by_path: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    seen_paragraphs: set[str] = set()
    packed_text: dict[tuple[str, int], str] = {}
    segments: dict[str, list[tuple[int, int, str]]] = {}
    for position, item in enumerate(candidates):
        key = (item.path, item.content)
        if key in seen:
            continue
        seen.add(key)
        if item.ordinal >= 0:
            # Reading-order neighbours already packed for this page: strip
            # the chunk overlap they share with this passage.
            content = strip_chunk_overlap(
                item.content,
                previous=packed_text.get((item.path, item.ordinal - 1)),
                following=packed_text.get((item.path, item.ordinal + 1)),
            )
        else:
            content = _deduplicate_overlap(previous_by_path.get(item.path, ""), item.content).strip()
        # Paragraphs are only recorded as seen once the passage is packed.
        deduplicated, paragraph_keys = drop_repeated_paragraphs(content, seen_paragraphs)
        if not deduplicated and str(item.path) not in aggregated:
            # Never leave a page without any body: its first passage keeps
            # repeated paragraphs rather than vanishing from the context.
            deduplicated = content.strip()
        content = deduplicated
        if not content:
            continue
        tokens = estimate_response_tokens(content)
        if used + tokens > budget:
            continue
        seen_paragraphs.update(paragraph_keys)
        path = str(item.path)
        if path not in aggregated:
            aggregated[path] = {
                "path": path,
                "heading": item.heading,
                "evidence_kind": item.evidence_kind,
                "content": "",
                "tokens": 0,
            }
            order.append(path)
        entry = aggregated[path]
        entry["tokens"] = int(entry["tokens"]) + tokens
        segments.setdefault(path, []).append((item.ordinal, position, content))
        if item.ordinal >= 0:
            packed_text[(item.path, item.ordinal)] = item.content
        previous_by_path[item.path] = f"{previous_by_path.get(item.path, '')} {content}".strip()
        used += tokens
    for path in order:
        # Stitch each page in reading order (passages without a known
        # ordinal keep their packing order, ahead of ordered ones).
        parts = sorted(segments[path], key=lambda part: (part[0], part[1]))
        aggregated[path]["content"] = "\n\n".join(content for _, _, content in parts)

    retained_tokens = sum(int(aggregated[path]["tokens"]) for path in order)
    used = retained_tokens
    return {
        "passages": [aggregated[path] for path in order],
        "budget": {
            "target": target,
            "total": budget,
            "used": used,
            "omitted": max(
                0,
                sum(estimate_response_tokens(item.content) for item in candidates)
                - retained_tokens,
            ),
        },
    }

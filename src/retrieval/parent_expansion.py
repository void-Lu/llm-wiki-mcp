"""Small-to-Big parent expansion for selected passage hits.

After FTS ranks small passages, expand each hit with same-page ordinal
neighbors before ``pack_context``. Citations stay on the hit ``passage_id``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from retrieval.context_packer import strip_chunk_overlap
from retrieval.retrieval_index import PassageHit

DEFAULT_PARENT_WINDOW = 1
# Soft cap for one expanded parent body (hit + kept neighbors).
DEFAULT_MAX_PARENT_CHARS = 1500
# Soft cap on total characters added beyond original hit texts in one batch.
DEFAULT_MAX_TOTAL_EXPANSION_CHARS = 6000


@dataclass(frozen=True)
class ExpandedPassage:
    """Hit identity preserved; ``text`` may include neighbor bodies."""

    passage_id: str
    page_path: str
    heading_path: tuple[str, ...]
    ordinal: int
    text: str
    expanded: bool
    neighbor_ordinals: tuple[int, ...]
    expansion_chars: int


def _neighbor_offsets(window: int) -> list[int]:
    """Preference order: closer first, ``-k`` before ``+k``."""

    offsets: list[int] = []
    for distance in range(1, max(0, window) + 1):
        offsets.append(-distance)
        offsets.append(distance)
    return offsets


def _join_with_overlap_stripped(parts: Sequence[str]) -> str:
    """Stitch parts in reading order, stripping chunker overlap on later parts."""

    assembled = ""
    for part in parts:
        body = part.strip()
        if not body:
            continue
        if not assembled:
            assembled = body
            continue
        stripped = strip_chunk_overlap(body, previous=assembled, following=None)
        if not stripped:
            continue
        assembled = f"{assembled}\n\n{stripped}"
    return assembled


def _select_neighbors_under_budget(
    hit_text: str,
    candidates: Sequence[tuple[int, str]],
    *,
    max_parent_chars: int,
    remaining_total_expansion: int,
) -> list[tuple[int, str]]:
    """Greedily keep neighbors in preference order without exceeding budgets.

    ``candidates`` must already be in keep-preference order (``-1``, ``+1``, …).
    The hit body is always kept; neighbors are dropped farthest-first by never
    selecting them when the budget is exhausted (``+1`` is considered after
    ``-1``, so ``-1`` wins when only one side fits).
    """

    kept: list[tuple[int, str]] = []
    used = len(hit_text)
    expansion_used = 0
    for ordinal, text in candidates:
        body = text.strip()
        if not body:
            continue
        # Separator overhead mirrors ``_join_with_overlap_stripped``.
        added = len(body) + (2 if used else 0)
        if used + added > max_parent_chars:
            continue
        if expansion_used + len(body) > remaining_total_expansion:
            continue
        kept.append((ordinal, body))
        used += added
        expansion_used += len(body)
    return kept


def expand_one_hit(
    hit: PassageHit,
    neighbors_by_ordinal: Mapping[int, PassageHit],
    *,
    window: int = DEFAULT_PARENT_WINDOW,
    same_heading_only: bool = False,
    max_parent_chars: int = DEFAULT_MAX_PARENT_CHARS,
    remaining_total_expansion: int = DEFAULT_MAX_TOTAL_EXPANSION_CHARS,
) -> ExpandedPassage:
    """Expand a single hit using already-loaded same-page neighbors."""

    if hit.ordinal < 0 or window <= 0:
        return ExpandedPassage(
            hit.passage_id,
            hit.page_path,
            hit.heading_path,
            hit.ordinal,
            hit.text,
            False,
            (),
            0,
        )

    candidates: list[tuple[int, str]] = []
    for offset in _neighbor_offsets(window):
        ordinal = hit.ordinal + offset
        neighbor = neighbors_by_ordinal.get(ordinal)
        if neighbor is None:
            continue
        if same_heading_only and neighbor.heading_path != hit.heading_path:
            continue
        candidates.append((ordinal, neighbor.text))

    kept = _select_neighbors_under_budget(
        hit.text,
        candidates,
        max_parent_chars=max_parent_chars,
        remaining_total_expansion=remaining_total_expansion,
    )
    if not kept:
        return ExpandedPassage(
            hit.passage_id,
            hit.page_path,
            hit.heading_path,
            hit.ordinal,
            hit.text,
            False,
            (),
            0,
        )

    by_ordinal = {ordinal: text for ordinal, text in kept}
    by_ordinal[hit.ordinal] = hit.text
    ordered_ordinals = sorted(by_ordinal)
    parts = [by_ordinal[ordinal] for ordinal in ordered_ordinals]
    text = _join_with_overlap_stripped(parts)
    neighbor_ordinals = tuple(ordinal for ordinal in ordered_ordinals if ordinal != hit.ordinal)
    expansion_chars = max(0, len(text) - len(hit.text))
    return ExpandedPassage(
        hit.passage_id,
        hit.page_path,
        hit.heading_path,
        hit.ordinal,
        text,
        True,
        neighbor_ordinals,
        expansion_chars,
    )


def expand_hit_texts(
    hits: Sequence[PassageHit],
    *,
    load_passages: Callable[[str, Sequence[int]], Sequence[PassageHit]],
    window: int = DEFAULT_PARENT_WINDOW,
    same_heading_only: bool = False,
    max_parent_chars: int = DEFAULT_MAX_PARENT_CHARS,
    max_total_expansion_chars: int = DEFAULT_MAX_TOTAL_EXPANSION_CHARS,
) -> list[ExpandedPassage]:
    """Expand each hit independently with same-page ``ordinal ± window`` neighbors.

    ``load_passages(page_path, ordinals)`` must return available passages for
    that page (missing ordinals omitted). Citation identity stays on each hit.
    """

    results: list[ExpandedPassage] = []
    remaining = max_total_expansion_chars
    # Group needed ordinals per page for one lookup batch per page.
    needed: dict[str, set[int]] = {}
    for hit in hits:
        if hit.ordinal < 0 or window <= 0:
            continue
        bucket = needed.setdefault(hit.page_path, set())
        for offset in _neighbor_offsets(window):
            bucket.add(hit.ordinal + offset)
        bucket.add(hit.ordinal)

    loaded: dict[str, dict[int, PassageHit]] = {}
    for page_path, ordinals in needed.items():
        found = load_passages(page_path, sorted(ordinals))
        loaded[page_path] = {item.ordinal: item for item in found}

    for hit in hits:
        page_neighbors = loaded.get(hit.page_path, {})
        expanded = expand_one_hit(
            hit,
            page_neighbors,
            window=window,
            same_heading_only=same_heading_only,
            max_parent_chars=max_parent_chars,
            remaining_total_expansion=remaining,
        )
        remaining = max(0, remaining - expanded.expansion_chars)
        results.append(expanded)
    return results

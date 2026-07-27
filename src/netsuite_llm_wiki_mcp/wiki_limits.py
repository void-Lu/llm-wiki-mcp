"""Shared capacity limits for generated wiki pages.

The limits are deliberately expressed in UTF-8 bytes because that is the unit
used by :mod:`wiki_lint` and by the filesystem.  Writers use the smaller target
limit so that a later rendering change has room before reaching the hard lint
limit.
"""

from __future__ import annotations

from collections.abc import Iterable


HARD_PAGE_BYTES = 200_000
TARGET_PAGE_BYTES = 180_000
MAX_LOG_ENTRIES = 200
TARGET_LOG_ENTRIES = 100
MAX_NAVIGATION_ENTRIES = 200


def utf8_size(text: str) -> int:
    """Return the exact on-disk size of *text* when encoded as UTF-8."""

    return len(text.encode("utf-8"))


def render_units(header: str, units: Iterable[str], footer: str = "") -> str:
    """Render complete units with the conventional blank-line separators."""

    parts = [part.rstrip() for part in (header, *units, footer) if part and part.rstrip()]
    return "\n\n".join(parts).rstrip() + "\n"


def partition_rendered_units(
    units: Iterable[str],
    header: str,
    footer: str = "",
    target_bytes: int = TARGET_PAGE_BYTES,
    max_units: int | None = None,
) -> tuple[list[list[str]], list[str]]:
    """Partition indivisible rendered units without exceeding ``target_bytes``.

    The returned ``pages`` contain only the supplied units; callers render each
    page with the same header/footer through :func:`render_units`.  Units that
    cannot fit by themselves are returned separately and are never split.
    """

    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")

    pages: list[list[str]] = []
    current: list[str] = []
    oversized: list[str] = []
    for unit in units:
        candidate = [*current, unit]
        if (
            (max_units is None or len(candidate) <= max_units)
            and utf8_size(render_units(header, candidate, footer)) <= target_bytes
        ):
            current.append(unit)
            continue
        if current:
            pages.append(current)
            current = []
        if utf8_size(render_units(header, [unit], footer)) > target_bytes:
            oversized.append(unit)
        else:
            current.append(unit)
    if current:
        pages.append(current)
    return pages, oversized

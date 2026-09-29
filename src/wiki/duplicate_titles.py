"""Write-time hints: near-duplicate titles for newly created pages.

Advisory only: the page is still created, and nothing is rewritten.  Exact
title/alias matches come from ``ConceptRegistry.resolve`` (the same
normalisation used for concept aliases, applied to every active Wiki page in
the retrieval projection).  Near matches use a character-bigram Jaccard
score over the normalised title and each existing title, alias and filename
stem phrase.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from wiki.concept_registry import ConceptRecord, ConceptRegistry, normalize_alias
from wiki.link_suggestions import is_hint_target, load_target_rows

# Chosen on seeded variants of graph_v1 titles plus a Chinese set (see
# tests/fixtures/retrieval/graph-eval-baseline.md): 0.6 keeps every
# leave-one-out graph_v1 title warning-free while catching plural, suffix and
# single-typo variants.
SIMILAR_TITLE_THRESHOLD = 0.6
# Short titles gain a suffix ("限流器" → "限流器组件", "Rate Engine" →
# "Rate Engine Service") with a bigram Jaccard below the threshold, so a
# normalised title containing the other also counts when the shorter side
# has at least three characters and covers at least half of the longer one.
CONTAINMENT_MIN_CHARS = 3
CONTAINMENT_MIN_RATIO = 0.5
MAX_DUPLICATE_WARNINGS = 5


def bigrams(value: str) -> frozenset[str]:
    return _grams(normalize_alias(value))


def _grams(normalized: str) -> frozenset[str]:
    if len(normalized) < 2:
        return frozenset({normalized}) if normalized else frozenset()
    return frozenset(normalized[index : index + 2] for index in range(len(normalized) - 1))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def containment(left: str, right: str) -> float:
    """Length ratio when one normalised surface contains the other, else 0."""

    return _containment(normalize_alias(left), normalize_alias(right))


def _containment(a: str, b: str) -> float:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < CONTAINMENT_MIN_CHARS or short not in long:
        return 0.0
    return len(short) / len(long)


def near_duplicate_titles(
    page_path: str,
    title: str,
    rows: Iterable[dict[str, Any]],
    *,
    aliases: Sequence[str] = (),
    threshold: float = SIMILAR_TITLE_THRESHOLD,
    limit: int = MAX_DUPLICATE_WARNINGS,
) -> list[dict[str, Any]]:
    """Return existing pages whose title/aliases match or nearly match *title*."""

    pages = [row for row in rows if is_hint_target(row) and str(row["path"]) != page_path]
    surfaces_by_path: dict[str, list[str]] = {}
    records: list[ConceptRecord] = []
    for row in pages:
        path = str(row["path"])
        page_title = str(row.get("title") or "")
        page_aliases = tuple(str(alias) for alias in row.get("aliases") or ())
        stem = path.rsplit("/", 1)[-1][:-3]
        surfaces_by_path[path] = [page_title, *page_aliases, " ".join(stem.replace("_", "-").split("-"))]
        records.append(ConceptRecord(path, path, page_title, page_aliases, "", (), str(row.get("lifecycle") or "active")))
    titles = {str(row["path"]): str(row.get("title") or "") for row in pages}
    registry = ConceptRegistry(Path("."), records=records)
    found: dict[str, dict[str, Any]] = {}
    new_surfaces = [surface for surface in (title, *aliases) if normalize_alias(surface)]
    for surface in new_surfaces:
        resolved = registry.resolve(surface, collect_evidence=False)
        if resolved["action"] == "existing":
            for record in resolved["matches"]:
                found[record.path] = {"path": record.path, "title": record.title, "reason": "same_title_or_alias", "score": 1.0, "matched": surface}
    new_forms = [(surface, normalize_alias(surface)) for surface in new_surfaces]
    new_grams = [(surface, norm, _grams(norm)) for surface, norm in new_forms]
    for path, surfaces in surfaces_by_path.items():
        if path in found:
            continue
        best = (0.0, "", "")
        contained = (0.0, "")
        for existing in surfaces:
            existing_norm = normalize_alias(existing)
            existing_grams = _grams(existing_norm)
            for surface, norm, grams in new_grams:
                score = jaccard(grams, existing_grams)
                if score > best[0]:
                    best = (score, surface, existing)
                ratio = _containment(norm, existing_norm)
                if ratio > contained[0]:
                    contained = (ratio, surface)
        if best[0] >= threshold:
            found[path] = {"path": path, "title": titles[path], "reason": "similar_title", "score": round(best[0], 3), "matched": best[1]}
        elif contained[0] >= CONTAINMENT_MIN_RATIO:
            found[path] = {"path": path, "title": titles[path], "reason": "title_contains", "score": round(contained[0], 3), "matched": contained[1]}
    return sorted(found.values(), key=lambda item: (-item["score"], item["path"]))[:limit]


def duplicate_title_warnings(
    root: str | Path,
    page_path: str,
    title: str,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper used by ``wiki_write_note``; never raises."""

    if rows is None:
        rows = load_target_rows(root)
    if not rows:
        return []
    try:
        return near_duplicate_titles(page_path, title, rows)
    except Exception:  # advisory only: a hint failure must not fail a write
        return []

"""Write-time hints: unlinked mentions of other Wiki pages.

The hints are advisory.  They are computed after a page is saved, returned in
the tool response, and never written back into the page.  Page titles and
aliases come from the already-built retrieval projection
(``RetrievalIndexStore.link_targets``); when that projection is missing or
incompatible no hints are produced and the write is unaffected.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from wiki.wiki_paths import slug
from wiki.wikilinks import code_ranges, format_wikilink, iter_wikilinks

MAX_LINK_SUGGESTIONS = 20
# Latin terms shorter than this ("API", "SMS") and CJK terms shorter than two
# ideographs match far too much ordinary text to be useful link hints.
MIN_LATIN_TERM_CHARS = 4
MIN_CJK_TERM_CHARS = 2
_EXCLUDED_PREFIXES = ("wiki/sources/", "wiki/archives/")
_STRUCTURAL_NAMES = {"index.md", "log.md", "overview.md"}
_STRUCTURAL_TYPES = {"index", "project_index", "log", "overview", "navigation"}
_INACTIVE_LIFECYCLES = {"superseded", "deprecated", "archived", "retired"}
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_WORD_CHAR_RE = re.compile(r"[0-9A-Za-z_]")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)|<https?://[^>\s]+>|https?://\S+")


@dataclass(frozen=True)
class LinkTarget:
    path: str
    title: str
    stem: str
    terms: tuple[str, ...]


def is_hint_target(row: dict[str, Any]) -> bool:
    """True for active, non-structural Wiki pages outside sources/archives."""

    path = str(row.get("path") or "")
    if not path.startswith("wiki/") or path.startswith(_EXCLUDED_PREFIXES) or not path.endswith(".md"):
        return False
    if path.rsplit("/", 1)[-1].casefold() in _STRUCTURAL_NAMES or str(row.get("type") or "") in _STRUCTURAL_TYPES:
        return False
    return str(row.get("lifecycle") or "").casefold() not in _INACTIVE_LIFECYCLES


def eligible_link_targets(rows: Iterable[dict[str, Any]]) -> list[LinkTarget]:
    """Project index rows into link targets (active, non-structural pages)."""

    targets: list[LinkTarget] = []
    for row in rows:
        if not is_hint_target(row):
            continue
        path = str(row["path"])
        stem = path.rsplit("/", 1)[-1][:-3]
        title = " ".join(str(row.get("title") or "").split())
        terms = [title, *(" ".join(str(alias).split()) for alias in row.get("aliases") or ())]
        # A multi-word filename stem ("retry-budget") is how the page is
        # usually referred to in prose even when its title is longer
        # ("Retry Budget Rule").
        stem_phrase = " ".join(stem.replace("_", "-").split("-")).strip()
        if len(stem_phrase.split()) >= 2 or _CJK_RE.search(stem_phrase):
            terms.append(stem_phrase)
        unique = tuple(dict.fromkeys(term for term in terms if _usable_term(term)))
        if unique:
            targets.append(LinkTarget(path, title, stem, unique))
    return targets


def load_target_rows(root: str | Path) -> list[dict[str, Any]] | None:
    """Read page titles/aliases from the retrieval projection; ``None`` if unavailable."""

    from retrieval.retrieval_index import RetrievalIndexError, RetrievalIndexStore

    try:
        return RetrievalIndexStore(Path(root).expanduser().resolve()).link_targets()
    except (RetrievalIndexError, sqlite3.Error, OSError, ValueError):
        return None


def load_link_targets(root: str | Path) -> list[LinkTarget] | None:
    """Read link targets from the retrieval projection; ``None`` if unavailable."""

    rows = load_target_rows(root)
    return None if rows is None else eligible_link_targets(rows)


def suggest_unlinked_mentions(
    page_path: str,
    body: str,
    targets: Sequence[LinkTarget],
    *,
    limit: int = MAX_LINK_SUGGESTIONS,
) -> list[dict[str, Any]]:
    """Return the first unlinked mention of each other page in *body*.

    Skips fenced/inline code, existing wikilinks and Markdown links, the page
    itself, pages the body already links to, and terms that name more than one
    page.  Latin terms need word boundaries; single-word Latin terms must
    match case exactly (so "ledger" in prose does not suggest a page titled
    "Ledger"); CJK terms match as substrings, longest term first.
    """

    own_terms = {term.casefold() for target in targets if target.path == page_path for term in target.terms}
    owners: dict[str, set[str]] = {}
    for target in targets:
        for term in target.terms:
            owners.setdefault(term.casefold(), set()).add(target.path)
    by_path = {target.path: target for target in targets}
    linked = _linked_keys(body)
    masked = _masked_ranges(body)
    lowered = body.lower()
    if len(lowered) != len(body):
        lowered = body
    claimed: list[tuple[int, int]] = []
    found: dict[str, dict[str, Any]] = {}
    terms = sorted(owners, key=lambda term: (-len(term), term))
    display = {term.casefold(): term for target in targets for term in target.terms}
    for key in terms:
        term = display[key]
        case_sensitive = _is_single_latin_word(term)
        haystack, needle = (body, term) if case_sensitive else (lowered, term.lower())
        if len(needle) != len(term):
            continue
        paths = owners[key]
        ambiguous = len(paths) > 1 or key in own_terms or page_path in paths
        for start in _occurrences(haystack, needle):
            end = start + len(needle)
            if not _bounded(body, start, end, term) or _overlaps(masked, start, end) or _overlaps(claimed, start, end):
                continue
            # Longer terms claim their text even when not suggested, so
            # "Carrier Gateway" never also yields a hint for "Gateway".
            claimed.append((start, end))
            if ambiguous:
                continue
            (path,) = paths
            target = by_path[path]
            # Several terms (title, aliases, stem phrase) can name one page;
            # keep the earliest occurrence across all of them.
            if (path in found and found[path]["offset"] <= start) or _is_linked(target, linked):
                continue
            mention = body[start:end]
            found[path] = {
                "target": path,
                "title": target.title,
                "mention": mention,
                "line": body.count("\n", 0, start) + 1,
                "offset": start,
                "link": format_wikilink(target.stem, None if mention == target.stem else mention),
            }
    ordered = sorted(found.values(), key=lambda item: (item["offset"], item["target"]))[:limit]
    for item in ordered:
        item.pop("offset")
    return ordered


def unlinked_mention_suggestions(
    root: str | Path,
    page_path: str,
    body: str,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper used by the write tools; never raises."""

    if rows is None:
        rows = load_target_rows(root)
    if not rows:
        return []
    try:
        return suggest_unlinked_mentions(page_path, body, eligible_link_targets(rows))
    except Exception:  # advisory only: a hint failure must not fail a write
        return []


def _usable_term(term: str) -> bool:
    if not term or not any(char.isalnum() for char in term):
        return False
    if term.replace(" ", "").isdigit():
        return False
    cjk = len(_CJK_RE.findall(term))
    if cjk:
        return cjk >= MIN_CJK_TERM_CHARS or len(term) >= MIN_LATIN_TERM_CHARS + cjk
    return len(term) >= MIN_LATIN_TERM_CHARS


def _is_single_latin_word(term: str) -> bool:
    return " " not in term and not _CJK_RE.search(term)


def _bounded(text: str, start: int, end: int, term: str) -> bool:
    if _WORD_CHAR_RE.match(term[0]) and start > 0 and _WORD_CHAR_RE.match(text[start - 1]):
        return False
    if _WORD_CHAR_RE.match(term[-1]) and end < len(text) and _WORD_CHAR_RE.match(text[end]):
        return False
    return True


def _occurrences(haystack: str, needle: str) -> Iterable[int]:
    start = haystack.find(needle)
    while start != -1:
        yield start
        start = haystack.find(needle, start + 1)


def _overlaps(ranges: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    return any(left < end and start < right for left, right in ranges)


def _masked_ranges(body: str) -> list[tuple[int, int]]:
    ranges = list(code_ranges(body))
    ranges.extend((token.start, token.end) for token in iter_wikilinks(body))
    ranges.extend(match.span() for match in _MARKDOWN_LINK_RE.finditer(body))
    return ranges


def _linked_keys(body: str) -> set[str]:
    keys: set[str] = set()
    for token in iter_wikilinks(body):
        target = token.target.split("#", 1)[0].strip()
        if target.endswith(".md"):
            target = target[:-3]
        keys.add(slug(target))
        keys.add(slug(target.rsplit("/", 1)[-1]))
    return keys


def _is_linked(target: LinkTarget, linked: set[str]) -> bool:
    candidates = {slug(target.stem), slug(target.path[:-3]), slug(target.path[len("wiki/"):-3]), slug(target.title)}
    return bool(candidates & linked)

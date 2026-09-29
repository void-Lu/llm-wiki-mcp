"""Index-time link edges for the retrieval graph projection.

Edges are extracted once when a page is projected into the retrieval store and
persisted in the ``links`` table.  Query-time graph expansion then resolves the
stored targets against the already-filtered candidate set instead of parsing
every page body on every query.

Wikilink resolution depends on the candidate boundary (filters) and on which
other pages exist, so the store keeps the raw target together with its
lexically normalised vault-relative path candidates and stem key.  The final
target page is still chosen per query, which keeps incremental maintenance
local to the page that changed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wiki.wikilinks import wikilink_targets

WIKILINK = "wikilink"
SOURCE = "source"
TYPED_RELATION_KINDS = ("related_objects", "applies_to", "derived_from")
# Typed relations whose values address wiki pages; ``applies_to`` holds labels.
PAGE_TARGET_RELATION_KINDS = ("related_objects", "derived_from")
GRAPH_EDGE_KINDS = (WIKILINK, SOURCE, *TYPED_RELATION_KINDS)
GRAPH_PAGE_PREFIX = "wiki/"
_PATH_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class PageLink:
    """One outgoing edge recorded for a projected page."""

    kind: str
    dst: str
    dst_paths: tuple[str, ...] = ()
    dst_stem: str = ""


def graph_body(chunk_texts: Iterable[str]) -> str:
    """Rebuild the body view that graph extraction has always parsed.

    Query V2 historically parsed ``group_concat(passages.text, '\\n\\n')`` of
    the indexed passages.  Extracting from the same view keeps edges identical
    to the former per-query parser, including code-fence detection.
    """

    return "\n\n".join(chunk_texts)


def extract_page_links(rel: str, body: str, frontmatter: Mapping[str, Any], *, root: Path) -> list[PageLink]:
    """Return ordered outgoing edges for one wiki page projection."""

    if not rel.startswith(GRAPH_PAGE_PREFIX):
        return []
    links = [
        PageLink(WIKILINK, target, _path_candidates(rel, target, root), Path(target).stem.casefold())
        for target in wikilink_targets(body)
    ]
    links.extend(PageLink(SOURCE, str(item)) for item in frontmatter_values(frontmatter.get("sources")))
    for kind in TYPED_RELATION_KINDS:
        for target in _typed_targets(kind, frontmatter.get(kind)):
            if kind in PAGE_TARGET_RELATION_KINDS:
                links.append(PageLink(kind, target, _path_candidates(rel, target, root), Path(target).stem.casefold()))
            else:
                links.append(PageLink(kind, target))
    return links


def _typed_targets(kind: str, value: Any) -> list[str]:
    """Normalise one typed frontmatter relation into ordered target strings.

    ``derived_from`` entries are shared-spec origins (``{project, path,
    rule}``) or plain paths; ``related_objects`` are page names, paths or
    wikilinks; ``applies_to`` holds applicability labels (``{languages: [...],
    frameworks: [...]}``), which name no page and are kept as ``key:label``.
    """

    targets: list[str] = []
    if kind == "applies_to" and isinstance(value, Mapping):
        for key in sorted(value, key=str):
            targets.extend(f"{key}:{label}" for label in frontmatter_values(value[key]))
        return [target for target in targets if target.strip()]
    for item in frontmatter_values(value):
        if isinstance(item, Mapping):
            item = item.get("path") or ""
        text = str(item).strip()
        if not text:
            continue
        targets.extend(list(wikilink_targets(text)) if "[[" in text else [text])
    return targets


def frontmatter_values(value: Any) -> list[Any]:
    """Normalise a scalar-or-sequence frontmatter value into a list."""

    if isinstance(value, (list, tuple)):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def encode_paths(paths: tuple[str, ...]) -> str:
    return _PATH_SEPARATOR.join(paths)


def decode_paths(value: str) -> tuple[str, ...]:
    return tuple(value.split(_PATH_SEPARATOR)) if value else ()


def resolve_wikilink(link: PageLink, by_rel: Mapping[str, object], by_stem: Mapping[str, list[str]]) -> str:
    """Pick the candidate page for one stored wikilink, or ``""``.

    Priority matches the former parser: page-relative path, ``wiki/``-relative
    path, vault-relative path, then a unique case-insensitive stem match among
    the current candidates.
    """

    for rel in link.dst_paths:
        if rel in by_rel:
            return rel
    stem_matches = by_stem.get(link.dst_stem, [])
    return stem_matches[0] if len(stem_matches) == 1 else ""


def _path_candidates(rel: str, target: str, root: Path) -> tuple[str, ...]:
    target_path = Path(target)
    if target_path.suffix != ".md":
        try:
            target_path = target_path.with_suffix(".md")
        except ValueError:
            # A target without a usable file name cannot address a page; only
            # the stem fallback remains available.
            return ()
    text = str(target_path)
    page_parent = os.path.dirname(rel.replace("/", os.sep))
    paths: list[str] = []
    for base in (page_parent, "wiki", ""):
        # Lexical normalisation mirrors ``Path.resolve`` for vault-internal
        # paths without touching the filesystem.  Symlinked directories inside
        # the vault are intentionally not followed.
        relative = "" if target_path.anchor else os.path.normpath(os.path.join(base, text))
        if not relative or relative == os.pardir or relative.startswith(os.pardir + os.sep):
            relative = _root_relative(root, base, target_path)
        relative = relative.replace(os.sep, "/")
        if relative and relative != "." and _PATH_SEPARATOR not in relative and relative not in paths:
            paths.append(relative)
    return tuple(paths)


def _root_relative(root: Path, base: str, target_path: Path) -> str:
    """Slow path for absolute targets or ones that step outside the vault."""

    candidate = Path(os.path.normpath(root / base / target_path))
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return ""


__all__ = [
    "GRAPH_EDGE_KINDS",
    "GRAPH_PAGE_PREFIX",
    "PAGE_TARGET_RELATION_KINDS",
    "PageLink",
    "SOURCE",
    "TYPED_RELATION_KINDS",
    "WIKILINK",
    "decode_paths",
    "encode_paths",
    "extract_page_links",
    "frontmatter_values",
    "graph_body",
    "resolve_wikilink",
]

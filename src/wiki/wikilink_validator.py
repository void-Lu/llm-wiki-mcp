"""Wikilink target validation and auto-normalization against vault filenames."""

from __future__ import annotations

from pathlib import Path

from wiki.wiki_paths import slug
from wiki.wikilinks import iter_wikilinks, normalize_wikilinks

_WIKI_DIR = "wiki"


def build_stem_index(vault_root: str | Path) -> dict[str, str]:
    """Scan ``wiki/`` for all ``.md`` files and return ``{slug(stem): actual_stem}``.

    The key is :func:`wiki.wiki_paths.slug` applied to the filename stem, so a
    wikilink target like ``"MapReduce 上下文对象"`` slugs to the same key as the
    actual file ``MapReduce-上下文对象.md``.
    """
    root = Path(vault_root).expanduser().resolve()
    wiki_dir = root / _WIKI_DIR
    if not wiki_dir.is_dir():
        return {}
    index: dict[str, str] = {}
    for path in wiki_dir.rglob("*.md"):
        stem = path.stem
        index[slug(stem)] = stem
    return index


def validate_wikilinks(
    content: str,
    vault_root: str | Path,
    stem_index: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return wikilink targets that won't resolve in Obsidian.

    A target is valid only if it **exactly** (case-insensitive) matches a
    filename stem.  Targets that can be slug-matched to a file are reported
    as broken with a *suggestion* so the caller knows the correct stem.
    """
    index = stem_index if stem_index is not None else build_stem_index(vault_root)
    exact_stems = {stem.casefold() for stem in index.values()}
    broken: list[dict[str, str]] = []
    for token in iter_wikilinks(content):
        target = token.target
        leaf = target.rsplit("/", 1)[-1]
        if leaf.casefold() in exact_stems:
            continue
        suggestion = index.get(slug(leaf), "")
        broken.append({"target": target, "suggestion": suggestion})
    return broken


def auto_normalize_wikilinks(
    content: str,
    vault_root: str | Path,
    stem_index: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Rewrite wikilink targets in *content* to match actual filename stems.

    Returns ``(normalized_content, fixed_count)``.  Only targets whose
    :func:`~wiki.wiki_paths.slug` matches an existing file are rewritten;
    unmatched targets are left untouched.
    """
    index = stem_index if stem_index is not None else build_stem_index(vault_root)
    mapping: dict[str, str] = {}
    for token in iter_wikilinks(content):
        target = token.target
        leaf = target.rsplit("/", 1)[-1]
        slug_key = slug(leaf)
        if slug_key in index:
            actual_stem = index[slug_key]
            if leaf != actual_stem:
                # Preserve path prefix if present.
                prefix = target[: len(target) - len(leaf)]
                mapping[target] = f"{prefix}{actual_stem}"
    if not mapping:
        return content, 0
    return normalize_wikilinks(content, mapping), len(mapping)

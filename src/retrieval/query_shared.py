"""Small immutable query boundary helpers shared by pipeline owners.

The module contains only value normalization and candidate eligibility rules.
It deliberately has no retrieval store, cancellation state, or execution
context; discovery and entity-batch owners consume these functions as pure
predicates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from retrieval.metadata_filters import QUERY_METADATA_FILTERS, normalize_metadata_filters, page_matches_filters
from retrieval.retrieval_index import PassageHit


@dataclass(frozen=True)
class QueryFilters:
    type: str | None = None
    tags: tuple[str, ...] = ()
    path_prefix: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "QueryFilters":
        normalized = normalize_metadata_filters(value, allowed=QUERY_METADATA_FILTERS, preserve_path_trailing=True)
        return cls(
            normalized.get("type"),
            tuple(normalized.get("tags", ())),
            normalized.get("path_prefix"),
        )


def is_source_index(path: str, frontmatter: Mapping[str, Any]) -> bool:
    page_type = str(frontmatter.get("type") or "").casefold()
    return (
        page_type in {"source_index", "source-index", "index", "source_summary"}
        or path.endswith("/index.md") and "/sources/" in path and "/capsules/" not in path
        or re.search(r"/(?:manifest|_toc_manifest|_path_aliases)\.json$", path.casefold()) is not None
    )


def is_retired_source_namespace(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized == "wiki/sources" or normalized.startswith("wiki/sources/")


def probe_hit(
    path: str,
    title: str,
    *,
    corpus: str = "active",
    authority: str = "",
    source_kind: str = "",
) -> PassageHit:
    """Build a placeholder hit used only by eligibility/filter probes."""

    return PassageHit("", path, title, (), "", 0.0, corpus, authority, source_kind)


def eligible(hit: PassageHit, metadata: Mapping[str, Mapping[str, Any]], *, scope: str) -> bool:
    fm = metadata.get(hit.page_path, {})
    if scope == "raw":
        normalized_path = hit.page_path.replace("\\", "/")
        if (
            not normalized_path.startswith("raw/sources/")
            or normalized_path.startswith("raw/sources/chat/")
        ):
            return False
    if scope != "archive" and is_retired_source_namespace(hit.page_path):
        return False
    lifecycle = str(fm.get("lifecycle") or fm.get("lifecycle_status") or "active")
    # An archive store contains only archived material. Its rows must not be
    # rejected merely because their lifecycle is correctly marked archived.
    if scope != "archive" and lifecycle in {"superseded", "deprecated", "archived"}:
        return False
    if is_source_index(hit.page_path, fm):
        return False
    history = hit.corpus == "history" or hit.source_kind in {"raw_chat", "legacy_chatlog"}
    return (
        scope == "raw"
        or scope == "all"
        or (scope == "history" and history)
        or (scope == "knowledge" and not history)
        or scope == "archive"
    )


def matches_request(
    hit: PassageHit,
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    project: str | None,
    filters: QueryFilters,
) -> bool:
    """Apply the same boundary filters to FTS and vector-only candidates."""

    frontmatter = metadata.get(hit.page_path, {})
    return page_matches_filters(
        frontmatter,
        hit.source_kind,
        project=project,
        page_type=filters.type,
        tags=filters.tags,
        path_prefix=filters.path_prefix,
        page_path=hit.page_path,
    )


def snapshot_page_eligible(
    page: Mapping[str, Any],
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
) -> bool:
    """Apply the shared eligibility and request filters to one snapshot page."""

    path = str(page["path"])
    probe = probe_hit(
        path,
        str(page["title"]),
        corpus=str(page.get("corpus") or "active"),
        authority=str(page.get("authority") or ""),
        source_kind=str(page.get("source_kind") or ""),
    )
    return eligible(probe, metadata, scope=scope) and matches_request(
        probe,
        metadata,
        project=project,
        filters=filters,
    )


def heading(hit: PassageHit) -> str:
    return " / ".join(hit.heading_path) if hit.heading_path else hit.title


__all__ = [
    "QueryFilters",
    "eligible",
    "heading",
    "is_retired_source_namespace",
    "is_source_index",
    "matches_request",
    "probe_hit",
    "snapshot_page_eligible",
]

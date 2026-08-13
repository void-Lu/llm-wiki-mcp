"""Shared, schema-safe metadata filters for query and catalog reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SUPPORTED_METADATA_FILTERS = frozenset(
    {"type", "tags", "path_prefix", "project", "freshness", "lifecycle", "corpus"}
)
QUERY_METADATA_FILTERS = frozenset({"type", "tags", "path_prefix"})


def path_matches_prefix(page_path: str, path_prefix: str) -> bool:
    """Match a vault path against a normalized directory boundary."""

    normalized_path = str(page_path).replace("\\", "/").strip("/")
    normalized_prefix = str(path_prefix).replace("\\", "/").strip(" /").rstrip("/")
    if not normalized_prefix:
        return True
    return normalized_path == normalized_prefix or normalized_path.startswith(f"{normalized_prefix}/")


def page_matches_filters(
    frontmatter: Mapping[str, Any],
    source_kind: str,
    *,
    project: str | None = None,
    page_type: str | None = None,
    tags: Sequence[str] = (),
    path_prefix: str | None = None,
    page_path: str = "",
) -> bool:
    """Apply the production metadata boundary to one retrieved page.

    Ordinary pages with no project metadata stay eligible. Type falls back to
    ``source_kind`` and requested tags must be a subset of list-like page
    metadata; scalar tag metadata never matches.
    """

    page_project = str(frontmatter.get("project") or "").casefold()
    if project and page_project and page_project != project.casefold():
        return False

    if page_type and str(frontmatter.get("type") or source_kind) != page_type:
        return False

    if tags:
        raw_tags = frontmatter.get("tags")
        if not isinstance(raw_tags, (list, tuple)):
            return False
        if not set(tags).issubset({str(tag) for tag in raw_tags}):
            return False

    if path_prefix and not path_matches_prefix(page_path, path_prefix):
        return False
    return True


def normalize_filter_aliases(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize public aliases without changing filter validation semantics.

    ``path_prefix`` is the canonical internal name.  ``pathPrefix`` remains a
    compatibility input at the public boundary, but accepting both spellings
    with different values would make the effective filter depend on iteration
    order.  Reject that ambiguity before the shared field allow-list runs.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("filters must be an object")

    normalized = dict(value)
    if "path_prefix" in value and "pathPrefix" in value:
        if value["path_prefix"] != value["pathPrefix"]:
            raise ValueError("filters.path_prefix and filters.pathPrefix must match")
        normalized.pop("pathPrefix")
    elif "pathPrefix" in value:
        normalized["path_prefix"] = value["pathPrefix"]
        normalized.pop("pathPrefix")
    return normalized


def normalize_metadata_filters(
    value: Mapping[str, Any] | None,
    *,
    allowed: frozenset[str] = SUPPORTED_METADATA_FILTERS,
    preserve_path_trailing: bool = False,
) -> dict[str, Any]:
    """Normalize filter values into a deterministic, SQL-friendly shape.

    The normalizer is deliberately independent of either the query pipeline
    or the catalog service.  That keeps the public filter vocabulary shared
    while allowing ``wiki_query`` to retain its existing three-field contract.
    """

    value = normalize_filter_aliases(value) or {}
    unknown = set(value) - set(allowed)
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise ValueError(f"filters contain unsupported fields: {names}")

    normalized: dict[str, Any] = {}
    for field in ("type", "project", "freshness", "lifecycle", "corpus"):
        if field not in value:
            continue
        raw = value[field]
        if raw is not None and not isinstance(raw, str):
            raise ValueError(f"filters.{field} must be a string")
        text = raw.strip() if isinstance(raw, str) else ""
        if text:
            normalized[field] = text

    if "tags" in value:
        raw_tags = value["tags"]
        if isinstance(raw_tags, (str, bytes)) or not isinstance(raw_tags, Sequence):
            raise ValueError("filters.tags must be a sequence of strings")
        if not all(isinstance(item, str) for item in raw_tags):
            raise ValueError("filters.tags must be a sequence of strings")
        tags = tuple(dict.fromkeys(item.strip() for item in raw_tags if item.strip()))
        normalized["tags"] = tags

    if "path_prefix" in value:
        raw_prefix = value["path_prefix"]
        if raw_prefix is not None and not isinstance(raw_prefix, str):
            raise ValueError("filters.path_prefix must be a string")
        raw_prefix_text = raw_prefix.replace("\\", "/") if raw_prefix else ""
        had_trailing = raw_prefix_text.rstrip().endswith("/")
        prefix = raw_prefix_text.strip(" /")
        if prefix:
            parts = prefix.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise ValueError("filters.path_prefix must be vault-relative")
            normalized["path_prefix"] = "/".join(parts) + ("/" if preserve_path_trailing and had_trailing else "")

    return normalized


def metadata_filter_fingerprint(filters: Mapping[str, Any]) -> str:
    """Return a stable fingerprint without making callers know JSON details."""

    import hashlib
    import json

    payload = {
        str(key): list(value) if isinstance(value, tuple) else value
        for key, value in sorted(filters.items())
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "QUERY_METADATA_FILTERS",
    "SUPPORTED_METADATA_FILTERS",
    "metadata_filter_fingerprint",
    "normalize_filter_aliases",
    "normalize_metadata_filters",
    "page_matches_filters",
    "path_matches_prefix",
]

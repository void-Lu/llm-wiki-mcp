"""Shared, schema-safe metadata filters for query and catalog reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SUPPORTED_METADATA_FILTERS = frozenset(
    {"type", "tags", "path_prefix", "project", "freshness", "lifecycle", "corpus"}
)
QUERY_METADATA_FILTERS = frozenset({"type", "tags", "path_prefix"})


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

    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("filters must be an object")
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
    "normalize_metadata_filters",
]

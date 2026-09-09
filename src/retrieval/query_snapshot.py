"""Immutable metadata snapshots shared by one Query V2 invocation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from retrieval.query_cancellation import QueryCancellationContext
from retrieval.retrieval_index import RetrievalIndexStore


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class QueryCorpusSnapshot:
    """One read-only page metadata/catalog view for a physical store."""

    scope: str
    pages: tuple[Mapping[str, object], ...]
    metadata: Mapping[str, Mapping[str, Any]]
    provenance: Mapping[str, Mapping[str, str]]

    @classmethod
    def empty(cls, scope: str) -> "QueryCorpusSnapshot":
        return cls(
            scope=scope,
            pages=(),
            metadata=MappingProxyType({}),
            provenance=MappingProxyType({}),
        )

    @classmethod
    def capture(
        cls,
        store: RetrievalIndexStore,
        *,
        cancellation: QueryCancellationContext,
    ) -> "QueryCorpusSnapshot":
        cancellation.checkpoint("snapshot")
        frozen_pages: list[Mapping[str, object]] = []
        metadata: dict[str, Mapping[str, Any]] = {}
        provenance: dict[str, Mapping[str, str]] = {}
        for index, raw_page in enumerate(store.page_candidates()):
            cancellation.checkpoint_batch(index, every=16, stage="snapshot")
            page = {str(key): _freeze(value) for key, value in raw_page.items()}
            path = str(page.get("path") or "")
            frontmatter = page.get("frontmatter")
            frozen_frontmatter = frontmatter if isinstance(frontmatter, Mapping) else MappingProxyType({})
            page["frontmatter"] = frozen_frontmatter
            frozen_page = MappingProxyType(page)
            frozen_pages.append(frozen_page)
            metadata[path] = frozen_frontmatter
            provenance[path] = MappingProxyType(
                {
                    key: str(page.get(key) or "")
                    for key in ("session_id", "occurred_at", "project", "content_hash")
                }
            )
        cancellation.checkpoint("snapshot")
        return cls(
            scope=store.scope,
            pages=tuple(frozen_pages),
            metadata=MappingProxyType(metadata),
            provenance=MappingProxyType(provenance),
        )


__all__ = ["QueryCorpusSnapshot"]

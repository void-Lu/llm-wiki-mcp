from __future__ import annotations

import pytest

from retrieval.candidate_items import candidate_item
from retrieval.discovery import discover_catalog
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_shared import QueryFilters
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit


def test_discovery_owner_returns_frozen_entities_from_one_snapshot() -> None:
    path = "wiki/concepts/module-catalog.md"
    hit = PassageHit(
        "catalog",
        path,
        "Module catalog",
        (),
        "# Module catalog\n\n| Name | Description |\n| --- | --- |\n| N/auth | Authentication |\n| N/search | Search |",
        1.0,
        "knowledge",
        "",
        "concept",
    )
    pages = ({"path": path, "title": "Module catalog", "frontmatter": {"type": "concept"}},)
    snapshot = QueryCorpusSnapshot(
        "active",
        pages,
        {path: {"type": "concept"}},
        {},
    )

    result = discover_catalog(
        store=object(),  # type: ignore[arg-type]
        snapshot=snapshot,
        question="Which modules are available?",
        selected=[candidate_item(hit, score=1.0)],
        context_items=[],
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(),
        cancellation=QueryCancellationContext.with_timeout(10.0),
    )

    assert result.requested is True
    assert [entity["canonical_id"] for entity in result.entities] == ["n/auth", "n/search"]
    with pytest.raises(TypeError):
        result.discovery["requested"] = False  # type: ignore[index]

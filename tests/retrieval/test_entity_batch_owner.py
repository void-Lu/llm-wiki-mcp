from __future__ import annotations

from retrieval.entity_batch import build_store_specs, run_entity_batch
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_shared import QueryFilters
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit


class _FakeStore:
    scope = "active"

    def search_fts(self, _query: str, **_kwargs: object) -> list[PassageHit]:
        return [
            PassageHit(
                "entity",
                "wiki/entities/client-script.md",
                "Client Script",
                (),
                "Client Script entry points",
                1.0,
                "knowledge",
                "",
                "entity",
            )
        ]


def test_entity_batch_owner_reuses_the_passed_snapshot_and_freezes_payload() -> None:
    path = "wiki/entities/client-script.md"
    snapshot = QueryCorpusSnapshot(
        "active",
        ({"path": path, "title": "Client Script", "frontmatter": {"type": "entity"}},),
        {path: {"type": "entity"}},
        {},
    )
    store = _FakeStore()
    specs = build_store_specs(
        store,  # type: ignore[arg-type]
        "knowledge",
        snapshot=snapshot,
        raw_snapshot=None,
        raw_store=None,
    )

    result = run_entity_batch(
        [
            {
                "canonical_id": "client script",
                "aliases": ["Client Script"],
                "evidence": {"path": "wiki/concepts/catalog.md", "passage_id": "catalog"},
            }
        ],
        "Client Script",
        primary_store=store,  # type: ignore[arg-type]
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(),
        retrieval_mode="lexical",
        hard_budget_tokens=1000,
        confirmation_token=None,
        snapshot=snapshot,
        raw_snapshot=None,
        raw_store=None,
        cancellation=QueryCancellationContext.with_timeout(10.0),
    )

    assert specs[0].snapshot is snapshot
    assert result.payload["status"] == "success"
    assert result.payload["entities"][0]["primary"]["path"] == path

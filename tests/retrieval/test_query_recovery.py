from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from retrieval.query_cancellation import QueryCancelled, QueryCancellationContext
from retrieval.query_recovery import RecoveryCondition, assemble_recovery
from retrieval.retrieval_index import PassageHit
from retrieval.query_snapshot import QueryCorpusSnapshot


class FakeStore:
    def __init__(self, hits: list[PassageHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[list[str], int]] = []

    def passages_for_pages(self, paths: list[str], *, limit_per_page: int) -> list[PassageHit]:
        self.calls.append((paths, limit_per_page))
        return [hit for hit in self.hits if hit.page_path in paths]


def _item(path: str, passage_id: str, score: float, *, source_kind: str = "wiki") -> dict[str, object]:
    return {
        "hit": PassageHit(passage_id, path, Path(path).stem, (), f"evidence {passage_id}", score, "active", "high", source_kind),
        "score": score,
        "fts_rank": 1,
        "title_rank": None,
        "vector_rank": None,
        "vector_score": 0.0,
        "rrf": 0.0,
        "exact": False,
        "graph_score": 0.0,
        "graph_reasons": [],
    }


def test_assembler_owns_stats_context_and_fallback_envelope() -> None:
    first = _item("wiki/concepts/a.md", "a-1", 8.0)
    second = _item("raw/sources/ref.md", "r-1", 7.0, source_kind="raw")
    store = FakeStore(cast(list[PassageHit], [first["hit"], second["hit"]]))

    result = assemble_recovery(
        [first, second],
        condition=RecoveryCondition("raw", ("wiki_zero_results",), "item"),
        candidates=[first, second],
        store=store,  # type: ignore[arg-type]
        raw_store=store,  # type: ignore[arg-type]
        cancellation=QueryCancellationContext.unbounded(),
    )

    assert result.hit_stats["wiki/concepts/a.md"]["max"] == 8.0
    assert result.pool_by_page["raw/sources/ref.md"][0]["hit"].passage_id == "r-1"
    assert result.fallback == {
        "level": "raw",
        "reasons": ["wiki_zero_results"],
        "allowed_source_paths": ["raw/sources/ref.md"],
        "added_token_usage": 0,
    }
    assert [item["hit"].passage_id for item in result.context_items] == ["a-1", "r-1"]


def test_assembler_checks_cancellation_before_context_reads() -> None:
    context = QueryCancellationContext.with_timeout(0)
    store = FakeStore([])

    with pytest.raises(QueryCancelled) as error:
        assemble_recovery(
            [],
            condition=RecoveryCondition(),
            store=store,  # type: ignore[arg-type]
            cancellation=context,
        )

    assert error.value.cancelled_stage == "fallback"
    assert store.calls == []


def test_snapshot_captures_metadata_once_and_is_immutable() -> None:
    pages = [
        {
            "path": "wiki/concepts/one.md",
            "title": "One",
            "frontmatter": {"tags": ["a"]},
            "source_kind": "wiki",
            "corpus": "knowledge",
            "authority": "high",
            "content_hash": "hash",
        }
    ]

    class Store:
        scope = "active"

        def __init__(self) -> None:
            self.calls = 0

        def page_candidates(self):
            self.calls += 1
            return pages

    store = Store()
    snapshot = QueryCorpusSnapshot.capture(store, cancellation=QueryCancellationContext.unbounded())  # type: ignore[arg-type]

    assert store.calls == 1
    assert snapshot.pages[0]["path"] == "wiki/concepts/one.md"
    with pytest.raises(TypeError):
        snapshot.metadata["new"] = {}  # type: ignore[index]

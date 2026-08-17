from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from retrieval.query_cancellation import QueryCancelled, QueryCancellationContext
from retrieval.query_execution_context import _store_metadata
from retrieval.query_pipeline import run_query_v2
from retrieval.query_snapshot import QueryCorpusSnapshot


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_deadline_is_monotonic_and_checkpoint_stops_at_current_stage() -> None:
    clock = FakeClock()
    context = QueryCancellationContext.with_timeout(5.0, clock=clock)

    assert context.checkpoint("vector") == pytest.approx(5.0)
    assert context.stage == "vector"
    clock.advance(5.0)

    with pytest.raises(QueryCancelled) as error:
        context.checkpoint()

    assert error.value.code == "query_timeout"
    assert error.value.cancelled_stage == "vector"
    assert context.cancelled is True


def test_explicit_cancel_preserves_reason_and_stage() -> None:
    context = QueryCancellationContext.unbounded()
    context.set_stage("graph")
    context.cancel("cancelled")

    with pytest.raises(QueryCancelled) as error:
        context.checkpoint()

    assert error.value.code == "query_cancelled"
    assert error.value.cancelled_stage == "graph"
    assert context.reason == "cancelled"


def test_batch_checkpoint_is_sparse_but_zero_index_is_checked() -> None:
    clock = FakeClock()
    context = QueryCancellationContext.with_timeout(1.0, clock=clock)
    clock.advance(1.0)

    with pytest.raises(QueryCancelled):
        context.checkpoint_batch(0, every=16, stage="metadata")


def test_store_metadata_checkpoints_snapshot_pages_without_reordering() -> None:
    pages = tuple(
        {
            "path": f"wiki/concepts/page-{index:02d}.md",
            "frontmatter": {"ordinal": index},
        }
        for index in range(17)
    )
    snapshot = QueryCorpusSnapshot("active", pages, {}, {})
    cancellation = MagicMock()

    metadata = _store_metadata(object(), snapshot=snapshot, cancellation=cancellation)  # type: ignore[arg-type]

    assert list(metadata) == [page["path"] for page in pages]
    assert metadata["wiki/concepts/page-16.md"] == {"ordinal": 16}
    assert cancellation.checkpoint_batch.call_args_list == [
        call(index, every=16, stage="metadata") for index in range(17)
    ]


def test_query_pipeline_does_not_open_the_index_after_status_cancellation(tmp_path) -> None:
    clock = FakeClock()
    context = QueryCancellationContext.with_timeout(0.0, clock=clock)

    with pytest.raises(QueryCancelled) as error:
        run_query_v2(tmp_path, "will not run", cancellation=context)

    assert error.value.cancelled_stage == "status"

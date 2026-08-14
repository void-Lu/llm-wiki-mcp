from __future__ import annotations

from typing import Any

import pytest

from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_snapshot import QueryCorpusSnapshot, _freeze


def test_freeze_recursively_freezes_nested_mappings_and_sequences() -> None:
    source: dict[str, Any] = {
        "nested": {"items": [{"value": 1}], "pair": ("stable",)},
    }

    frozen = _freeze(source)
    source["nested"]["items"][0]["value"] = 2
    source["nested"]["pair"] = ("changed",)

    assert frozen["nested"]["items"][0]["value"] == 1
    assert frozen["nested"]["pair"] == ("stable",)
    with pytest.raises(TypeError):
        frozen["nested"]["items"][0]["value"] = 3


def test_empty_snapshot_has_an_immutable_empty_shape() -> None:
    snapshot = QueryCorpusSnapshot.empty("archive")

    assert snapshot.scope == "archive"
    assert snapshot.pages == ()
    assert dict(snapshot.metadata) == {}
    assert dict(snapshot.provenance) == {}
    with pytest.raises(TypeError):
        snapshot.metadata["new"] = {}  # type: ignore[index]


def test_captured_snapshot_does_not_drift_when_the_source_changes() -> None:
    pages: list[dict[str, Any]] = [
        {
            "path": "wiki/concepts/one.md",
            "frontmatter": {"tags": ["alpha"]},
            "project": "Demo",
            "content_hash": "hash-1",
        }
    ]

    class Store:
        scope = "active"

        def __init__(self) -> None:
            self.calls = 0

        def page_candidates(self) -> list[dict[str, Any]]:
            self.calls += 1
            return pages

    store = Store()
    snapshot = QueryCorpusSnapshot.capture(
        store, cancellation=QueryCancellationContext.unbounded()  # type: ignore[arg-type]
    )

    pages[0]["frontmatter"]["tags"].append("changed")
    pages[0]["project"] = "Changed"
    pages.append({"path": "wiki/concepts/two.md", "frontmatter": {"tags": ["new"]}})

    assert store.calls == 1
    assert len(snapshot.pages) == 1
    assert snapshot.pages[0]["frontmatter"]["tags"] == ("alpha",)
    assert snapshot.metadata["wiki/concepts/one.md"]["tags"] == ("alpha",)
    assert snapshot.provenance["wiki/concepts/one.md"]["project"] == "Demo"

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import retrieval.query_pipeline as query_pipeline
from retrieval.query_pipeline import _graph_expand, _title_candidates, run_query_v2
from retrieval.query_shared import QueryFilters, snapshot_page_eligible
from retrieval.retrieval_index import PassageHit
from runtime.runtime_config import EmbeddingSettings
from tests.helpers import write_test_page
from wiki.wiki_index import refresh_indexes
from wiki.wiki_paths import create_wiki_root


def _page(
    path: str,
    title: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    body: str = "candidate body",
    corpus: str = "active",
    source_kind: str = "wiki",
) -> dict[str, Any]:
    return {
        "path": path,
        "title": title,
        "body": body,
        "frontmatter": frontmatter or {},
        "corpus": corpus,
        "authority": "",
        "source_kind": source_kind,
    }


def _eligibility_fixture() -> list[dict[str, Any]]:
    return [
        _page(
            "wiki/concepts/eligible.md",
            "Eligible candidate",
            frontmatter={"project": "Demo", "type": "concept", "tags": ["eligible"], "lifecycle": "active"},
        ),
        _page(
            "wiki/concepts/other-project.md",
            "Eligible other project",
            frontmatter={"project": "Other", "type": "concept", "tags": ["eligible"], "lifecycle": "active"},
        ),
        _page(
            "wiki/sources/retired.md",
            "Eligible retired namespace",
            frontmatter={"project": "Demo", "type": "concept", "tags": ["eligible"], "lifecycle": "active"},
        ),
        _page(
            "wiki/concepts/retired.md",
            "Eligible retired lifecycle",
            frontmatter={"project": "Demo", "type": "concept", "tags": ["eligible"], "lifecycle": "archived"},
        ),
        _page(
            "wiki/concepts/source-index.md",
            "Eligible source index",
            frontmatter={"project": "Demo", "type": "source_index", "tags": ["eligible"], "lifecycle": "active"},
        ),
    ]


def _metadata(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(page["path"]): page["frontmatter"] for page in pages}


def _filters() -> QueryFilters:
    return QueryFilters(type="concept", tags=("eligible",), path_prefix="wiki/concepts")


def test_snapshot_page_eligible_composes_scope_and_request_contracts() -> None:
    page = _page(
        "wiki/concepts/target.md",
        "Target",
        frontmatter={"project": "Demo", "type": "concept", "tags": ["alpha", "beta"], "lifecycle": "active"},
    )
    metadata = _metadata([page])

    assert snapshot_page_eligible(
        page,
        metadata,
        scope="knowledge",
        project="demo",
        filters=QueryFilters(type="concept", tags=("alpha",), path_prefix="wiki/concepts"),
    ) is True
    assert snapshot_page_eligible(
        page,
        metadata,
        scope="knowledge",
        project="other",
        filters=QueryFilters(type="concept", tags=("alpha",), path_prefix="wiki/concepts"),
    ) is False
    assert snapshot_page_eligible(
        page,
        metadata,
        scope="knowledge",
        project="demo",
        filters=QueryFilters(type="entity"),
    ) is False


@pytest.mark.parametrize(
    ("path", "frontmatter", "scope", "source_kind", "expected"),
    [
        ("raw/sources/reference.txt", {}, "raw", "raw", True),
        ("wiki/concepts/history.md", {}, "history", "raw_chat", True),
        ("wiki/concepts/history.md", {}, "knowledge", "raw_chat", False),
        ("wiki/concepts/archived.md", {"lifecycle": "archived"}, "archive", "wiki", True),
        ("wiki/sources/index.md", {}, "knowledge", "wiki", False),
    ],
)
def test_snapshot_page_eligible_preserves_scope_boundaries(
    path: str,
    frontmatter: dict[str, Any],
    scope: str,
    source_kind: str,
    expected: bool,
) -> None:
    page = _page(path, "Boundary", frontmatter=frontmatter, corpus="history" if scope == "history" else "active", source_kind=source_kind)
    assert snapshot_page_eligible(page, _metadata([page]), scope=scope, project=None, filters=QueryFilters()) is expected


class _SnapshotStore:
    scope = "active"

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.passage_requests: list[list[str]] = []

    def page_candidates(self) -> list[dict[str, Any]]:
        return self.pages

    def passages_for_pages(self, paths: list[str], *, limit_per_page: int = 1) -> list[PassageHit]:
        del limit_per_page
        normalized = sorted(paths)
        self.passage_requests.append(normalized)
        return [
            PassageHit(
                f"p:{path}",
                path,
                next(page["title"] for page in self.pages if page["path"] == path),
                (),
                "",
                0.0,
                "active",
                "",
                "wiki",
            )
            for path in normalized
        ]


def test_title_and_graph_candidate_boundaries_match_the_baseline() -> None:
    pages = _eligibility_fixture()
    metadata = _metadata(pages)
    filters = _filters()
    store = _SnapshotStore(pages)

    title_hits = _title_candidates(
        store,
        metadata,
        "Eligible",
        scope="knowledge",
        project="demo",
        filters=filters,
    )
    graph_candidates, _ = _graph_expand(
        Path("vault"),
        store,
        metadata,
        scope="knowledge",
        project="demo",
        filters=filters,
        seed_scores={page["path"]: 1.0 for page in pages if page["path"].startswith("wiki/")},
        debug=False,
    )

    assert [hit.page_path for hit in title_hits] == ["wiki/concepts/eligible.md"]
    assert sorted(graph_candidates) == ["wiki/concepts/eligible.md"]
    assert store.passage_requests == [["wiki/concepts/eligible.md"], []]


def test_vector_allowlist_uses_the_same_snapshot_page_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for page in _eligibility_fixture():
        if page["path"].startswith("wiki/sources/"):
            continue
        write_test_page(root, page["path"], {"title": page["title"], "generated": True, **page["frontmatter"]}, page["body"])
    refresh_indexes(root)

    captured: list[list[str]] = []

    def fake_vector_hits(*_args: Any, **kwargs: Any) -> tuple[dict[str, tuple[int, float]], list[str]]:
        captured.append(sorted(kwargs["allowed_paths"]))
        return {}, []

    monkeypatch.setattr(query_pipeline, "_vector_hits", fake_vector_hits)
    result = run_query_v2(
        root,
        "no-primary-match",
        scope="knowledge",
        project="demo",
        filters=_filters(),
        top_k=5,
        embedding=EmbeddingSettings(enabled=True),
        retrieval_mode="vector",
    )

    assert result["ok"] is True
    assert captured == [["wiki/concepts/eligible.md"]]

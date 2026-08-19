from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval.candidate_items import candidate_item
from retrieval.discovery import (
    PUBLIC_DISCOVERY_BYTE_LIMIT,
    PUBLIC_DISCOVERY_CANDIDATE_LIMIT,
    discover_catalog,
    discovery_requested_for,
)
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_shared import QueryFilters
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit


@pytest.fixture
def sanitized_discovery_spill_path(tmp_path: Path) -> Path:
    candidates = [
        {
            "canonical_id": f"n/item-{index}",
            "aliases": [f"N/item-{index}"],
            "evidence": {
                "path": "wiki/concepts/module-catalog.md",
                "heading": "Module catalog",
                "excerpt": "sanitized relative fixture content " * 24,
                "passage_id": f"catalog-{index}",
            },
            "evidence_fragments": [
                {"path": "wiki/concepts/module-catalog.md", "excerpt": "safe fixture"}
            ],
        }
        for index in range(766)
    ]
    payload = {
        "pipeline": {
            "discovery": {
                "candidate_entities": candidates,
                "source_pages": [
                    {"path": "wiki/concepts/module-catalog.md", "heading": "Module catalog"}
                ],
            }
        }
    }
    fixture_path = tmp_path / "discovery-spill-sanitized.json"
    fixture_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return fixture_path


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


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("N/record 模块有哪些方法", False),
        ("N/record list methods", False),
        ("N/auth N/search N/record", False),
        ("列出 N/*", True),
        ("N/* 是什么", False),
        ("有哪些模块", True),
    ],
)
def test_discovery_intent_uses_generic_structure_before_listing_words(
    question: str,
    expected: bool,
) -> None:
    assert discovery_requested_for(question) is expected


def test_public_discovery_projection_is_bounded_and_keeps_internal_evidence() -> None:
    path = "wiki/concepts/module-catalog.md"
    lines = "\n".join(f"- N/item-{index} Item {index}" for index in range(60))
    hit = PassageHit(
        "catalog",
        path,
        "Module catalog",
        (),
        f"# Module catalog\n\n{lines}",
        1.0,
        "knowledge",
        "",
        "concept",
    )
    snapshot = QueryCorpusSnapshot(
        "active",
        ({"path": path, "title": "Module catalog", "frontmatter": {"type": "concept"}},),
        {path: {"type": "concept"}},
        {},
    )

    result = discover_catalog(
        store=object(),  # type: ignore[arg-type]
        snapshot=snapshot,
        question="列出 N/*",
        selected=[candidate_item(hit, score=1.0)],
        context_items=[],
        effective_scope="knowledge",
        project=None,
        filters=QueryFilters(),
        cancellation=QueryCancellationContext.with_timeout(10.0),
    )

    discovery = dict(result.discovery)
    encoded_size = len(
        json.dumps(
            discovery,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda value: dict(value) if hasattr(value, "items") else list(value),
        ).encode("utf-8")
    )
    candidates = discovery["candidate_entities"]
    assert encoded_size <= PUBLIC_DISCOVERY_BYTE_LIMIT
    assert len(candidates) <= PUBLIC_DISCOVERY_CANDIDATE_LIMIT
    assert discovery["total_count"] == 60
    assert discovery["returned_count"] == len(candidates)
    assert discovery["truncated"] is True
    assert all("passage_id" not in item for item in candidates)
    assert all("evidence_fragments" not in item for item in candidates)
    assert all("excerpt" not in item["evidence"] for item in candidates)
    assert result.entities[0]["evidence"]["passage_id"] == "catalog"
    assert result.entities[0]["evidence"]["excerpt"]


def test_sanitized_spill_fixture_supports_file_side_small_projection(
    sanitized_discovery_spill_path: Path,
) -> None:
    payload = json.loads(sanitized_discovery_spill_path.read_text(encoding="utf-8"))
    discovery = payload["pipeline"]["discovery"]
    candidates = discovery["candidate_entities"]
    encoded_size = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    assert encoded_size >= 673 * 1024
    assert len(candidates) == 766
    assert all(not Path(item["evidence"]["path"]).is_absolute() for item in candidates)

    target = next(item for item in candidates if item["canonical_id"] == "n/item-42")
    small_projection = {
        "canonical_id": target["canonical_id"],
        "aliases": target["aliases"],
        "evidence": {
            "path": target["evidence"]["path"],
            "heading": target["evidence"]["heading"],
        },
    }
    projection_size = len(
        json.dumps(small_projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    assert projection_size < PUBLIC_DISCOVERY_BYTE_LIMIT
    assert "excerpt" not in small_projection["evidence"]

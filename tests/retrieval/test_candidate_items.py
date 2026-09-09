from __future__ import annotations

from pathlib import Path

from retrieval.candidate_items import CANDIDATE_CORE_KEYS, FUSION_KEYS, candidate_item, with_fusion
from retrieval.retrieval_index import PassageHit


def _item(path: str, passage_id: str, score: float) -> dict[str, object]:
    return candidate_item(
        PassageHit(passage_id, path, Path(path).stem, (), f"evidence {passage_id}", score, "active", "high", "wiki"),
        score=score,
        fts_rank=1,
    )


def test_candidate_item_has_the_canonical_core_shape_and_defaults() -> None:
    hit = PassageHit("p-1", "wiki/concepts/item.md", "Item", (), "body", 1.0, "active", "high", "wiki")

    item = candidate_item(hit, score=2.5)

    assert set(item) == CANDIDATE_CORE_KEYS
    assert item["hit"] is hit
    assert item["score"] == 2.5
    assert item["fts_rank"] is None
    assert item["title_rank"] is None
    assert item["vector_rank"] is None
    assert item["vector_score"] == 0.0
    assert item["rrf"] == 0.0
    assert item["exact"] is False
    assert item["graph_score"] == 0.0
    assert item["graph_reasons"] == []

    # 主查询使用 exact；entity batch 的 exact_match 是独立契约，不能混入核心键集。
    assert "exact_match" not in CANDIDATE_CORE_KEYS


def test_with_fusion_adds_only_the_canonical_fusion_keys() -> None:
    item = _item("wiki/concepts/item.md", "p-1", 2.5)

    fused = with_fusion(
        item,
        coverage_terms=["rag"],
        coverage_ratio=1.0,
        source_local_rank=1,
        source_local_rrf=0.9,
        fusion_score=1.9,
        fusion_source="active",
        fusion_local_position=1,
    )

    assert set(fused) == set(item) | FUSION_KEYS
    assert fused["coverage_terms"] == ["rag"]
    assert fused["fusion_score"] == 1.9

    minimal = with_fusion(
        {"hit": item["hit"], "score": item["score"]},
        coverage_terms=[],
        coverage_ratio=0.0,
        source_local_rank=1,
        source_local_rrf=0.5,
        fusion_score=0.5,
        fusion_source="active",
        fusion_local_position=1,
    )
    assert set(minimal) == {"hit", "score", *FUSION_KEYS}

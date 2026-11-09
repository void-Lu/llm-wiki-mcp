# tests/retrieval/test_bench_weak_label.py
from __future__ import annotations

from retrieval.bench_weak_label import (
    citation_coverage,
    gold_page_in_topk,
    score_topk_evidence,
)
from retrieval.sag2_evidence import HeuristicEvidenceJudge


def test_score_topk_evidence_uses_heuristic_judge_only() -> None:
    hits = [
        {
            "path": "wiki/concepts/invoice.md",
            "title": "Invoice",
            "snippet": "invoice approval workflow documented here",
        },
        {
            "path": "wiki/concepts/other.md",
            "title": "Other",
            "snippet": "unrelated gardening tips",
        },
    ]
    out = score_topk_evidence("invoice approval", hits, top_k=2)
    assert 0.0 <= out["top1_evidence"] <= 1.0
    assert 0.0 <= out["mean_evidence"] <= 1.0
    assert 0.0 <= out["max_evidence"] <= 1.0
    assert out["scorer"] == "HeuristicEvidenceJudge"
    out2 = score_topk_evidence(
        "invoice approval", hits, judge=HeuristicEvidenceJudge(), top_k=2
    )
    assert out2["scorer"] == "HeuristicEvidenceJudge"


def test_gold_page_in_topk_and_citation_coverage() -> None:
    assert gold_page_in_topk(
        ["wiki/a.md", "wiki/b.md"], ["wiki/b.md"], top_k=2
    )
    assert not gold_page_in_topk(
        ["wiki/a.md", "wiki/b.md"], ["wiki/c.md"], top_k=2
    )
    assert not gold_page_in_topk(
        ["wiki/a.md", "wiki/b.md"], ["wiki/b.md"], top_k=1
    )
    covered = citation_coverage(
        {
            "results": [{"path": "wiki/a.md"}, {"path": "wiki/b.md"}],
            "citation_trace": {
                "hits": [
                    {"citation": "[1]", "path": "wiki/a.md"},
                    {"citation": "[2]", "path": "wiki/b.md"},
                ]
            },
        }
    )
    assert covered == 1.0
    assert citation_coverage({"results": [{"path": "wiki/a.md"}]}) is None

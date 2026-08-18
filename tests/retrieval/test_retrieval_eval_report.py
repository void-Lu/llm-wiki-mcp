"""评测指标与报告 owner 的纯计算/输出边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval.retrieval_eval_dataset import Relevance
from retrieval.retrieval_eval_report import (
    calculate_ranking_metrics,
    evaluate_retrieval_gate,
    safe_report_identity,
    write_retrieval_eval_report,
)


def test_report_owner_calculates_page_deduplicated_metrics() -> None:
    metrics = calculate_ranking_metrics(
        ["wiki/a.md", "wiki/a.md", "wiki/b.md"],
        [Relevance("wiki/b.md", 3), Relevance("wiki/a.md", 1)],
        top_k=3,
    )

    assert metrics["hits"] == 2
    assert metrics["recall"] == 1.0
    assert metrics["precision"] == pytest.approx(2 / 3)
    assert metrics["mrr"] == 1.0


def test_report_owner_keeps_gate_identity_bounded() -> None:
    identity = safe_report_identity(
        {
            "metadata": {
                "dataset_id": "fixture",
                "dataset_revision": "rev-1",
                "vault_fingerprint": {"algorithm": "sha256", "value": "digest", "path": "secret"},
                "ranking": {"version": "ranking-v1", "absolute_path": "secret"},
            }
        }
    )

    assert identity == {
        "dataset_id": "fixture",
        "dataset_revision": "rev-1",
        "vault_fingerprint": {"algorithm": "sha256", "value": "digest"},
        "ranking_version": "ranking-v1",
    }


def test_report_owner_redacts_evidence_at_output_boundary(tmp_path: Path) -> None:
    report = {
        "metadata": {
            "dataset_id": "fixture",
            "dataset_revision": "rev-1",
            "parameters": {"top_k": 1},
            "vault_fingerprint": {"value": "digest", "file_count": 1},
            "runtime_provenance": {"package_version": "test", "revision": "rev"},
            "query_v2": {},
        },
        "metrics": {
            "recall_at_k_macro": 1.0,
            "recall_at_k_micro": 1.0,
            "precision_at_k_macro": 1.0,
            "precision_at_k_micro": 1.0,
            "mrr_at_k_macro": 1.0,
            "ndcg_at_k_macro": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "context_budget": {"within_budget": True},
            "filter_correctness": 1.0,
            "p95_latency_ms": 1.0,
        },
        "cases": [
            {
                "id": "case",
                "ranked_paths": [],
                "filter_correct": True,
                "pipeline": {
                    "retrieval_mode": "lexical",
                    "counters": {"vector_hits": 0},
                    "discovery": {"evidence": {"excerpt": "secret"}},
                    "confirmation_token": "secret-token",
                    "fallback": {"level": "raw", "reasons": ["safe_reason", "contains spaces"]},
                    "quality_gate": {
                        "policy_version": "query-quality-policy-v0",
                        "mode": "shadow",
                        "status": "gate_shadow",
                        "candidate_count": 2,
                        "accepted_count": 2,
                        "rejected_count": 0,
                        "score_family_counts": {"main_rrf": 2, "C:\\secret": 9},
                        "reason_counts": {"gate_keep_default": 2, "query secret": 9},
                        "low_sample_buckets": ["score_family:main_rrf", "C:\\secret"],
                        "fail_open": False,
                        "path": "C:\\secret\\candidate.md",
                        "query": "secret query",
                        "body": "secret passage",
                    },
                },
            }
        ],
    }

    output = write_retrieval_eval_report(report, tmp_path / "reports")
    text = Path(output["json"]).read_text(encoding="utf-8")
    assert "secret" not in text
    assert "confirmation_token" not in text
    assert "excerpt" not in text
    assert "safe_reason" in text
    assert "contains spaces" not in text
    assert json.loads(text)["cases"][0]["pipeline"]["retrieval_mode"] == "lexical"
    assert json.loads(text)["cases"][0]["pipeline"]["quality_gate"] == {
        "policy_version": "query-quality-policy-v0",
        "mode": "shadow",
        "status": "gate_shadow",
        "candidate_count": 2,
        "accepted_count": 2,
        "rejected_count": 0,
        "score_family_counts": {"main_rrf": 2},
        "reason_counts": {"gate_keep_default": 2},
        "low_sample_buckets": ["score_family:main_rrf"],
        "fail_open": False,
    }


def test_report_owner_gate_requires_frozen_identity() -> None:
    baseline = {
        "metadata": {
            "dataset_id": "fixture",
            "dataset_revision": "rev-1",
            "vault_fingerprint": {"value": "digest"},
            "ranking": {"version": "ranking-v1"},
            "parameters": {"retrieval_mode": "lexical", "vector_enabled": False},
        },
        "metrics": {
            "recall_at_k_macro": 1.0,
            "ndcg_at_k_macro": 1.0,
            "filter_correctness": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "p95_latency_ms": 1.0,
        },
    }
    candidate = {**baseline, "metadata": {**baseline["metadata"], "dataset_revision": "other"}}

    gate = evaluate_retrieval_gate(candidate, baseline)

    assert gate["passed"] is False
    assert gate["checks"]["dataset_revision"]["passed"] is False

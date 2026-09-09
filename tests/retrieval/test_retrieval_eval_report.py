"""评测指标与报告 owner 的纯计算/输出边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval.retrieval_eval_dataset import Relevance
from retrieval.retrieval_eval_report import (
    calculate_ranking_metrics,
    calculate_quality_gate_metrics,
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

    gate_identity = safe_report_identity(
        {"metadata": {"quality_gate": {"policy_version": "query-quality-policy-v0", "config_hash": "hash-a"}}}
    )
    assert gate_identity == {"gate_policy_version": "query-quality-policy-v0", "gate_config_hash": "hash-a"}


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


def test_report_owner_projects_quality_gate_whitelist_without_paths_or_query(tmp_path: Path) -> None:
    report = {
        "metadata": {
            "dataset_id": "fixture",
            "dataset_revision": "rev-1",
            "parameters": {"top_k": 1},
            "vault_fingerprint": {"value": "digest", "file_count": 1},
            "runtime_provenance": {"package_version": "test", "revision": "runtime"},
            "ranking": {"version": "ranking-v1"},
            "query_v2": {},
        },
        "metrics": {
            "recall_at_k_macro": 1.0,
            "precision_at_k_macro": 1.0,
            "mrr_at_k_macro": 1.0,
            "ndcg_at_k_macro": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "filter_correctness": 1.0,
            "p95_latency_ms": 1.0,
            "context_budget": {"within_budget": True},
        },
        "cases": [
            {
                "id": "case",
                "ranked_paths": [],
                "filter_correct": True,
                "pipeline": {
                    "quality_gate": {
                        "policy_version": "query-quality-policy-v0",
                        "calibration_revision": "cal-1",
                        "mode": "shadow",
                        "status": "gate_shadow",
                        "candidate_count": 2,
                        "accepted_count": 2,
                        "rejected_count": 0,
                        "score_family_counts": {"main_rrf": 2, "not-a-family": 99},
                        "reason_counts": {"gate_keep_default": 2, "secret reason": 99},
                        "selection_counts": {"backoff": 2, "other": 99},
                        "low_sample_buckets": ["main_rrf|knowledge|latin|lexical|wiki", "path/secret"],
                        "fail_open": True,
                        "query": "secret query",
                    },
                    "discovery": {"query": "secret query"},
                },
            }
        ],
    }

    output = write_retrieval_eval_report(report, tmp_path / "reports")
    rendered = json.loads(Path(output["json"]).read_text(encoding="utf-8"))
    quality_gate = rendered["cases"][0]["pipeline"]["quality_gate"]
    assert quality_gate == {
        "accepted_count": 2,
        "candidate_count": 2,
        "calibration_revision": "cal-1",
        "fail_open": True,
        "low_sample_buckets": ["main_rrf|knowledge|latin|lexical|wiki"],
        "mode": "shadow",
        "policy_version": "query-quality-policy-v0",
        "reason_counts": {"gate_keep_default": 2},
        "rejected_count": 0,
        "score_family_counts": {"main_rrf": 2},
        "selection_counts": {"backoff": 2},
        "status": "gate_shadow",
    }
    assert "secret query" not in Path(output["json"]).read_text(encoding="utf-8")


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


def test_report_owner_calculates_shadow_gate_metrics_from_deduped_results() -> None:
    cases = [
        {
            "id": "answerable",
            "answerable": True,
            "relevant": [{"path": "wiki/a.md", "grade": 3}, {"path": "wiki/b.md", "grade": 1}],
            "ranked_paths": ["wiki/a.md", "wiki/a.md", "wiki/b.md", "wiki/c.md"],
            "quality_gate_observation": {
                "mode": "shadow",
                "policy_version": "query-quality-policy-v0",
                "decisions": [
                    {"path": "wiki/a.md", "accepted": False, "reason_code": "gate_reject_score_floor", "score_family": "main_rrf"},
                    {"path": "wiki/b.md", "accepted": False, "reason_code": "gate_reject_low_confidence", "score_family": "wiki_relaxed"},
                    {"path": "wiki/c.md", "accepted": True, "reason_code": "gate_keep_default", "score_family": "main_rrf"},
                ],
            },
        },
        {
            "id": "no-answer-accepted",
            "answerable": False,
            "relevant": [],
            "ranked_paths": ["wiki/x.md", "wiki/y.md"],
            "quality_gate_observation": {
                "mode": "shadow",
                "policy_version": "query-quality-policy-v0",
                "decisions": [
                    {"path": "wiki/x.md", "accepted": True, "reason_code": "gate_keep_default", "score_family": "main_rrf"},
                    {"path": "wiki/y.md", "accepted": False, "reason_code": "gate_reject_low_confidence", "score_family": "main_rrf"},
                ],
            },
        },
        {
            "id": "no-answer-rejected",
            "answerable": False,
            "relevant": [],
            "ranked_paths": ["wiki/z.md"],
            "quality_gate_observation": {
                "mode": "shadow",
                "policy_version": "query-quality-policy-v0",
                "decisions": [
                    {"path": "wiki/z.md", "accepted": False, "reason_code": "gate_reject_score_floor", "score_family": "raw_recovery"},
                ],
            },
        },
    ]

    metrics = calculate_quality_gate_metrics(cases, top_k=3)

    assert metrics["status"] == "proven"
    assert metrics["false_suppression"]["rate"] == 1.0
    assert metrics["false_suppression"]["grade_1_rate"] == 1.0
    assert metrics["would_accept"]["rate"] == pytest.approx(1 / 2)
    assert metrics["rank_churn"]["top1_flip_rate"] == pytest.approx(2 / 3)
    assert metrics["rank_churn"]["top_k_jaccard"] == pytest.approx(5 / 18)
    assert metrics["reason_counts"]["gate_reject_score_floor"] == 2
    assert metrics["score_family_counts"]["main_rrf"] == 4


def test_report_owner_marks_missing_gate_observation_unproven_and_off_not_enabled() -> None:
    missing = calculate_quality_gate_metrics(
        [{"answerable": True, "ranked_paths": ["wiki/a.md"], "relevant": []}],
        gate_enabled=True,
    )
    off = calculate_quality_gate_metrics([], gate_enabled=False)

    assert missing["status"] == "unproven"
    assert missing["false_suppression_rate"] is None
    assert off["status"] == "not_enabled"
    assert off["false_suppression"] is None


def test_report_owner_gate_identity_mismatch_is_unproven() -> None:
    metadata = {
        "dataset_id": "fixture",
        "dataset_revision": "rev-1",
        "vault_fingerprint": {"value": "digest"},
        "ranking": {"version": "ranking-v1"},
        "quality_gate": {"policy_version": "query-quality-policy-v0", "config_hash": "hash-a"},
        "parameters": {"retrieval_mode": "lexical", "vector_enabled": False},
    }
    baseline = {
        "metadata": metadata,
        "metrics": {
            "recall_at_k_macro": 1.0,
            "ndcg_at_k_macro": 1.0,
            "filter_correctness": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "p95_latency_ms": 1.0,
        },
    }
    candidate = {
        **baseline,
        "metadata": {**metadata, "quality_gate": {"policy_version": "query-quality-policy-v0", "config_hash": "hash-b"}},
    }

    gate = evaluate_retrieval_gate(candidate, baseline)

    assert gate["status"] == "unproven"
    assert gate["reason"] == "baseline_identity_mismatch"
    assert gate["checks"]["gate_config_hash"]["passed"] is False


def test_report_owner_drops_case_query_and_paths_at_persistence_boundary(tmp_path: Path) -> None:
    report = {
        "metadata": {
            "dataset_id": "fixture",
            "dataset_revision": "rev-1",
            "parameters": {"top_k": 1},
            "vault_fingerprint": {"value": "digest", "file_count": 1},
            "runtime_provenance": {"package_version": "test", "revision": "rev"},
            "ranking": {"version": "ranking-v1"},
            "query_v2": {},
        },
        "metrics": {
            "recall_at_k_macro": 1.0,
            "precision_at_k_macro": 1.0,
            "mrr_at_k_macro": 1.0,
            "ndcg_at_k_macro": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "filter_correctness": 1.0,
            "p95_latency_ms": 1.0,
            "context_budget": {"within_budget": True},
        },
        "cases": [
            {
                "id": "case",
                "query": "secret query",
                "ranked_paths": ["C:/secret/page.md"],
                "language": "C:/secret",
                "scope": "C:/secret",
                "metrics": {"recall": 1.0, "query": "secret query"},
                "pipeline": {"retrieval_mode": "lexical", "counters": {"vector_hits": 0}},
            }
        ],
    }

    output = write_retrieval_eval_report(report, tmp_path / "reports")
    rendered = json.loads(Path(output["json"]).read_text(encoding="utf-8"))
    text = Path(output["json"]).read_text(encoding="utf-8")

    assert "query" not in rendered["cases"][0]
    assert "ranked_paths" not in rendered["cases"][0]
    assert "C:/secret" not in text
    assert "secret query" not in text
    assert '"query"' not in text

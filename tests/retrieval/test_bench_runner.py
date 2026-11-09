# tests/retrieval/test_bench_runner.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval.bench_runner import extract_recall_at_10, run_bench


def test_extract_recall_at_10_prefers_macro_at_k() -> None:
    assert extract_recall_at_10({"metrics": {"recall_at_k_macro": 0.84}}) == pytest.approx(0.84)
    assert extract_recall_at_10({"metrics": {"ranking": {"recall_at_10": 0.5}}}) == pytest.approx(0.5)
    assert extract_recall_at_10({"metrics": {}}) is None


def test_ci_profile_skips_accuracy_and_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_runner as runner

    work = tmp_path / "work-vault"
    work.mkdir()
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "cases.manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        runner,
        "run_regression_track",
        lambda output_dir, **kwargs: {
            "status": "pass",
            "pytest_exit_code": 0,
            "work_vault": work,
            "dataset": dataset,
            "manifest": manifest,
            "reports": {},
        },
    )

    def fake_weak(**kwargs):  # noqa: ANN001
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        path = out / "weak-label.json"
        path.write_text("{}\n", encoding="utf-8")
        return {
            "mean_top1_evidence": 0.25,
            "mean_topk_evidence": 0.2,
            "gold_in_topk_rate": 0.0,
            "citation_coverage_mean": "unavailable",
            "reports": {"json": str(path)},
        }

    monkeypatch.setattr(runner, "run_weak_label_track", fake_weak)

    codingwork = Path("/tmp/sag2-eval/codingwork-vault-should-not-be-touched-by-ci")
    exit_code, summary = run_bench(
        profile="ci",
        output_dir=tmp_path / "out",
        vault=codingwork,
    )
    assert exit_code == 0
    assert summary["tracks"]["accuracy"]["status"] == "skipped"
    assert summary["tracks"]["latency"]["status"] == "skipped"
    assert summary["tracks"]["weak_label"]["status"] == "report_only"
    assert summary["tracks"]["regression"]["status"] == "pass"
    assert not codingwork.exists()


def test_regression_failure_sets_exit_code_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_runner as runner

    monkeypatch.setattr(
        runner,
        "run_regression_track",
        lambda output_dir, **kwargs: {
            "status": "fail",
            "pytest_exit_code": 1,
            "work_vault": tmp_path,
            "dataset": tmp_path / "c.jsonl",
            "manifest": tmp_path / "c.manifest.json",
            "reports": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "run_weak_label_track",
        lambda **kwargs: {
            "mean_top1_evidence": 0.0,
            "reports": {"json": str(tmp_path / "w.json")},
        },
    )
    (tmp_path / "w.json").write_text("{}\n", encoding="utf-8")
    exit_code, summary = run_bench(profile="ci", output_dir=tmp_path / "out2")
    assert exit_code == 2
    assert summary["tracks"]["regression"]["status"] == "fail"


def test_local_accuracy_gate_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_runner as runner

    monkeypatch.setattr(
        runner,
        "run_regression_track",
        lambda output_dir, **kwargs: {
            "status": "pass",
            "pytest_exit_code": 0,
            "work_vault": tmp_path,
            "dataset": tmp_path / "c.jsonl",
            "manifest": tmp_path / "m.json",
            "reports": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "run_accuracy_track",
        lambda **kwargs: {
            "status": "fail",
            "recall_at_10": 0.5,
            "r10_min": 0.75,
            "reason": "FAIL_LOUD: R@10=0.5 < 0.75",
            "reports": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "run_latency_track",
        lambda **kwargs: {
            "mean_ms": 1.0,
            "p50_ms": 1.0,
            "p95_ms": 1.0,
            "query_count": 5,
            "reports": {"json": str(tmp_path / "lat.json")},
        },
    )
    (tmp_path / "lat.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "run_weak_label_track",
        lambda **kwargs: {
            "mean_top1_evidence": 0.1,
            "reports": {"json": str(tmp_path / "w.json")},
        },
    )
    (tmp_path / "w.json").write_text("{}\n", encoding="utf-8")
    # Pretend vault+dataset exist so local does not skip before calling accuracy.
    vault = tmp_path / "vault"
    vault.mkdir()
    dataset = tmp_path / "gold.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    exit_code, summary = run_bench(
        profile="local",
        output_dir=tmp_path / "out3",
        vault=vault,
        dataset=dataset,
        skip_latency=True,
    )
    assert exit_code == 2
    assert summary["tracks"]["accuracy"]["status"] == "fail"

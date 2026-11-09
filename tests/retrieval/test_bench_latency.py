# tests/retrieval/test_bench_latency.py
from __future__ import annotations

from pathlib import Path

import pytest

from retrieval.bench_latency import load_timing_queries, percentile_ms, run_latency_track

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMING_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bench" / "timing_queries.json"


def test_load_timing_queries_and_percentiles() -> None:
    queries = load_timing_queries(TIMING_FIXTURE)
    assert len(queries) == 5
    assert queries[0]["id"] == "codingwork-0015"
    assert percentile_ms([10.0, 20.0, 30.0, 40.0, 50.0], 50) == 30.0
    assert percentile_ms([10.0, 20.0, 30.0, 40.0, 50.0], 95) >= 50.0


def test_run_latency_track_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_latency as latency_mod

    calls: list[str] = []

    def fake_run_query_v2(root, question, **kwargs):  # noqa: ANN001
        calls.append(question)
        return {
            "ok": True,
            "results": [{"path": "wiki/x.md"}],
            "pipeline": {
                "sag2_hop": {
                    "hops_run": 1,
                    "recovery_reasons": [],
                    "raw_fallback_mode": "none",
                }
            },
        }

    monkeypatch.setattr(latency_mod, "run_query_v2", fake_run_query_v2)
    fixture = tmp_path / "queries.json"
    fixture.write_text(TIMING_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    report = run_latency_track(
        tmp_path / "vault",
        queries_path=fixture,
        output_dir=tmp_path / "latency",
        warmup=True,
    )
    assert report["query_count"] == 5
    assert "mean_ms" in report and "p50_ms" in report and "p95_ms" in report
    assert Path(report["reports"]["json"]).is_file()
    assert len(calls) == 6  # warmup + 5 timed

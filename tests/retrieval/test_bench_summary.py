from __future__ import annotations

import json
from pathlib import Path

from retrieval.bench_summary import build_bench_summary, write_bench_summary

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMING_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bench" / "timing_queries.json"


def test_timing_queries_fixture_has_five_frozen_queries() -> None:
    assert TIMING_FIXTURE.is_file(), "timing_queries.json must be committed (git add -f)"
    payload = json.loads(TIMING_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["dataset_id"] == "bench-timing-v1"
    queries = payload["queries"]
    assert len(queries) == 5
    ids = [q["id"] for q in queries]
    assert ids == [
        "codingwork-0015",
        "codingwork-0001",
        "codingwork-0004",
        "codingwork-0027",
        "codingwork-0020",
    ]
    for q in queries:
        assert isinstance(q["query"], str) and q["query"].strip()
        assert "body" not in q and "content" not in q and "text" not in q
        assert q.get("answerable", True) is True


def test_build_and_write_bench_summary(tmp_path: Path) -> None:
    summary = build_bench_summary(
        profile="ci",
        started_at="2026-11-09T00:00:00+08:00",
        finished_at="2026-11-09T00:01:00+08:00",
        tracks={
            "regression": {
                "status": "pass",
                "pytest_exit_code": 0,
                "reports": {"retrieval_eval": "regression/retrieval-eval.json"},
            },
            "accuracy": {"status": "skipped", "reason": "ci_profile"},
            "latency": {"status": "skipped", "reason": "ci_profile"},
            "weak_label": {
                "status": "report_only",
                "mean_top1_evidence": 0.4,
                "reports": {"json": "weak_label/weak-label.json"},
            },
        },
    )
    assert summary["profile"] == "ci"
    assert summary["tracks"]["accuracy"]["status"] == "skipped"
    assert summary["tracks"]["regression"]["status"] == "pass"
    paths = write_bench_summary(summary, tmp_path)
    assert Path(paths["json"]).name == "bench-summary.json"
    assert Path(paths["markdown"]).name == "bench-summary.md"
    loaded = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert loaded["tracks"]["weak_label"]["status"] == "report_only"
    md = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "regression" in md and "pass" in md

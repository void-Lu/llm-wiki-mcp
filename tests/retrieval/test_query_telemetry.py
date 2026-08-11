from pathlib import Path

import sqlite3

from retrieval.query_telemetry import QueryTelemetry


def test_telemetry_redacts_secret_and_never_stores_passage_body(tmp_path: Path) -> None:
    telemetry = QueryTelemetry(tmp_path)
    telemetry.record(question="token=super-secret what is approval", scope="knowledge", project=None, passage_ids=["passage-1"], fallback_level="none", token_count=4, latency_ms=1)
    assert telemetry.status()["stores_content"] is False
    with sqlite3.connect(telemetry.path) as conn:
        row = conn.execute("SELECT normalized_query_redacted FROM query_telemetry").fetchone()
    assert "super-secret" not in row[0]


def test_cancelled_telemetry_is_finish_once_and_has_no_query_or_passage_evidence(tmp_path: Path) -> None:
    telemetry = QueryTelemetry(tmp_path)
    fields = {
        "question": "token=super-secret what is approval",
        "scope": "knowledge",
        "project": None,
        "passage_ids": ["passage-1"],
        "fallback_level": "",
        "token_count": 0,
        "latency_ms": 2,
        "outcome": "timeout",
        "cancelled_stage": "vector",
        "worker_state": "cancellation_pending",
    }

    assert telemetry.finish_once(**fields) is True
    assert telemetry.finish_once(**fields) is False

    with sqlite3.connect(telemetry.path) as conn:
        row = conn.execute(
            "SELECT normalized_query_redacted, passage_ids, outcome, cancelled_stage, worker_state "
            "FROM query_telemetry"
        ).fetchone()
    assert row == ("", "", "timeout", "vector", "cancellation_pending")

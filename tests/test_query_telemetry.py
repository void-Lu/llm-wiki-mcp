from pathlib import Path

from netsuite_llm_wiki_mcp.query_telemetry import QueryTelemetry


def test_telemetry_redacts_secret_and_never_stores_passage_body(tmp_path: Path) -> None:
    telemetry = QueryTelemetry(tmp_path)
    telemetry.record(question="token=super-secret what is approval", scope="knowledge", project=None, passage_ids=["passage-1"], fallback_level="none", token_count=4, latency_ms=1)
    assert telemetry.status()["stores_content"] is False
    import sqlite3
    with sqlite3.connect(telemetry.path) as conn:
        row = conn.execute("SELECT normalized_query_redacted FROM query_telemetry").fetchone()
    assert "super-secret" not in row[0]

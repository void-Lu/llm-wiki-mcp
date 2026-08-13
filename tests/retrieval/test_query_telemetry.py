from pathlib import Path

import sqlite3
import pytest

from retrieval.query_telemetry import QueryTelemetry, TelemetryReadError, read_completed_candidates, read_event_count


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


def test_read_event_count_is_missing_or_corrupt_database_safe(tmp_path: Path) -> None:
    assert read_event_count(tmp_path) is None

    database = tmp_path / ".llm-wiki" / "state.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not sqlite")
    assert read_event_count(tmp_path) is None


def test_read_completed_candidates_rejects_missing_schema(tmp_path: Path) -> None:
    database = tmp_path / ".llm-wiki" / "state.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE query_telemetry(query_hash TEXT)")

    with pytest.raises(TelemetryReadError) as error:
        read_completed_candidates(tmp_path)
    assert error.value.code == "telemetry_schema_invalid"


def test_read_completed_candidates_supports_legacy_schema_without_outcome(tmp_path: Path) -> None:
    database = tmp_path / ".llm-wiki" / "state.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE query_telemetry("
            "query_hash TEXT NOT NULL, normalized_query_redacted TEXT NOT NULL, at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, scope TEXT NOT NULL, project TEXT NOT NULL, passage_ids TEXT NOT NULL, "
            "fallback_level TEXT NOT NULL, token_count INTEGER NOT NULL, latency_ms REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO query_telemetry VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("hash", "legacy query", "2026-08-11", "2026-12-31", "knowledge", "", "p1", "none", 0, 1.0),
        )

    assert read_completed_candidates(tmp_path) == [
        {
            "query_hash": "hash",
            "normalized_query_redacted": "legacy query",
            "scope": "knowledge",
            "project": "",
            "passage_ids": "p1",
            "at": "2026-08-11",
        }
    ]


def test_query_telemetry_migration_covers_alter_columns(tmp_path: Path) -> None:
    database = tmp_path / ".llm-wiki" / "state.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE query_telemetry("
            "query_hash TEXT NOT NULL, normalized_query_redacted TEXT NOT NULL, at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, scope TEXT NOT NULL, project TEXT NOT NULL, passage_ids TEXT NOT NULL, "
            "fallback_level TEXT NOT NULL, token_count INTEGER NOT NULL, latency_ms REAL NOT NULL, "
            "outcome TEXT NOT NULL DEFAULT 'completed')"
        )
        conn.execute(
            "INSERT INTO query_telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("old-hash", "old query", "2026-08-11", "2026-12-31", "knowledge", "", "p1", "none", 0, 1.0, "completed"),
        )

    telemetry = QueryTelemetry(tmp_path)
    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(query_telemetry)")}
        defaults = conn.execute("SELECT outcome, cancelled_stage, worker_state FROM query_telemetry").fetchone()
    assert columns == {
        "query_hash",
        "normalized_query_redacted",
        "at",
        "expires_at",
        "scope",
        "project",
        "passage_ids",
        "fallback_level",
        "token_count",
        "latency_ms",
        "outcome",
        "cancelled_stage",
        "worker_state",
    }
    assert defaults == ("completed", "", "")

    telemetry.record(
        question="legacy query",
        scope="knowledge",
        project=None,
        passage_ids=["p1"],
        fallback_level="none",
        token_count=1,
        latency_ms=1.0,
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT outcome, cancelled_stage, worker_state FROM query_telemetry").fetchall() == [
            ("completed", "", ""),
            ("completed", "", ""),
        ]
    assert {row["normalized_query_redacted"] for row in read_completed_candidates(tmp_path)} == {"old query", "legacy query"}

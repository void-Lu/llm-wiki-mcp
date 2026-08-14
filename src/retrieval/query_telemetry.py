"""Minimal query telemetry; it deliberately never persists answer evidence."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from wiki.wiki_paths import STATE_DB


_SECRET = re.compile(r"(?i)(?:api[_-]?key|token|password|secret|authorization|cookie)\s*[:=]\s*[^\s]+")
REQUIRED_CANDIDATE_COLUMNS = frozenset(
    {
        "query_hash",
        "normalized_query_redacted",
        "scope",
        "project",
        "passage_ids",
    }
)


class TelemetryReadError(ValueError):
    """A stable error raised by the read-only telemetry projection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def redact_query(query: str) -> str:
    return _SECRET.sub("[REDACTED]", " ".join(query.split()))


def _database_path(root: str | Path) -> Path:
    return Path(root).expanduser().resolve() / STATE_DB


def read_event_count(root: str | Path) -> int | None:
    """Read the telemetry event count without creating or migrating storage."""

    database = _database_path(root)
    if not database.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        row = connection.execute("SELECT count(*) FROM query_telemetry").fetchone()
        return int(row[0]) if row is not None else None
    except (OSError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            connection.close()


def read_completed_candidates(root: str | Path) -> list[dict[str, str]]:
    """Read completed, redacted telemetry candidates through the read-only seam."""

    database = _database_path(root)
    if not database.is_file():
        raise TelemetryReadError("telemetry_missing", "query telemetry database does not exist")

    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        except (OSError, sqlite3.Error) as exc:
            raise TelemetryReadError(
                "telemetry_unreadable",
                "query telemetry database could not be opened read-only",
            ) from exc

        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(query_telemetry)")}
        except sqlite3.Error as exc:
            raise TelemetryReadError("telemetry_unreadable", "query telemetry schema could not be read") from exc
        if not REQUIRED_CANDIDATE_COLUMNS.issubset(columns):
            raise TelemetryReadError("telemetry_schema_invalid", "query telemetry schema is missing required fields")

        outcome_expression = "outcome" if "outcome" in columns else "'completed'"
        try:
            rows = connection.execute(
                "SELECT query_hash, normalized_query_redacted, scope, project, passage_ids, at "
                f"FROM query_telemetry WHERE {outcome_expression} = 'completed' "
                "AND normalized_query_redacted IS NOT NULL AND normalized_query_redacted <> ''"
            ).fetchall()
        except sqlite3.Error as exc:
            raise TelemetryReadError("telemetry_unreadable", "query telemetry could not be read") from exc
    finally:
        if connection is not None:
            connection.close()

    return [
        {
            "query_hash": str(row[0] or ""),
            "normalized_query_redacted": str(row[1] or ""),
            "scope": str(row[2] or ""),
            "project": str(row[3] or ""),
            "passage_ids": str(row[4] or ""),
            "at": str(row[5] or ""),
        }
        for row in rows
    ]


class QueryTelemetry:
    def __init__(self, vault_root: str | Path) -> None:
        root = Path(vault_root).expanduser().resolve()
        self.path = root / STATE_DB
        self._finish_lock = threading.Lock()
        self._finished = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS query_telemetry("
                "query_hash TEXT NOT NULL, normalized_query_redacted TEXT NOT NULL, "
                "at TEXT NOT NULL, expires_at TEXT NOT NULL, scope TEXT NOT NULL, "
                "project TEXT NOT NULL, passage_ids TEXT NOT NULL, fallback_level TEXT NOT NULL, "
                "token_count INTEGER NOT NULL, latency_ms REAL NOT NULL, "
                "outcome TEXT NOT NULL DEFAULT 'completed', cancelled_stage TEXT NOT NULL DEFAULT '', "
                "worker_state TEXT NOT NULL DEFAULT '')"
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(query_telemetry)")}
            if "outcome" not in columns:
                conn.execute("ALTER TABLE query_telemetry ADD COLUMN outcome TEXT NOT NULL DEFAULT 'completed'")
            if "cancelled_stage" not in columns:
                conn.execute("ALTER TABLE query_telemetry ADD COLUMN cancelled_stage TEXT NOT NULL DEFAULT ''")
            if "worker_state" not in columns:
                conn.execute("ALTER TABLE query_telemetry ADD COLUMN worker_state TEXT NOT NULL DEFAULT ''")

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(
        self,
        *,
        question: str,
        scope: str,
        project: str | None,
        passage_ids: Iterable[str],
        fallback_level: str,
        token_count: int,
        latency_ms: float,
        retention_days: int = 90,
        outcome: str = "completed",
        cancelled_stage: str = "",
        worker_state: str = "",
    ) -> None:
        terminal_without_evidence = outcome in {"timeout", "cancelled"}
        redacted = redact_query(question) if not terminal_without_evidence else ""
        safe_passage_ids = () if terminal_without_evidence else passage_ids
        now = datetime.now(UTC)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO query_telemetry("
                "query_hash, normalized_query_redacted, at, expires_at, scope, project, passage_ids, "
                "fallback_level, token_count, latency_ms, outcome, cancelled_stage, worker_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(redacted.encode()).hexdigest(),
                    redacted,
                    now.isoformat(),
                    (now + timedelta(days=retention_days)).isoformat(),
                    scope,
                    project or "",
                    ",".join(sorted(set(safe_passage_ids))),
                    fallback_level,
                    int(token_count),
                    float(latency_ms),
                    outcome,
                    cancelled_stage,
                    worker_state,
                ),
            )

    def finish_once(self, **kwargs: object) -> bool:
        """Persist at most one terminal event for this request."""

        with self._finish_lock:
            if self._finished:
                return False
            self._finished = True
            self.record(**kwargs)  # type: ignore[arg-type]
            return True

    def cleanup(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM query_telemetry WHERE expires_at < ?", (current.isoformat(),))
            return cursor.rowcount

    def status(self) -> dict[str, object]:
        with self._connection() as conn:
            count = conn.execute("SELECT count(*) FROM query_telemetry").fetchone()[0]
        return {"enabled": True, "events": int(count), "stores_content": False}

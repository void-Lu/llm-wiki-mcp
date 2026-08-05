"""Minimal query telemetry; it deliberately never persists answer evidence."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable


_SECRET = re.compile(r"(?i)(?:api[_-]?key|token|password|secret|authorization|cookie)\s*[:=]\s*[^\s]+")


def redact_query(query: str) -> str:
    return _SECRET.sub("[REDACTED]", " ".join(query.split()))


class QueryTelemetry:
    def __init__(self, vault_root: str | Path) -> None:
        root = Path(vault_root).expanduser().resolve()
        self.path = root / ".llm-wiki" / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS query_telemetry(query_hash TEXT NOT NULL, normalized_query_redacted TEXT NOT NULL, at TEXT NOT NULL, expires_at TEXT NOT NULL, scope TEXT NOT NULL, project TEXT NOT NULL, passage_ids TEXT NOT NULL, fallback_level TEXT NOT NULL, token_count INTEGER NOT NULL, latency_ms REAL NOT NULL)")

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(self, *, question: str, scope: str, project: str | None, passage_ids: Iterable[str], fallback_level: str, token_count: int, latency_ms: float, retention_days: int = 90) -> None:
        redacted = redact_query(question)
        now = datetime.now(UTC)
        with self._connection() as conn:
            conn.execute("INSERT INTO query_telemetry VALUES(?,?,?,?,?,?,?,?,?,?)", (hashlib.sha256(redacted.encode()).hexdigest(), redacted, now.isoformat(), (now + timedelta(days=retention_days)).isoformat(), scope, project or "", ",".join(sorted(set(passage_ids))), fallback_level, int(token_count), float(latency_ms)))

    def cleanup(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM query_telemetry WHERE expires_at < ?", (current.isoformat(),))
            return cursor.rowcount

    def status(self) -> dict[str, object]:
        with self._connection() as conn:
            count = conn.execute("SELECT count(*) FROM query_telemetry").fetchone()[0]
        return {"enabled": True, "events": int(count), "stores_content": False}

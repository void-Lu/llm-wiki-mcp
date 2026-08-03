"""Durable, provider-neutral content generation jobs.

The queue deliberately stores prompts and results without credentials.  It is
separate from retrieval/vector caches: deleting either cache must never lose a
generation job.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping


ACTIVE_STATES = {"pending", "leased", "failed"}
TERMINAL_STATES = {"applied", "superseded"}
DISABLED_JOB_TYPES = frozenset({"source_capsule", "chat_source_capsule"})


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _job_id(job_type: str, target_path: str, sources: Mapping[str, str], prompt_version: str, schema_version: int) -> str:
    payload = {"type": job_type, "target": target_path, "sources": sorted(sources.items()), "prompt": prompt_version, "schema": schema_version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class GenerationQueue:
    """The sole owner of job state transitions and their audit events."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.path = self.root / ".llm-wiki" / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS generation_jobs(
                  job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, target_path TEXT NOT NULL,
                  state TEXT NOT NULL, prompt_version TEXT NOT NULL, schema_version INTEGER NOT NULL,
                  expected_target_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT,
                  lease_owner TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  result_hash TEXT, error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS job_sources(
                  job_id TEXT NOT NULL REFERENCES generation_jobs(job_id) ON DELETE CASCADE,
                  source_path TEXT NOT NULL, source_hash TEXT NOT NULL,
                  PRIMARY KEY(job_id, source_path)
                );
                CREATE TABLE IF NOT EXISTS job_events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event TEXT NOT NULL,
                  at TEXT NOT NULL, details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_items(
                  review_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reason TEXT NOT NULL,
                  payload_json TEXT NOT NULL, state TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS generation_jobs_claim ON generation_jobs(state, created_at);
                """
            )

    def create(self, *, job_type: str, target_path: str, sources: Mapping[str, str], prompt_version: str, schema_version: int, expected_target_hash: str | None = None) -> dict[str, Any]:
        if not sources:
            raise ValueError("generation jobs require at least one source")
        if job_type in DISABLED_JOB_TYPES:
            raise ValueError(f"generation job type is disabled: {job_type}")
        job_id = _job_id(job_type, target_path, sources, prompt_version, schema_version)
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                now = _stamp()
                conn.execute("INSERT INTO generation_jobs(job_id,job_type,target_path,state,prompt_version,schema_version,expected_target_hash,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (job_id, job_type, target_path, "pending", prompt_version, schema_version, expected_target_hash, 0, now, now))
                conn.executemany("INSERT INTO job_sources(job_id,source_path,source_hash) VALUES(?,?,?)", [(job_id, path, digest) for path, digest in sorted(sources.items())])
                self._event(conn, job_id, "created", {"sources": sorted(sources), "expected_target_hash": expected_target_hash})
                created = True
            else:
                created = False
            return {"ok": True, "job": self._public(conn, job_id), "created": created}

    def claim(self, owner: str, *, lease_seconds: int = 300) -> dict[str, Any]:
        with self._connection() as conn:
            self._expire(conn)
            disabled_marks = ",".join("?" for _ in DISABLED_JOB_TYPES)
            row = conn.execute(
                f"SELECT job_id FROM generation_jobs WHERE state IN ('pending','failed') AND job_type NOT IN ({disabled_marks}) ORDER BY created_at, job_id LIMIT 1",
                tuple(sorted(DISABLED_JOB_TYPES)),
            ).fetchone()
            if row is None:
                return {"ok": True, "job": None}
            token = uuid.uuid4().hex
            expires = _stamp(_now() + timedelta(seconds=max(1, lease_seconds)))
            now = _stamp()
            conn.execute("UPDATE generation_jobs SET state='leased', lease_token=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=?, error_code=NULL WHERE job_id=?", (token, owner, expires, now, row["job_id"]))
            self._event(conn, row["job_id"], "claimed", {"owner": owner, "expires_at": expires})
            job = self._public(conn, row["job_id"])
            job["lease_token"] = token
            return {"ok": True, "job": job}

    def release(self, job_id: str, lease_token: str) -> dict[str, Any]:
        return self._transition_lease(job_id, lease_token, "pending", "released")

    def fail(self, job_id: str, lease_token: str, error_code: str) -> dict[str, Any]:
        return self._transition_lease(job_id, lease_token, "failed", "failed", error_code=error_code)

    def complete(self, job_id: str, lease_token: str, result_hash: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT state,lease_token,result_hash FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row and row["state"] == "applied" and row["result_hash"] == result_hash:
                return {"ok": True, "idempotent": True, "job": self._public(conn, job_id)}
            if row is None:
                return {"ok": False, "code": "job_not_found"}
            if not self._valid_lease(conn, job_id, lease_token):
                return {"ok": False, "code": "lease_invalid"}
            conn.execute("UPDATE generation_jobs SET state='applied',result_hash=?,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?", (result_hash, _stamp(), job_id))
            self._event(conn, job_id, "applied", {"result_hash": result_hash})
            return {"ok": True, "idempotent": False, "job": self._public(conn, job_id)}

    def supersede_sources(self, source_paths: set[str]) -> list[str]:
        """Supersede all active jobs depending on changed/deleted raw paths."""
        with self._connection() as conn:
            marks = ",".join("?" for _ in source_paths)
            if not marks:
                return []
            rows = conn.execute(f"SELECT DISTINCT j.job_id FROM generation_jobs j JOIN job_sources s ON s.job_id=j.job_id WHERE j.state IN ('pending','leased','failed') AND s.source_path IN ({marks})", tuple(source_paths)).fetchall()
            ids = [str(row["job_id"]) for row in rows]
            for job_id in ids:
                conn.execute("UPDATE generation_jobs SET state='superseded',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?", (_stamp(), job_id))
                self._event(conn, job_id, "superseded", {"changed_sources": sorted(source_paths)})
            return ids

    def supersede_job_types(self, job_types: set[str], *, reason: str = "disabled_job_type") -> list[str]:
        """Terminally cancel queued jobs for a retired generation pipeline."""
        values = sorted({str(item) for item in job_types})
        if not values:
            return []
        marks = ",".join("?" for _ in values)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT job_id FROM generation_jobs WHERE state IN ('pending','leased','failed') AND job_type IN ({marks})",
                tuple(values),
            ).fetchall()
            ids = [str(row["job_id"]) for row in rows]
            for job_id in ids:
                conn.execute(
                    "UPDATE generation_jobs SET state='superseded',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                    (_stamp(), job_id),
                )
                self._event(conn, job_id, "superseded", {"reason": reason, "job_type": "retired"})
            return ids

    def add_review(self, job_id: str, reason: str, payload: Mapping[str, Any]) -> str:
        review_id = uuid.uuid4().hex
        with self._connection() as conn:
            conn.execute("INSERT INTO review_items(review_id,job_id,reason,payload_json,state) VALUES(?,?,?,?,?)", (review_id, job_id, reason, json.dumps(payload, ensure_ascii=False, sort_keys=True), "open"))
            self._event(conn, job_id, "review_required", {"reason": reason, "review_id": review_id})
        return review_id

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            self._expire(conn)
            rows = conn.execute("SELECT state, COUNT(*) AS count FROM generation_jobs GROUP BY state").fetchall()
            return {"ok": True, "path": self.path.relative_to(self.root).as_posix(), "counts": {row["state"]: row["count"] for row in rows}}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            return self._public(conn, job_id) if conn.execute("SELECT 1 FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone() else None

    def lease_is_valid(self, job_id: str, lease_token: str) -> bool:
        with self._connection() as conn:
            return self._valid_lease(conn, job_id, lease_token)

    def _transition_lease(self, job_id: str, token: str, state: str, event: str, *, error_code: str | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            if not self._valid_lease(conn, job_id, token):
                return {"ok": False, "code": "lease_invalid"}
            conn.execute("UPDATE generation_jobs SET state=?,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,error_code=?,updated_at=? WHERE job_id=?", (state, error_code, _stamp(), job_id))
            self._event(conn, job_id, event, {"error_code": error_code} if error_code else {})
            return {"ok": True, "job": self._public(conn, job_id)}

    def _valid_lease(self, conn: sqlite3.Connection, job_id: str, token: str) -> bool:
        row = conn.execute("SELECT 1 FROM generation_jobs WHERE job_id=? AND state='leased' AND lease_token=? AND lease_expires_at>?", (job_id, token, _stamp())).fetchone()
        return row is not None

    def _expire(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT job_id FROM generation_jobs WHERE state='leased' AND lease_expires_at<=?", (_stamp(),)).fetchall()
        for row in rows:
            conn.execute("UPDATE generation_jobs SET state='pending',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?", (_stamp(), row["job_id"]))
            self._event(conn, row["job_id"], "lease_expired", {})

    @staticmethod
    def _event(conn: sqlite3.Connection, job_id: str, event: str, details: Mapping[str, Any]) -> None:
        conn.execute("INSERT INTO job_events(job_id,event,at,details_json) VALUES(?,?,?,?)", (job_id, event, _stamp(), json.dumps(details, ensure_ascii=False, sort_keys=True)))

    def _public(self, conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row is not None
        data = dict(row)
        data["sources"] = {item["source_path"]: item["source_hash"] for item in conn.execute("SELECT source_path,source_hash FROM job_sources WHERE job_id=? ORDER BY source_path", (job_id,))}
        data.pop("lease_token", None)
        return data

"""Raw-source invalidation records for retired generation jobs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any, Iterator


_ACTIVE_STATES = ("pending", "leased", "failed")
_REQUIRED_COLUMNS = {
    "generation_jobs": frozenset({"job_id", "job_type", "target_path", "state", "created_at", "updated_at"}),
    "job_sources": frozenset({"job_id", "source_path", "source_hash"}),
}


def _has_required_schema(connection: sqlite3.Connection) -> bool:
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not required.issubset(columns):
            return False
    return True


class SupersedeRegistry:
    """Own the small registry used to supersede legacy generation jobs."""

    def __init__(self, vault_root: str | Path) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.path = self.root / ".llm-wiki" / "supersede-state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._migrate_from_legacy()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    @classmethod
    def read_status(cls, vault_root: str | Path) -> dict[str, Any]:
        """Read queue-shaped state without initializing or migrating a vault.

        ``wiki_status`` is a read-only boundary.  It may observe a legacy
        queue database, but the first writer remains responsible for the
        one-time migration into the dedicated registry file.
        """

        root = Path(vault_root).expanduser().resolve()
        registry_path = root / ".llm-wiki" / "supersede-state.sqlite3"
        status_path = registry_path.relative_to(root).as_posix()
        database = registry_path if registry_path.is_file() else root / ".llm-wiki" / "state.sqlite3"
        if not database.is_file():
            return {"path": status_path, "total": 0, "counts": {}}

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            if not _has_required_schema(connection):
                return {"path": status_path, "total": 0, "counts": {}}
            rows = connection.execute(
                "SELECT state,COUNT(*) AS count FROM generation_jobs GROUP BY state ORDER BY state"
            ).fetchall()
        except (OSError, sqlite3.Error):
            return {"path": status_path, "total": 0, "counts": {}}
        finally:
            if connection is not None:
                connection.close()

        counts = {str(row["state"]): int(row["count"]) for row in rows}
        return {"path": status_path, "total": sum(counts.values()), "counts": counts}

    def _initialize(self) -> None:
        with self._connection() as connection:
            self._create_schema(connection)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS generation_jobs(
              job_id TEXT PRIMARY KEY,
              job_type TEXT NOT NULL,
              target_path TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_sources(
              job_id TEXT NOT NULL REFERENCES generation_jobs(job_id) ON DELETE CASCADE,
              source_path TEXT NOT NULL,
              source_hash TEXT NOT NULL,
              PRIMARY KEY(job_id, source_path)
            );
            """
        )

    def _migrate_from_legacy(self) -> None:
        """Copy the retired queue's durable rows once, without changing it."""

        legacy_path = self.root / ".llm-wiki" / "state.sqlite3"
        if not legacy_path.exists():
            return

        legacy: sqlite3.Connection | None = None
        try:
            legacy = sqlite3.connect(f"{legacy_path.as_uri()}?mode=ro", uri=True)
            legacy.row_factory = sqlite3.Row
            if not _has_required_schema(legacy):
                return
            jobs = legacy.execute(
                "SELECT job_id,job_type,target_path,state,created_at,updated_at "
                "FROM generation_jobs"
            ).fetchall()
            sources = legacy.execute(
                "SELECT job_id,source_path,source_hash FROM job_sources"
            ).fetchall()
            with self._connection() as connection:
                self._create_schema(connection)
                connection.executemany(
                    "INSERT INTO generation_jobs(job_id,job_type,target_path,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    [tuple(row) for row in jobs],
                )
                connection.executemany(
                    "INSERT INTO job_sources(job_id,source_path,source_hash) VALUES(?,?,?)",
                    [tuple(row) for row in sources],
                )
        except Exception:
            # Legacy state is optional and remains untouched.  The following
            # initialization creates an empty registry when it cannot be read.
            return
        finally:
            if legacy is not None:
                legacy.close()

    def supersede_sources(self, source_paths: set[str]) -> list[str]:
        """Mark active legacy jobs that reference any changed source."""

        if not source_paths:
            return []
        placeholders = ",".join("?" for _ in source_paths)
        states = ",".join("?" for _ in _ACTIVE_STATES)
        parameters = (*_ACTIVE_STATES, *sorted(source_paths))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT jobs.job_id "
                "FROM generation_jobs AS jobs "
                "JOIN job_sources AS sources ON sources.job_id=jobs.job_id "
                f"WHERE jobs.state IN ({states}) AND sources.source_path IN ({placeholders}) "
                "ORDER BY jobs.created_at, jobs.job_id",
                parameters,
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            now = datetime.now(UTC).isoformat()
            for job_id in job_ids:
                connection.execute(
                    "UPDATE generation_jobs SET state='superseded',updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
            return job_ids

    def status(self) -> dict[str, Any]:
        """Return the stable queue-shaped summary for the registry."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT state,COUNT(*) AS count FROM generation_jobs GROUP BY state ORDER BY state"
            ).fetchall()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "path": self.path.relative_to(self.root).as_posix(),
            "total": sum(counts.values()),
            "counts": counts,
        }


__all__ = ["SupersedeRegistry"]

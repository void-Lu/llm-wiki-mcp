"""Read archive state without creating or mutating vault state."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from archive.archive_schema import (
    ARCHIVE_OPERATIONS_STATUS_COLUMNS,
    ARCHIVE_REQUIRED_COLUMNS,
    ARCHIVE_REQUIRED_TABLES,
)
from retrieval.retrieval_index import RetrievalIndexStore
from wiki.wiki_paths import STATE_DB


class ArchiveStatusReader:
    """Own the read-only archive status contract.

    This adapter remains separate from ``ArchiveService`` because status uses
    a read-only connection with ``query_only`` semantics.  A missing or
    incompatible database is an observation, not a request to initialize it.
    """

    def __init__(self, vault_root: str | Path) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.state_path = self.root / STATE_DB

    def status(self) -> dict[str, Any]:
        archive_index = RetrievalIndexStore(self.root, scope="archive").status()
        base: dict[str, Any] = {
            "operations": [],
            "tombstone_count": 0,
            "archive_index": archive_index,
        }
        if not self.state_path.exists():
            return {"ok": True, "state": "missing", "code": "archive_state_missing", **base}

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{self.state_path.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted(ARCHIVE_REQUIRED_TABLES - tables)
            if missing:
                return {
                    "ok": False,
                    "state": "incompatible",
                    "code": "archive_state_incompatible",
                    "missing_tables": missing,
                    **base,
                }
            missing_columns: dict[str, list[str]] = {}
            for table, required in ARCHIVE_REQUIRED_COLUMNS.items():
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                absent = sorted(required - columns)
                if absent:
                    missing_columns[table] = absent
            if missing_columns:
                return {
                    "ok": False,
                    "state": "incompatible",
                    "code": "archive_state_incompatible",
                    "missing_columns": missing_columns,
                    **base,
                }
            operations = [
                dict(row)
                for row in connection.execute(
                    f"SELECT {','.join(ARCHIVE_OPERATIONS_STATUS_COLUMNS)} "
                    "FROM archive_operations ORDER BY updated_at DESC"
                ).fetchall()
            ]
            tombstone_row = connection.execute("SELECT count(*) AS tombstone_count FROM tombstones").fetchone()
            tombstones = int(tombstone_row["tombstone_count"])
            return {
                "ok": True,
                "state": "ready",
                "code": "ready",
                "operations": operations,
                "tombstone_count": tombstones,
                "archive_index": archive_index,
            }
        except sqlite3.OperationalError:
            return {
                "ok": False,
                "state": "unavailable",
                "code": "archive_state_unavailable",
                **base,
            }
        except sqlite3.DatabaseError:
            return {
                "ok": False,
                "state": "incompatible",
                "code": "archive_state_incompatible",
                **base,
            }
        finally:
            if connection is not None:
                connection.close()


__all__ = ["ArchiveStatusReader"]

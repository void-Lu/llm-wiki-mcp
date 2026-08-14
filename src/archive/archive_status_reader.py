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


class ArchiveStatusReader:
    """Own the read-only archive status contract.

    ``ArchiveService`` is intentionally not reused here: its constructor
    creates the state directory and runs schema initialization.  Status is a
    read path, so a missing or incompatible database must remain untouched.
    """

    def __init__(self, vault_root: str | Path) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.state_path = self.root / ".llm-wiki" / "state.sqlite3"

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
            tombstones = int(connection.execute("SELECT count(*) FROM tombstones").fetchone()[0])
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

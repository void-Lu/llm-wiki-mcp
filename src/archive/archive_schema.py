"""Single contract for the archive journal schema and status projections.

Keep ``ARCHIVE_TABLE_DDL`` and ``ARCHIVE_REQUIRED_COLUMNS`` in sync when the
archive journal schema changes.  The status reader uses the same column
contract to distinguish a compatible database from an incomplete one.
"""

from __future__ import annotations


ARCHIVE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS archive_plans(plan_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS archive_operations(operation_id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, operation_type TEXT NOT NULL, state TEXT NOT NULL, plan_hash TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_code TEXT);
CREATE TABLE IF NOT EXISTS archive_operation_items(operation_id TEXT NOT NULL, original_path TEXT NOT NULL, original_hash TEXT NOT NULL, staged_path TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(operation_id, original_path));
CREATE TABLE IF NOT EXISTS archive_events(id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, archive_id TEXT NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones(archive_id TEXT PRIMARY KEY, purged_at TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
"""

ARCHIVE_REQUIRED_TABLES = frozenset(
    {
        "archive_plans",
        "archive_operations",
        "archive_operation_items",
        "archive_events",
        "tombstones",
    }
)
ARCHIVE_REQUIRED_COLUMNS = {
    "archive_plans": frozenset({"plan_id", "payload", "created_at", "expires_at", "used"}),
    "archive_operations": frozenset(
        {"operation_id", "archive_id", "operation_type", "state", "plan_hash", "actor", "created_at", "updated_at", "error_code"}
    ),
    "tombstones": frozenset({"archive_id", "purged_at", "reason", "payload"}),
}
ARCHIVE_OPERATIONS_STATUS_COLUMNS = (
    "operation_id",
    "archive_id",
    "operation_type",
    "state",
    "updated_at",
    "error_code",
)


__all__ = [
    "ARCHIVE_OPERATIONS_STATUS_COLUMNS",
    "ARCHIVE_REQUIRED_COLUMNS",
    "ARCHIVE_REQUIRED_TABLES",
    "ARCHIVE_TABLE_DDL",
]

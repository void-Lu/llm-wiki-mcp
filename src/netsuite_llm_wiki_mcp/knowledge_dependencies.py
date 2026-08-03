"""Persistent raw-to-knowledge dependency and lifecycle projection."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Iterable


VALID_LIFECYCLE = {"active", "stale", "review_required", "superseded", "deprecated", "archived"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeDependencies:
    """A rebuildable dependency projection, independent of retrieval caches."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.path = self.root / ".llm-wiki" / "knowledge-dependencies.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS knowledge_pages(path TEXT PRIMARY KEY, page_hash TEXT NOT NULL, freshness TEXT NOT NULL, lifecycle TEXT NOT NULL, generated INTEGER NOT NULL, maintenance TEXT NOT NULL, replaced_by TEXT, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS source_edges(source_path TEXT NOT NULL, source_hash TEXT NOT NULL, page_path TEXT NOT NULL REFERENCES knowledge_pages(path) ON DELETE CASCADE, PRIMARY KEY(source_path,page_path));
              CREATE INDEX IF NOT EXISTS source_edges_source ON source_edges(source_path);
            """)

    def update_page(
        self,
        path: str,
        page_hash: str,
        sources: dict[str, str],
        *,
        generated: bool,
        maintenance: str = "auto",
        lifecycle: str = "active",
        replaced_by: str | None = None,
        freshness: str = "fresh",
    ) -> None:
        if lifecycle not in VALID_LIFECYCLE:
            raise ValueError("invalid lifecycle")
        if lifecycle == "superseded" and not replaced_by:
            raise ValueError("superseded pages require replaced_by")
        if freshness not in {"fresh", "stale", "review_required"}:
            raise ValueError("invalid freshness")
        with self._connection() as conn:
            conn.execute("INSERT INTO knowledge_pages(path,page_hash,freshness,lifecycle,generated,maintenance,replaced_by,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET page_hash=excluded.page_hash,freshness=excluded.freshness,lifecycle=excluded.lifecycle,generated=excluded.generated,maintenance=excluded.maintenance,replaced_by=excluded.replaced_by,updated_at=excluded.updated_at", (path, page_hash, freshness, lifecycle, int(generated), maintenance, replaced_by, _now()))
            conn.execute("DELETE FROM source_edges WHERE page_path=?", (path,))
            conn.executemany("INSERT INTO source_edges(source_path,source_hash,page_path) VALUES(?,?,?)", [(source, digest, path) for source, digest in sources.items()])

    def source_changed(self, source_path: str, source_hash: str | None = None) -> list[str]:
        """Mark all dependents stale and return their paths exactly once."""
        with self._connection() as conn:
            rows = conn.execute("SELECT DISTINCT p.path FROM knowledge_pages p JOIN source_edges e ON e.page_path=p.path WHERE e.source_path=? AND p.lifecycle IN ('active','stale','review_required')", (source_path,)).fetchall()
            paths = [row["path"] for row in rows]
            for path in paths:
                row = conn.execute("SELECT generated,maintenance FROM knowledge_pages WHERE path=?", (path,)).fetchone()
                freshness = "review_required" if not row["generated"] or row["maintenance"] == "manual" else "stale"
                conn.execute("UPDATE knowledge_pages SET freshness=?,lifecycle=?,updated_at=? WHERE path=?", (freshness, freshness, _now(), path))
            return paths

    def mark_fresh_if_sources(self, path: str, current_sources: dict[str, str]) -> bool:
        with self._connection() as conn:
            stored = {row["source_path"]: row["source_hash"] for row in conn.execute("SELECT source_path,source_hash FROM source_edges WHERE page_path=?", (path,))}
            if stored != current_sources:
                return False
            conn.execute("UPDATE knowledge_pages SET freshness='fresh',lifecycle='active',updated_at=? WHERE path=?", (_now(), path))
            return True

    def dependents(self, source_path: str) -> list[str]:
        with self._connection() as conn:
            return [row["page_path"] for row in conn.execute("SELECT page_path FROM source_edges WHERE source_path=? ORDER BY page_path", (source_path,))]

    def lifecycle(self, path: str, *, state: str, replaced_by: str | None = None) -> dict[str, Any]:
        if state not in VALID_LIFECYCLE:
            return {"ok": False, "code": "invalid_lifecycle"}
        if state == "superseded" and not replaced_by:
            return {"ok": False, "code": "replaced_by_required"}
        with self._connection() as conn:
            row = conn.execute("SELECT generated FROM knowledge_pages WHERE path=?", (path,)).fetchone()
            if row is None:
                return {"ok": False, "code": "page_not_found"}
            if state == "archived":
                return {"ok": False, "code": "archive_service_required"}
            if state in {"superseded", "deprecated"} and not row["generated"]:
                return {"ok": False, "code": "manual_page_protected"}
            conn.execute("UPDATE knowledge_pages SET lifecycle=?,replaced_by=?,updated_at=? WHERE path=?", (state, replaced_by, _now(), path))
            return {"ok": True, "archive_ready": state == "superseded" and bool(replaced_by)}

    def archive_ready(self, path: str) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT lifecycle,replaced_by,generated FROM knowledge_pages WHERE path=?", (path,)).fetchone()
            return bool(row and row["generated"] and row["lifecycle"] == "superseded" and row["replaced_by"])

    def rebuild(self, pages: Iterable[tuple[str, str, dict[str, str], bool, str, str, str | None]]) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM source_edges")
            conn.execute("DELETE FROM knowledge_pages")
        for path, digest, sources, generated, maintenance, lifecycle, replaced_by in pages:
            self.update_page(path, digest, sources, generated=generated, maintenance=maintenance, lifecycle=lifecycle, replaced_by=replaced_by)

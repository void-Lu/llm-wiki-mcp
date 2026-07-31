"""Rebuildable passage-level SQLite retrieval stores.

The active and archive stores intentionally have different physical files.  This
keeps archive data out of ordinary query traffic by construction rather than by
an easy-to-miss SQL filter.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Literal

from netsuite_llm_wiki_mcp.content_redaction import REDACTION_POLICY_VERSION, redact_for_index
from netsuite_llm_wiki_mcp.lexical_analyzer import fts_query, normalize
from netsuite_llm_wiki_mcp.passage_chunker import CHUNK_SCHEMA_VERSION, PassageChunk, chunk_markdown
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter

RETRIEVAL_SCHEMA_VERSION = 2
StoreScope = Literal["active", "archive"]


class RetrievalIndexError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IndexedPage:
    path: str
    title: str
    body: str
    frontmatter: dict[str, Any]
    corpus: str
    authority: str
    lifecycle_status: str
    source_kind: str
    original_content_hash: str
    redacted_content_hash: str
    mtime_ns: int = 0
    size_bytes: int = 0
    session_id: str = ""
    occurred_at: str = ""


@dataclass(frozen=True)
class PassageHit:
    passage_id: str
    page_path: str
    title: str
    heading_path: tuple[str, ...]
    text: str
    score: float
    corpus: str
    authority: str
    source_kind: str


class RetrievalIndexStore:
    def __init__(self, vault_root: str | Path, *, scope: StoreScope = "active", path: str | Path | None = None) -> None:
        if scope not in {"active", "archive"}:
            raise RetrievalIndexError("invalid_store_scope", "scope must be active or archive")
        self.root = Path(vault_root).expanduser().resolve()
        self.scope: StoreScope = scope
        default = self.root / ".llm-wiki" / ("retrieval.sqlite3" if scope == "active" else "archive-index.sqlite3")
        self.path = Path(path).expanduser().resolve() if path is not None else default
        if not self.path.is_relative_to(self.root):
            raise RetrievalIndexError("invalid_index_path", "retrieval store must remain inside the vault")

    def status(self) -> dict[str, object]:
        if not self.path.exists():
            return {"ok": False, "state": "missing", "code": "index_missing", "scope": self.scope}
        try:
            with self._connection(readonly=True) as connection:
                meta = dict(connection.execute("SELECT key, value FROM meta"))
                if int(meta.get("schema_version", "0")) != RETRIEVAL_SCHEMA_VERSION:
                    raise RetrievalIndexError("index_incompatible", "retrieval schema version differs")
                return {"ok": True, "state": meta.get("state", "fresh"), "code": "ready", "scope": self.scope, "page_count": connection.execute("SELECT count(*) FROM pages").fetchone()[0], "passage_count": connection.execute("SELECT count(*) FROM passages").fetchone()[0], "fingerprint": meta.get("fingerprint", ""), "schema_version": RETRIEVAL_SCHEMA_VERSION}
        except (sqlite3.Error, RetrievalIndexError) as exc:
            code = exc.code if isinstance(exc, RetrievalIndexError) else "index_corrupt"
            return {"ok": False, "state": "incompatible", "code": code, "scope": self.scope, "error": str(exc)}

    def build(self, pages: Iterable[IndexedPage]) -> dict[str, object]:
        """Stage a full build and only replace the existing DB after validation."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, staged_name = tempfile.mkstemp(prefix=f"{self.scope}-retrieval-", suffix=".sqlite3", dir=self.path.parent)
        os.close(handle)
        staged = Path(staged_name)
        try:
            connection = self._connect(staged)
            try:
                self._create_schema(connection)
                for page in sorted(pages, key=lambda item: item.path):
                    self._upsert(connection, page)
                fingerprint = self._fingerprint(connection)
                self._set_meta(connection, {"schema_version": str(RETRIEVAL_SCHEMA_VERSION), "scope": self.scope, "chunk_schema_version": str(CHUNK_SCHEMA_VERSION), "redaction_policy_version": REDACTION_POLICY_VERSION, "fingerprint": fingerprint, "state": "fresh"})
                connection.commit()
                connection.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                connection.close()
            os.replace(staged, self.path)
        except Exception:
            try:
                staged.unlink(missing_ok=True)
            except PermissionError:
                pass
            raise
        return {**self.status(), "operation": "build"}

    def update_page(self, page: IndexedPage) -> dict[str, object]:
        if not self.path.exists():
            return {**self.status(), "operation": "update"}
        try:
            with self._connection() as connection:
                self._ensure_schema(connection)
                existing = connection.execute("SELECT redacted_content_hash FROM pages WHERE path = ?", (page.path,)).fetchone()
                if existing and existing[0] == page.redacted_content_hash:
                    return {**self.status(), "operation": "unchanged"}
                self._upsert(connection, page)
                self._set_meta(connection, {"fingerprint": self._fingerprint(connection), "state": "fresh"})
                connection.commit()
            return {**self.status(), "operation": "update"}
        except (sqlite3.Error, RetrievalIndexError) as exc:
            self._mark_stale()
            code = exc.code if isinstance(exc, RetrievalIndexError) else "index_update_failed"
            return {"ok": False, "code": code, "state": "stale", "error": str(exc)}

    def delete_page(self, page_path: str) -> dict[str, object]:
        if not self.path.exists():
            return self.status()
        try:
            with self._connection() as connection:
                self._ensure_schema(connection)
                old_ids = [row[0] for row in connection.execute("SELECT passage_id FROM passages WHERE page_path = ?", (page_path,))]
                if old_ids:
                    marks = ",".join("?" for _ in old_ids)
                    connection.execute(f"DELETE FROM passages_fts WHERE passage_id IN ({marks})", old_ids)
                    connection.execute(f"DELETE FROM vector_dirty WHERE passage_id IN ({marks})", old_ids)
                connection.execute("DELETE FROM pages WHERE path = ?", (page_path,))
                self._set_meta(connection, {"fingerprint": self._fingerprint(connection), "state": "fresh"})
                connection.commit()
            return {**self.status(), "operation": "delete"}
        except sqlite3.Error as exc:
            self._mark_stale(); return {"ok": False, "code": "index_update_failed", "state": "stale", "error": str(exc)}

    def reconcile(self) -> dict[str, object]:
        """Explicitly compare source stats and update changed eligible files only."""
        if self.scope != "active":
            return {"ok": False, "code": "reconcile_unsupported", "error": "archive stores are rebuilt from bundles"}
        if not self.path.exists():
            return self.status()
        known = self._page_stats()
        current = {page.path: page for page in self.iter_vault_pages()}
        changed = [page for path, page in current.items() if known.get(path) != (page.mtime_ns, page.size_bytes)]
        for page in changed:
            update = self.update_page(page)
            if not update.get("ok"):
                return update
        for path in sorted(set(known) - set(current)):
            deleted = self.delete_page(path)
            if not deleted.get("ok"):
                return deleted
        return {**self.status(), "operation": "reconcile", "changed": len(changed), "deleted": len(set(known) - set(current))}

    def search_fts(self, query: str, *, limit: int = 10, project: str | None = None, page_type: str | None = None, tags: list[str] | None = None) -> list[PassageHit]:
        if self.scope != "active" and self.scope != "archive":
            return []
        phrase = fts_query(query)
        if not phrase or not self.path.exists():
            return []
        clauses = ["passages_fts MATCH ?"]
        params: list[object] = [phrase]
        if project:
            clauses.append("pages.project = ?"); params.append(project)
        if page_type:
            clauses.append("pages.page_type = ?"); params.append(page_type)
        if tags:
            for tag in tags:
                clauses.append("pages.frontmatter_json LIKE ?"); params.append(f'%"{tag}"%')
        params.append(limit)
        sql = """
            SELECT passages.passage_id, passages.page_path, pages.title, passages.heading_path_json,
                   passages.text, -bm25(passages_fts, 8.0, 4.0, 3.0, 2.0, 1.0) AS score,
                   pages.corpus, pages.authority, pages.source_kind
            FROM passages_fts JOIN passages ON passages_fts.passage_id = passages.passage_id
            JOIN pages ON pages.path = passages.page_path
            WHERE """ + " AND ".join(clauses) + " ORDER BY score DESC, passages.page_path, passages.ordinal LIMIT ?"
        try:
            with self._connection(readonly=True) as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise RetrievalIndexError("index_corrupt", "retrieval store could not be searched") from exc
        return [PassageHit(row[0], row[1], row[2], tuple(json.loads(row[3])), row[4], float(row[5]), row[6], row[7], row[8]) for row in rows]

    def load_passages(self, ids: Iterable[str]) -> list[PassageHit]:
        values = sorted(set(ids))
        if not values or not self.path.exists():
            return []
        marks = ",".join("?" for _ in values)
        with self._connection(readonly=True) as connection:
            rows = connection.execute(f"SELECT passages.passage_id, passages.page_path, pages.title, passages.heading_path_json, passages.text, 0.0, pages.corpus, pages.authority, pages.source_kind FROM passages JOIN pages ON pages.path=passages.page_path WHERE passages.passage_id IN ({marks}) ORDER BY passages.page_path, passages.ordinal", values).fetchall()
        return [PassageHit(row[0], row[1], row[2], tuple(json.loads(row[3])), row[4], float(row[5]), row[6], row[7], row[8]) for row in rows]

    def passages_for_pages(self, page_paths: Iterable[str], *, limit_per_page: int = 1) -> list[PassageHit]:
        """Return bounded passage projections for already-selected pages.

        This is deliberately a read-only selection helper for graph and
        fallback stages; it never substitutes a whole Markdown page body.
        """
        paths = sorted(set(page_paths))
        if not paths or not self.path.exists() or limit_per_page <= 0:
            return []
        marks = ",".join("?" for _ in paths)
        sql = f"""
            SELECT passage_id, page_path, title, heading_path_json, text, corpus, authority, source_kind
            FROM (
                SELECT passages.passage_id, passages.page_path, pages.title, passages.heading_path_json,
                       passages.text, passages.ordinal, pages.corpus, pages.authority, pages.source_kind,
                       ROW_NUMBER() OVER (PARTITION BY passages.page_path ORDER BY passages.ordinal) AS row_number
                FROM passages JOIN pages ON pages.path = passages.page_path
                WHERE passages.page_path IN ({marks})
            ) WHERE row_number <= ? ORDER BY page_path, row_number
        """
        with self._connection(readonly=True) as connection:
            rows = connection.execute(sql, [*paths, limit_per_page]).fetchall()
        return [PassageHit(row[0], row[1], row[2], tuple(json.loads(row[3])), row[4], 0.0, row[5], row[6], row[7]) for row in rows]

    def page_candidates(self) -> list[dict[str, object]]:
        """Load query projections from the DB without re-reading source files."""
        if not self.path.exists():
            return []
        with self._connection(readonly=True) as connection:
            rows = connection.execute("""
                SELECT pages.path, pages.title, pages.frontmatter_json, pages.source_kind,
                       pages.corpus, pages.project, pages.session_id, pages.occurred_at,
                       pages.redacted_content_hash,
                       group_concat(passages.text, char(10) || char(10))
                FROM pages JOIN passages ON passages.page_path = pages.path
                GROUP BY pages.path ORDER BY pages.path
            """).fetchall()
        return [
            {
                "path": row[0], "title": row[1], "frontmatter": json.loads(row[2]),
                "source_kind": row[3], "corpus": row[4], "project": row[5],
                "session_id": row[6], "occurred_at": row[7],
                "content_hash": row[8], "body": row[9],
            }
            for row in rows
        ]

    def vector_records(self) -> list[dict[str, str]]:
        """Return only passage metadata/text required by explicit vector lifecycle."""
        if not self.path.exists():
            return []
        with self._connection(readonly=True) as connection:
            rows = connection.execute("SELECT passages.passage_id, passages.page_path, passages.content_hash, passages.text, pages.source_kind, pages.corpus FROM passages JOIN pages ON pages.path=passages.page_path ORDER BY passages.page_path, passages.ordinal").fetchall()
        return [{"passage_id": row[0], "page_path": row[1], "content_hash": row[2], "text": row[3], "source_kind": row[4], "corpus": row[5]} for row in rows]

    def iter_vault_pages(self) -> Iterable[IndexedPage]:
        candidates = (
            sorted([*self.root.glob("wiki/**/*.md"), *self.root.glob("raw/sources/chat/**/*")])
            if self.scope == "active"
            else sorted((self.root / "archives" / "bundles").rglob("*.md"))
        )
        return [page for path in candidates if path.is_file() for page in [page_from_file(self.root, path, scope=self.scope)] if page is not None]

    def _connect(self, path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
        target = path or self.path
        if readonly:
            # SQLite URI authorities do not accept Windows' ``\\?\\`` prefix.
            # The database itself is under the short .llm-wiki path, so remove
            # only that transport prefix while retaining the compiler's
            # long-path-safe root for vault traversal.
            uri_target = str(target)
            if uri_target.startswith("\\\\?\\UNC\\"):
                uri_target = "\\\\" + uri_target[len("\\\\?\\UNC\\"):]
            elif uri_target.startswith("\\\\?\\"):
                uri_target = uri_target[len("\\\\?\\"):]
            connection = sqlite3.connect(f"file:{Path(uri_target).as_posix()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(target)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self, path: Path | None = None, *, readonly: bool = False):
        connection = self._connect(path, readonly=readonly)
        try:
            yield connection
            if not readonly:
                connection.commit()
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE pages(path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, page_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, project TEXT NOT NULL, corpus TEXT NOT NULL, authority TEXT NOT NULL, lifecycle_status TEXT NOT NULL, source_kind TEXT NOT NULL, frontmatter_json TEXT NOT NULL, freshness TEXT NOT NULL, original_content_hash TEXT NOT NULL, redacted_content_hash TEXT NOT NULL, session_id TEXT NOT NULL, occurred_at TEXT NOT NULL, mtime_ns INTEGER NOT NULL, size_bytes INTEGER NOT NULL);
            CREATE TABLE passages(passage_id TEXT PRIMARY KEY, page_path TEXT NOT NULL REFERENCES pages(path) ON DELETE CASCADE, heading_path_json TEXT NOT NULL, heading_anchor TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL, normalized_terms TEXT NOT NULL, token_count INTEGER NOT NULL, content_hash TEXT NOT NULL, chunk_schema_version INTEGER NOT NULL);
            CREATE VIRTUAL TABLE passages_fts USING fts5(passage_id UNINDEXED, title, aliases, keywords, heading, normalized_terms);
            CREATE TABLE vector_dirty(passage_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX passages_page_ordinal ON passages(page_path, ordinal);
        """)

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        try:
            schema = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error as exc:
            raise RetrievalIndexError("index_incompatible", "retrieval store is missing metadata") from exc
        if not schema or int(schema[0]) != RETRIEVAL_SCHEMA_VERSION:
            raise RetrievalIndexError("index_incompatible", "retrieval store schema is incompatible; run a full build")

    def _upsert(self, connection: sqlite3.Connection, page: IndexedPage) -> None:
        old_ids = [row[0] for row in connection.execute("SELECT passage_id FROM passages WHERE page_path = ?", (page.path,))]
        if old_ids:
            marks = ",".join("?" for _ in old_ids)
            connection.execute(f"DELETE FROM passages_fts WHERE passage_id IN ({marks})", old_ids)
            connection.execute(f"DELETE FROM vector_dirty WHERE passage_id IN ({marks})", old_ids)
        connection.execute("DELETE FROM pages WHERE path = ?", (page.path,))
        connection.execute("INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (page.path, page.redacted_content_hash, str(page.frontmatter.get("type") or page.source_kind), page.title, str(page.frontmatter.get("summary") or ""), str(page.frontmatter.get("project") or ""), page.corpus, page.authority, page.lifecycle_status, page.source_kind, json.dumps(page.frontmatter, ensure_ascii=False, sort_keys=True, default=str), str(page.frontmatter.get("freshness") or ""), page.original_content_hash, page.redacted_content_hash, page.session_id, page.occurred_at, page.mtime_ns, page.size_bytes))
        aliases = page.frontmatter.get("aliases", [])
        keywords = page.frontmatter.get("tags", [])
        aliases_text = " ".join(map(str, aliases if isinstance(aliases, list) else [aliases]))
        keywords_text = " ".join(map(str, keywords if isinstance(keywords, list) else [keywords]))
        for chunk in chunk_markdown(page.path, page.body):
            self._insert_chunk(connection, chunk, page.title, aliases_text, keywords_text)

    @staticmethod
    def _insert_chunk(connection: sqlite3.Connection, chunk: PassageChunk, title: str, aliases: str, keywords: str) -> None:
        connection.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (chunk.passage_id, chunk.page_path, json.dumps(chunk.heading_path, ensure_ascii=False), chunk.heading_anchor, chunk.ordinal, chunk.text, normalize(chunk.text), chunk.token_count, chunk.content_hash, chunk.chunk_schema_version))
        connection.execute("INSERT INTO passages_fts VALUES (?, ?, ?, ?, ?, ?)", (chunk.passage_id, normalize(title), normalize(aliases), normalize(keywords), normalize(" ".join(chunk.heading_path)), normalize(chunk.text)))
        connection.execute("INSERT INTO vector_dirty VALUES (?, ?, ?)", (chunk.passage_id, chunk.content_hash, "page_updated"))

    def _page_stats(self) -> dict[str, tuple[int, int]]:
        with self._connection(readonly=True) as connection:
            return {str(path): (int(mtime), int(size)) for path, mtime, size in connection.execute("SELECT path, mtime_ns, size_bytes FROM pages")}

    def _fingerprint(self, connection: sqlite3.Connection) -> str:
        rows = connection.execute("SELECT path, redacted_content_hash FROM pages ORDER BY path").fetchall()
        return sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, values: dict[str, str]) -> None:
        connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())

    def _mark_stale(self) -> None:
        if not self.path.exists():
            return
        try:
            with self._connection() as connection:
                self._set_meta(connection, {"state": "stale"}); connection.commit()
        except sqlite3.Error:
            pass


def page_from_file(root: Path, path: Path, *, scope: StoreScope) -> IndexedPage | None:
    rel = path.relative_to(root).as_posix()
    if not eligible_path(rel, scope=scope):
        return None
    raw = path.read_text(encoding="utf-8", errors="ignore")
    frontmatter, body = split_frontmatter(raw) if path.suffix.lower() == ".md" else ({}, raw)
    redacted = redact_for_index(raw)
    _, redacted_body = split_frontmatter(redacted.text) if path.suffix.lower() == ".md" else ({}, redacted.text)
    stat = path.stat()
    is_chat = rel.startswith("raw/sources/chat/")
    if is_chat:
        parts = rel.split("/")
        frontmatter = dict(frontmatter)
        # Chat storage is date/session based, not project based.  Never label
        # a date segment as a project in public historical evidence.
        frontmatter.setdefault("project", "unknown")
    occurred_at = str(frontmatter.get("occurred_at") or frontmatter.get("date") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat())
    return IndexedPage(rel, str(frontmatter.get("title") or path.stem), redacted_body, frontmatter, "history" if is_chat else "knowledge", "low" if is_chat else "high", "active" if scope == "active" else "archived", "raw_chat" if is_chat else str(frontmatter.get("type") or "wiki"), redacted.original_hash, redacted.redacted_hash, stat.st_mtime_ns, stat.st_size, _chat_session(rel) if is_chat else "", occurred_at)


def eligible_path(relative_path: str, *, scope: StoreScope) -> bool:
    path = relative_path.replace("\\", "/")
    if scope == "archive":
        return path.startswith("archives/bundles/")
    if path.startswith("raw/sources/chat/"):
        return True
    if not path.startswith("wiki/") or path.startswith("wiki/archives/"):
        return False
    name = Path(path).name.casefold()
    if name in {"index.md", "overview.md", "log.md"}:
        return False
    return any(path.startswith(prefix) for prefix in ("wiki/concepts/", "wiki/entities/", "wiki/projects/", "wiki/sources/"))


def _chat_session(relative_path: str) -> str:
    parts = relative_path.split("/")
    # Preserve the complete date/session locator after ``.../chat/`` so the
    # citation can lead an operator back to one concrete chat session.
    return "/".join(parts[3:-1]) if len(parts) > 4 else ""

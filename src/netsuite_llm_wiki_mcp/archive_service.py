"""Durable, recoverable archive / restore / purge service.

The archive bundle is the fact of record.  SQLite is deliberately only an
operation journal and rebuildable projection; no code assumes a cross-medium
transaction exists.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from netsuite_llm_wiki_mcp.archive_manifest import content_hash, verify_bundle, write_manifest
from netsuite_llm_wiki_mcp.archive_models import ArchiveError, ArchiveItem, ArchiveManifest, ArchivePlan, Tombstone
from netsuite_llm_wiki_mcp.archive_planner import ArchivePlanner
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore, page_from_file


_TRANSITIONS = {
    "planned": {"staged", "rolling_back", "failed_recoverable"},
    "staged": {"pending", "rolling_back", "failed_recoverable"},
    "pending": {"detaching", "rolling_back", "failed_recoverable"},
    "detaching": {"committed", "rolling_back", "failed_recoverable"},
    "failed_recoverable": {"rolling_back", "detaching"},
    "rolling_back": {"rolled_back"},
    "committed": set(), "rolled_back": set(),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ArchiveService:
    def __init__(self, vault_root: str | Path, *, actor: str = "unknown", fault_at: str | None = None) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.actor = actor
        self.fault_at = fault_at
        self.archive_root = self.root / "archives"
        self.state_path = self.root / ".llm-wiki" / "state.sqlite3"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.planner = ArchivePlanner(self.root)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.state_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn; conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS archive_plans(plan_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS archive_operations(operation_id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, operation_type TEXT NOT NULL, state TEXT NOT NULL, plan_hash TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_code TEXT);
            CREATE TABLE IF NOT EXISTS archive_operation_items(operation_id TEXT NOT NULL, original_path TEXT NOT NULL, original_hash TEXT NOT NULL, staged_path TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(operation_id, original_path));
            CREATE TABLE IF NOT EXISTS archive_events(id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, archive_id TEXT NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tombstones(archive_id TEXT PRIMARY KEY, purged_at TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    def plan_archive(self, targets: str | list[str], *, reason: str = "manual", cascade: bool = False) -> dict[str, Any]:
        plan = self.planner.archive_plan(targets, reason=reason, cascade=cascade, actor=self.actor)
        self._save_plan(plan)
        return self._plan_payload(plan)

    def plan_restore(self, archive_id: str, *, targets: list[str] | None = None) -> dict[str, Any]:
        plan = self.planner.restore_plan(archive_id, targets=targets)
        self._save_plan(plan)
        return self._plan_payload(plan)

    @staticmethod
    def _plan_payload(plan: ArchivePlan) -> dict[str, Any]:
        payload = plan.to_dict()
        if plan.blockers:
            payload["code"] = str(plan.blockers[0].get("code", "archive_plan_blocked"))
        return payload

    def apply(self, plan_id: str) -> dict[str, Any]:
        try:
            plan = self._load_plan(plan_id)
        except ArchiveError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        if plan.blockers:
            return {
                "ok": False,
                "code": str(plan.blockers[0].get("code", "archive_plan_blocked")),
                "blockers": list(plan.blockers),
            }
        if plan.operation_type == "archive":
            return self._apply_archive(plan)
        return self._apply_restore(plan)

    def _save_plan(self, plan: ArchivePlan) -> None:
        payload = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._connection() as conn:
            conn.execute("INSERT INTO archive_plans(plan_id,payload,created_at,expires_at) VALUES(?,?,?,?)", (plan.plan_id, payload, plan.created_at, plan.expires_at))

    def _load_plan(self, plan_id: str) -> ArchivePlan:
        with self._connection() as conn:
            row = conn.execute("SELECT payload,expires_at,used FROM archive_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None: raise ArchiveError("archive_plan_required", "a valid plan_id is required")
            if row["used"]: raise ArchiveError("archive_plan_used", "archive plan was already applied")
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC): raise ArchiveError("archive_plan_expired", "archive plan has expired")
            raw = json.loads(row["payload"])
        items = tuple(ArchiveItem(item["original_path"], item["archive_path"], item["content_hash"], item["kind"], tuple(item.get("dependencies", [])), tuple(item.get("passage_ids", []))) for item in raw["items"])
        return ArchivePlan(raw["plan_id"], raw["operation_type"], raw.get("archive_id"), raw["created_at"], raw["expires_at"], items, raw["plan_hash"], raw.get("reason"), tuple(raw.get("blockers", [])), bool(raw.get("cascade")))

    def _operation(self, plan: ArchivePlan, archive_id: str) -> str:
        operation_id = uuid4().hex
        now = _now()
        with self._connection() as conn:
            conn.execute("INSERT INTO archive_operations VALUES(?,?,?,?,?,?,?,?,NULL)", (operation_id, archive_id, plan.operation_type, "planned", plan.plan_hash, self.actor, now, now))
            conn.executemany("INSERT INTO archive_operation_items VALUES(?,?,?,?,?)", [(operation_id, item.original_path, item.content_hash, item.archive_path, item.kind) for item in plan.items])
        return operation_id

    def _transition(self, operation_id: str, new_state: str) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT state FROM archive_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or new_state not in _TRANSITIONS.get(row["state"], set()):
                raise ArchiveError("invalid_archive_transition", "archive operation cannot transition to requested state")
            conn.execute("UPDATE archive_operations SET state=?,updated_at=? WHERE operation_id=?", (new_state, _now(), operation_id))
        if self.fault_at == new_state:
            raise RuntimeError(f"injected archive fault at {new_state}")

    def _apply_archive(self, plan: ArchivePlan) -> dict[str, Any]:
        # Re-plan before touching the filesystem: plan payload contains the CAS hashes.
        current = self.planner.archive_plan([item.original_path for item in plan.items], reason=plan.reason or "manual", cascade=plan.cascade, actor=self.actor)
        if current.blockers: return {"ok": False, "code": current.blockers[0]["code"], "blockers": list(current.blockers)}
        if {item.original_path: item.content_hash for item in current.items} != {item.original_path: item.content_hash for item in plan.items}:
            return {"ok": False, "code": "archive_plan_drift"}
        archive_id = self._archive_id()
        operation_id = self._operation(plan, archive_id)
        staging = self.archive_root / ".staging" / operation_id
        pending = self.archive_root / ".pending" / operation_id
        try:
            manifest = ArchiveManifest(archive_id, operation_id, plan.reason or "manual", _now(), plan.items, actor=self.actor)
            for item in plan.items:
                source = self.root / item.original_path
                target = staging / item.archive_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if content_hash(target) != item.content_hash: raise ArchiveError("archive_hash_mismatch", "staged archive payload differs")
            write_manifest(staging, manifest)
            self._transition(operation_id, "staged")
            pending.parent.mkdir(parents=True, exist_ok=True); os.replace(staging, pending)
            self._transition(operation_id, "pending")
            recovery = pending / ".recovery"
            for item in plan.items:
                source = self.root / item.original_path
                if not source.exists() or content_hash(source) != item.content_hash: raise ArchiveError("archive_plan_drift", "active payload changed")
                backup = recovery / item.original_path; backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, backup)
            self._transition(operation_id, "detaching")
            for item in plan.items:
                self._delete_active_index(item.original_path)
            final = self.archive_root / "bundles" / archive_id[:4] / archive_id[4:6] / archive_id
            final.parent.mkdir(parents=True, exist_ok=True)
            # recovery payload is intentionally not part of the immutable bundle.
            shutil.rmtree(recovery)
            os.replace(pending, final)
            self._transition(operation_id, "committed")
            self._event(operation_id, archive_id, "archived", {"items": [item.original_path for item in plan.items]})
            self._mark_plan_used(plan.plan_id)
            index = self.rebuild_archive_index()
            return {"ok": True, "operation_id": operation_id, "archive_id": archive_id, "state": "committed", "archive_index": index}
        except Exception as exc:
            self._recover_operation(operation_id)
            return {"ok": False, "code": getattr(exc, "code", "archive_apply_failed"), "error": str(exc), "operation_id": operation_id}

    def _apply_restore(self, plan: ArchivePlan) -> dict[str, Any]:
        if not plan.archive_id: return {"ok": False, "code": "archive_not_found"}
        operation_id: str | None = None
        try:
            bundle = self._bundle(plan.archive_id); manifest = verify_bundle(self.root, bundle)
            if not manifest.restorable: return {"ok": False, "code": "archive_not_restorable"}
            operation_id = self._operation(plan, plan.archive_id)
            for item in plan.items:
                payload, target = bundle / item.archive_path, self.root / item.original_path
                if content_hash(payload) != item.content_hash: raise ArchiveError("archive_hash_mismatch", "archive payload differs")
                if target.exists() and content_hash(target) != item.content_hash: raise ArchiveError("restore_target_conflict", "restore would overwrite changed content")
            staging = self.archive_root / ".staging" / operation_id
            for item in plan.items:
                target = staging / item.archive_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle / item.archive_path, target)
                if content_hash(target) != item.content_hash:
                    raise ArchiveError("archive_hash_mismatch", "staged restore payload differs")
            self._transition(operation_id, "staged")
            for item in plan.items:
                payload, target = staging / item.archive_path, self.root / item.original_path
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Record intent before the atomic move so recovery can remove
                    # only files created by this unfinished restore operation.
                    self._mark_restore_created(operation_id, item.original_path)
                    os.replace(payload, target)
                    self._update_active_index(item.original_path)
            self._transition(operation_id, "pending"); self._transition(operation_id, "detaching"); self._transition(operation_id, "committed")
            shutil.rmtree(staging, ignore_errors=True)
            self._event(operation_id, plan.archive_id, "restored", {"items": [item.original_path for item in plan.items]})
            self._mark_plan_used(plan.plan_id)
            return {"ok": True, "operation_id": operation_id, "archive_id": plan.archive_id, "state": "committed"}
        except Exception as exc:
            if operation_id is not None:
                self._recover_operation(operation_id)
            return {"ok": False, "code": getattr(exc, "code", "restore_apply_failed"), "error": str(exc), "operation_id": operation_id}

    def recover(self) -> dict[str, Any]:
        with self._connection() as conn:
            ids = [row[0] for row in conn.execute("SELECT operation_id FROM archive_operations WHERE state NOT IN ('committed','rolled_back')")]
        return {"ok": True, "recovered": [self._recover_operation(operation_id) for operation_id in ids]}

    def _recover_operation(self, operation_id: str) -> str:
        with self._connection() as conn:
            operation = conn.execute(
                "SELECT archive_id,operation_type,state FROM archive_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            items = list(conn.execute(
                "SELECT original_path,original_hash,staged_path FROM archive_operation_items WHERE operation_id=?", (operation_id,)
            ))
        if operation is None or operation["state"] in {"committed", "rolled_back"}:
            return operation_id
        pending = self.archive_root / ".pending" / operation_id
        staging = self.archive_root / ".staging" / operation_id
        if operation["operation_type"] == "restore":
            for item in items:
                if not str(item["staged_path"]).startswith("restore-created:"):
                    continue
                target = self.root / item["original_path"]
                if target.is_file() and content_hash(target) == item["original_hash"]:
                    target.unlink()
                    try:
                        self._delete_active_index(item["original_path"])
                    except ArchiveError:
                        pass
            shutil.rmtree(staging, ignore_errors=True)
        else:
            # A crash after pending was renamed to the final path but before
            # the committed journal transition must restore active payloads
            # from that not-yet-committed bundle, then discard the bundle.
            final = self.archive_root / "bundles" / operation["archive_id"][:4] / operation["archive_id"][4:6] / operation["archive_id"]
            if final.exists():
                for item in items:
                    payload = final / item["staged_path"]
                    target = self.root / item["original_path"]
                    if payload.is_file() and content_hash(payload) == item["original_hash"] and not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(target.name + f".recover-{operation_id}")
                        shutil.copy2(payload, temporary)
                        os.replace(temporary, target)
                        self._update_active_index(item["original_path"])
                shutil.rmtree(final, ignore_errors=True)
        if pending.exists():
            recovery = pending / ".recovery"
            if recovery.exists():
                for source in sorted(recovery.rglob("*")):
                    if source.is_file():
                        target = self.root / source.relative_to(recovery); target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists(): os.replace(source, target)
                        self._update_active_index(target.relative_to(self.root).as_posix())
            shutil.rmtree(pending, ignore_errors=True)
        if staging.exists(): shutil.rmtree(staging, ignore_errors=True)
        with self._connection() as conn:
            row = conn.execute("SELECT state FROM archive_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row and row["state"] not in {"committed", "rolled_back"}:
                conn.execute("UPDATE archive_operations SET state='rolled_back',updated_at=? WHERE operation_id=?", (_now(), operation_id))
        return operation_id

    def purge(self, archive_id: str, *, authorized: bool = False, forget: bool = False, reason: str = "manual") -> dict[str, Any]:
        if not authorized: return {"ok": False, "code": "purge_not_authorized"}
        try:
            bundle = self._bundle(archive_id); manifest = verify_bundle(self.root, bundle)
            # An archived raw source cannot be purged while it has returned to active use.
            active = [item.original_path for item in manifest.items if (self.root / item.original_path).exists()]
            active.extend(
                dependent
                for item in manifest.items
                if item.kind == "raw"
                for dependent in self.planner.dependencies.dependents(item.original_path)
                if (self.root / dependent).is_file()
            )
            if active: return {"ok": False, "code": "purge_active_reference", "paths": active}
            path_hashes = () if forget else tuple("sha256:" + sha256(item.original_path.encode()).hexdigest() for item in manifest.items)
            tombstone = Tombstone(archive_id, _now(), reason, path_hashes, forget)
            with self._connection() as conn:
                conn.execute("INSERT OR REPLACE INTO tombstones VALUES(?,?,?,?)", (archive_id, tombstone.purged_at, reason, json.dumps(tombstone.to_dict(), ensure_ascii=False)))
            shutil.rmtree(bundle)
            self._event(uuid4().hex, archive_id, "purged", {"count": len(manifest.items), "forget": forget})
            self.rebuild_archive_index()
            return {"ok": True, "archive_id": archive_id, "tombstone": tombstone.to_dict()}
        except ArchiveError as exc: return {"ok": False, "code": exc.code, "error": str(exc)}

    def rebuild_archive_index(self) -> dict[str, Any]:
        store = RetrievalIndexStore(self.root, scope="archive")
        try:
            # Do not let a directory merely placed under bundles become
            # searchable: the manifest and every payload must validate first.
            pages = []
            for bundle in sorted((self.archive_root / "bundles").glob("*/*/*")):
                if not bundle.is_dir():
                    continue
                manifest = verify_bundle(self.root, bundle)
                for item in manifest.items:
                    page = page_from_file(self.root, bundle / item.archive_path, scope="archive")
                    if page is not None:
                        pages.append(page)
            return store.build(pages)
        except Exception as exc:
            # Index is a projection: failure does not roll back a committed bundle.
            try: store._mark_stale()  # noqa: SLF001 - explicit projection stale marker
            except Exception: pass
            return {"ok": False, "code": "archive_index_stale", "state": "stale", "error": str(exc)}

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            operations = [dict(row) for row in conn.execute("SELECT operation_id,archive_id,operation_type,state,updated_at,error_code FROM archive_operations ORDER BY updated_at DESC")]
            tombstones = conn.execute("SELECT count(*) FROM tombstones").fetchone()[0]
        return {"ok": True, "operations": operations, "tombstone_count": tombstones, "archive_index": RetrievalIndexStore(self.root, scope="archive").status()}

    def _bundle(self, archive_id: str) -> Path:
        matches = list((self.archive_root / "bundles").glob(f"*/*/{archive_id}"))
        if len(matches) != 1: raise ArchiveError("archive_not_found", "archive id was not found")
        return matches[0]

    def _archive_id(self) -> str:
        # Lexically sortable and collision-resistant; UTC timestamp remains audit friendly.
        return datetime.now(UTC).strftime("%Y%m%d%H%M%S%f") + uuid4().hex[:10]

    def _mark_plan_used(self, plan_id: str) -> None:
        with self._connection() as conn: conn.execute("UPDATE archive_plans SET used=1 WHERE plan_id=?", (plan_id,))

    def _mark_restore_created(self, operation_id: str, original_path: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE archive_operation_items SET staged_path=? WHERE operation_id=? AND original_path=?",
                (f"restore-created:{original_path}", operation_id, original_path),
            )

    def _event(self, operation_id: str, archive_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connection() as conn: conn.execute("INSERT INTO archive_events(operation_id,archive_id,event_type,created_at,payload) VALUES(?,?,?,?,?)", (operation_id, archive_id, event_type, _now(), json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def _delete_active_index(self, path: str) -> None:
        status = RetrievalIndexStore(self.root).delete_page(path)
        if status.get("ok") is False and status.get("code") not in {"index_missing"}: raise ArchiveError("active_index_update_failed", "could not update active retrieval index")

    def _update_active_index(self, path: str) -> None:
        store = RetrievalIndexStore(self.root)
        if not store.status().get("ok"): return
        page = page_from_file(self.root, self.root / path, scope="active")
        if page is not None:
            result = store.update_page(page)
            if not result.get("ok"): raise ArchiveError("active_index_update_failed", "could not restore active retrieval index")

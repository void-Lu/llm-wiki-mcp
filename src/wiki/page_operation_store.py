"""Independent SQLite owner for page operations and projection stages."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Iterator, Literal, Mapping, overload

from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki.wiki_paths import PAGE_STATE_DB


SCHEMA_VERSION = 1
PAGE_STAGES = ("dependencies", "retrieval", "navigation", "overview", "audit_log")
_OPERATION_STATES = {"prepared", "page_committed", "repair_pending", "completed", "failed_precommit", "conflict"}


class PageOperationError(ValueError):
    """A stable page-state or operation-journal failure."""

    def __init__(self, code: str, message: str = "page operation state is unavailable") -> None:
        super().__init__(message)
        self.code = code


class UpdatePlanError(ValueError):
    """A stable update-plan gate failure."""

    def __init__(self, code: str, message: str = "update plan is not usable", *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


@dataclass(frozen=True)
class PageOperation:
    operation_id: str
    request_key: str
    operation_kind: str
    page_path: str
    base_hash: str | None
    intended_hash: str
    state: str
    created_at: str
    updated_at: str
    error_code: str | None = None
    stages: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "request_key": self.request_key,
            "operation_kind": self.operation_kind,
            "page_path": self.page_path,
            "base_hash": self.base_hash,
            "intended_hash": self.intended_hash,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "stages": {key: dict(value) for key, value in self.stages.items()},
        }


@dataclass(frozen=True)
class UpdatePlan:
    plan_id: str
    page_path: str
    base_hash: str
    intent_hash: str
    expires_at: str
    state: str
    operation_id: str | None
    created_at: str
    updated_at: str
    consumed_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "page_path": self.page_path,
            "base_hash": self.base_hash,
            "intent_hash": self.intent_hash,
            "expires_at": self.expires_at,
            "state": self.state,
            "operation_id": self.operation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "consumed_hash": self.consumed_hash,
        }


def plan_is_expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.now(UTC)
    except ValueError:
        return True


class PageOperationStore:
    """Own the page-state database and nothing from archive state."""

    def __init__(self, vault_root: str | Path, *, busy_timeout_ms: int = 5000):
        self.root = Path(vault_root).expanduser().resolve()
        self.path = self.root / PAGE_STATE_DB
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def database_path(self) -> Path:
        """Return the database path owned by this store."""

        return self.path

    @staticmethod
    def normalize_page_path(value: str) -> str:
        """Normalize a vault-relative page path at the public store boundary."""

        return _normalize_page_path(value)

    @staticmethod
    def safe_stage_result(result: Mapping[str, Any] | None) -> dict[str, object]:
        """Project a stage result to the bounded, non-sensitive journal summary."""

        return _safe_stage_result(result)

    @staticmethod
    def safe_stage_record(stage: Mapping[str, Any] | None) -> dict[str, object]:
        """Project a serialized stage record without exposing arbitrary metadata."""

        if not isinstance(stage, Mapping):
            return {}
        safe: dict[str, object] = {}
        state = stage.get("state")
        if isinstance(state, str):
            safe["state"] = state
        code = stage.get("code")
        if isinstance(code, str):
            safe["code"] = code
        attempts = stage.get("attempts")
        if isinstance(attempts, int) and not isinstance(attempts, bool):
            safe["attempts"] = attempts
        updated_at = stage.get("updated_at")
        if isinstance(updated_at, str):
            safe["updated_at"] = updated_at
        safe_result = _safe_stage_result(stage.get("result"))
        if safe_result:
            safe["result"] = safe_result
        return safe

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a read connection through the store-owned lifecycle."""

        with self._connection() as connection:
            yield connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        except sqlite3.Error as exc:
            raise PageOperationError("page_state_unavailable") from exc
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                raise PageOperationError("page_state_busy") from exc
            except Exception:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS page_state_meta(
                        schema_version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS page_operations(
                        operation_id TEXT PRIMARY KEY,
                        request_key TEXT NOT NULL UNIQUE,
                        operation_kind TEXT NOT NULL,
                        page_path TEXT NOT NULL,
                        base_hash TEXT,
                        intended_hash TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error_code TEXT
                    );
                    CREATE TABLE IF NOT EXISTS page_operation_stages(
                        operation_id TEXT NOT NULL REFERENCES page_operations(operation_id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        state TEXT NOT NULL,
                        code TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        result_json TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(operation_id, stage)
                    );
                    CREATE TABLE IF NOT EXISTS update_plans(
                        plan_id TEXT PRIMARY KEY,
                        page_path TEXT NOT NULL,
                        base_hash TEXT NOT NULL,
                        intent_hash TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        state TEXT NOT NULL,
                        operation_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        consumed_hash TEXT
                    );
                    """
                )
                row = connection.execute("SELECT schema_version FROM page_state_meta LIMIT 1").fetchone()
                if row is None:
                    connection.execute("INSERT INTO page_state_meta(schema_version) VALUES(?)", (SCHEMA_VERSION,))
                elif int(row["schema_version"]) != SCHEMA_VERSION:
                    raise PageOperationError("page_state_incompatible")
                connection.commit()
        except PageOperationError:
            raise
        except sqlite3.DatabaseError as exc:
            raise PageOperationError("page_state_incompatible") from exc

    def create_operation(
        self,
        *,
        request_key: str,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
        operation_id: str | None = None,
    ) -> PageOperation:
        normalized_path = self.normalize_page_path(page_path)
        if not request_key or not intended_hash:
            raise PageOperationError("operation_invalid")
        now = _now()
        operation_id = operation_id or secrets.token_hex(16)
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM page_operations WHERE request_key=?", (request_key,)).fetchone()
            if existing is not None:
                if any(
                    existing[field] != value
                    for field, value in {
                        "operation_kind": operation_kind,
                        "page_path": normalized_path,
                        "base_hash": base_hash,
                        "intended_hash": intended_hash,
                    }.items()
                ):
                    raise PageOperationError("operation_request_conflict")
                return self._load_with_connection(connection, str(existing["operation_id"]))
            try:
                connection.execute(
                    "INSERT INTO page_operations(operation_id,request_key,operation_kind,page_path,base_hash,intended_hash,state,created_at,updated_at,error_code) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (operation_id, request_key, operation_kind, normalized_path, base_hash, intended_hash, "prepared", now, now),
                )
                connection.executemany(
                    "INSERT INTO page_operation_stages(operation_id,stage,state,code,attempts,result_json,updated_at) VALUES(?,?,?,?,?,?,?)",
                    [(operation_id, stage, "pending", None, 0, None, now) for stage in PAGE_STAGES],
                )
            except sqlite3.IntegrityError as exc:
                raise PageOperationError("operation_request_conflict") from exc
            return self._load_with_connection(connection, operation_id)

    def issue_plan(self, page_path: str, base_hash: str, intent_hash: str, *, ttl_seconds: int = 300) -> UpdatePlan:
        if not base_hash or not intent_hash or ttl_seconds <= 0:
            raise UpdatePlanError("update_plan_invalid")
        normalized_path = self.normalize_page_path(page_path)
        now = _now()
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        plan_id = secrets.token_urlsafe(32)
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO update_plans(plan_id,page_path,base_hash,intent_hash,expires_at,state,operation_id,created_at,updated_at,consumed_hash) VALUES(?,?,?,?,?,'issued',NULL,?,?,NULL)",
                    (plan_id, normalized_path, base_hash, intent_hash, expires_at, now, now),
                )
                plan = self._get_plan_with_connection(connection, plan_id)
                assert plan is not None
                return plan
        except PageOperationError as exc:
            raise UpdatePlanError(exc.code) from exc

    def get_plan(self, plan_id: str) -> UpdatePlan | None:
        if not plan_id:
            return None
        with self._connection() as connection:
            return self._get_plan_with_connection(connection, plan_id, missing_ok=True)

    def claim_plan(
        self,
        plan_id: str,
        *,
        page_path: str,
        base_hash: str,
        intent_hash: str,
        operation_id: str,
    ) -> UpdatePlan:
        normalized_path = self.normalize_page_path(page_path)
        with self._transaction() as connection:
            plan = self._get_plan_with_connection(connection, plan_id, missing_ok=True)
            if plan is None:
                raise UpdatePlanError("plan_unknown")
            if plan.page_path != normalized_path:
                raise UpdatePlanError("plan_intent_drift", operation_id=plan.operation_id)
            if plan.state == "consumed":
                if plan.base_hash == base_hash and plan.intent_hash == intent_hash:
                    raise UpdatePlanError("already_applied", operation_id=plan.operation_id)
                raise UpdatePlanError("plan_used", operation_id=plan.operation_id)
            if plan.state == "claimed":
                raise UpdatePlanError("plan_claimed", operation_id=plan.operation_id)
            if plan.state == "expired" or plan_is_expired(plan.expires_at):
                raise UpdatePlanError("plan_expired")
            if plan.base_hash != base_hash:
                raise UpdatePlanError("plan_base_mismatch")
            if plan.intent_hash != intent_hash:
                raise UpdatePlanError("plan_intent_drift")
            updated = connection.execute(
                "UPDATE update_plans SET state='claimed',operation_id=?,updated_at=? WHERE plan_id=? AND state='issued' AND expires_at>? AND page_path=? AND base_hash=? AND intent_hash=?",
                (operation_id, _now(), plan_id, _now(), normalized_path, base_hash, intent_hash),
            ).rowcount
            if updated != 1:
                raise UpdatePlanError("plan_claimed")
            updated_plan = self._get_plan_with_connection(connection, plan_id)
            assert updated_plan is not None
            return updated_plan

    def consume_plan(self, plan_id: str, *, operation_id: str, committed_hash: str) -> UpdatePlan:
        with self._transaction() as connection:
            plan = self._get_plan_with_connection(connection, plan_id, missing_ok=True)
            if plan is None:
                raise UpdatePlanError("plan_unknown")
            if plan.state == "consumed":
                if plan.operation_id == operation_id and plan.consumed_hash == committed_hash:
                    return plan
                raise UpdatePlanError("plan_used", operation_id=plan.operation_id)
            if plan.state != "claimed" or plan.operation_id != operation_id:
                raise UpdatePlanError("plan_claimed", operation_id=plan.operation_id)
            updated = connection.execute(
                "UPDATE update_plans SET state='consumed',consumed_hash=?,updated_at=? WHERE plan_id=? AND state='claimed' AND operation_id=?",
                (committed_hash, _now(), plan_id, operation_id),
            ).rowcount
            if updated != 1:
                raise UpdatePlanError("plan_claimed", operation_id=operation_id)
            updated_plan = self._get_plan_with_connection(connection, plan_id)
            assert updated_plan is not None
            return updated_plan

    def _get_plan_with_connection(self, connection: sqlite3.Connection, plan_id: str, *, missing_ok: bool = False) -> UpdatePlan | None:
        row = connection.execute("SELECT * FROM update_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            if missing_ok:
                return None
            raise UpdatePlanError("plan_unknown")
        return UpdatePlan(
            plan_id=str(row["plan_id"]),
            page_path=str(row["page_path"]),
            base_hash=str(row["base_hash"]),
            intent_hash=str(row["intent_hash"]),
            expires_at=str(row["expires_at"]),
            state=str(row["state"]),
            operation_id=str(row["operation_id"]) if row["operation_id"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            consumed_hash=str(row["consumed_hash"]) if row["consumed_hash"] is not None else None,
        )

    def get_operation(self, operation_id: str) -> PageOperation | None:
        with self._connection() as connection:
            return self._load_with_connection(connection, operation_id, missing_ok=True)

    def get_operation_by_request_key(self, request_key: str) -> PageOperation | None:
        with self._connection() as connection:
            row = connection.execute("SELECT operation_id FROM page_operations WHERE request_key=?", (request_key,)).fetchone()
            return None if row is None else self._load_with_connection(connection, str(row["operation_id"]))

    def set_operation_state(self, operation_id: str, state: str, *, error_code: str | None = None) -> PageOperation:
        if state not in _OPERATION_STATES:
            raise PageOperationError("operation_state_invalid")
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE page_operations SET state=?,error_code=?,updated_at=? WHERE operation_id=?",
                (state, error_code, _now(), operation_id),
            ).rowcount
            if updated != 1:
                raise PageOperationError("operation_not_found")
            return self._load_with_connection(connection, operation_id)

    def record_stage(
        self,
        operation_id: str,
        stage: str,
        state: str,
        *,
        code: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> PageOperation:
        if stage not in PAGE_STAGES:
            raise PageOperationError("stage_invalid")
        if state not in {"pending", "running", "succeeded", "failed"}:
            raise PageOperationError("stage_state_invalid")
        safe_result = self.safe_stage_result(result)
        with self._transaction() as connection:
            row = connection.execute("SELECT attempts FROM page_operation_stages WHERE operation_id=? AND stage=?", (operation_id, stage)).fetchone()
            if row is None:
                raise PageOperationError("operation_not_found")
            attempts = int(row["attempts"]) + (1 if state in {"running", "succeeded", "failed"} else 0)
            connection.execute(
                "UPDATE page_operation_stages SET state=?,code=?,attempts=?,result_json=?,updated_at=? WHERE operation_id=? AND stage=?",
                (state, code, attempts, json.dumps(safe_result, ensure_ascii=False, sort_keys=True) if safe_result else None, _now(), operation_id, stage),
            )
            return self._load_with_connection(connection, operation_id)

    def pending_operations(self) -> list[PageOperation]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM page_operations WHERE state IN ('prepared','page_committed','repair_pending') ORDER BY created_at"
            ).fetchall()
            return [self._load_with_connection(connection, str(row["operation_id"])) for row in rows]

    @overload
    def _load_with_connection(
        self, connection: sqlite3.Connection, operation_id: str, *, missing_ok: Literal[False] = False
    ) -> PageOperation: ...

    @overload
    def _load_with_connection(
        self, connection: sqlite3.Connection, operation_id: str, *, missing_ok: Literal[True]
    ) -> PageOperation | None: ...

    def _load_with_connection(
        self, connection: sqlite3.Connection, operation_id: str, *, missing_ok: bool = False
    ) -> PageOperation | None:
        row = connection.execute("SELECT * FROM page_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            if missing_ok:
                return None
            raise PageOperationError("operation_not_found")
        stage_rows = connection.execute(
            "SELECT stage,state,code,attempts,result_json,updated_at FROM page_operation_stages WHERE operation_id=? ORDER BY stage",
            (operation_id,),
        ).fetchall()
        stages: dict[str, dict[str, Any]] = {}
        for stage in stage_rows:
            raw_result = stage["result_json"]
            try:
                result = json.loads(raw_result) if raw_result else {}
            except (TypeError, json.JSONDecodeError):
                result = {}
            stages[str(stage["stage"])] = {
                "state": str(stage["state"]),
                "code": stage["code"],
                "attempts": int(stage["attempts"]),
                "result": result,
                "updated_at": str(stage["updated_at"]),
            }
        return PageOperation(
            operation_id=str(row["operation_id"]),
            request_key=str(row["request_key"]),
            operation_kind=str(row["operation_kind"]),
            page_path=str(row["page_path"]),
            base_hash=str(row["base_hash"]) if row["base_hash"] is not None else None,
            intended_hash=str(row["intended_hash"]),
            state=str(row["state"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            stages=stages,
        )

def _normalize_page_path(value: str) -> str:
    try:
        return normalize_vault_relative(value.replace("\\", "/"))
    except (AttributeError, LocatorError) as exc:
        raise PageOperationError("path_escape") from exc


def _safe_stage_result(result: Mapping[str, Any] | None) -> dict[str, object]:
    if not isinstance(result, Mapping):
        return {}
    safe: dict[str, object] = {}
    for key in ("ok", "state", "code", "repair_action", "deduplicated", "operation", "affected_count", "written", "changed", "batch"):
        value = result.get(key)
        if isinstance(value, (str, bool, int, float)) and value is not None:
            safe[key] = value
        elif key in {"written", "changed"} and isinstance(value, (list, tuple)):
            paths = [item for item in value if isinstance(item, str) and not Path(item).is_absolute()]
            if len(paths) == len(value):
                safe[key] = paths[:64]
        elif key == "batch" and isinstance(value, Mapping):
            batch: dict[str, object] = {}
            for name in ("kind", "boundary", "affected_count"):
                item = value.get(name)
                if isinstance(item, (str, bool, int, float)) and item is not None:
                    batch[name] = item
            if batch:
                safe[key] = batch
    return safe


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "PAGE_STAGES",
    "PageOperation",
    "PageOperationError",
    "PageOperationStore",
    "SCHEMA_VERSION",
    "UpdatePlan",
    "UpdatePlanError",
    "plan_is_expired",
]

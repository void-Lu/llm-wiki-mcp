"""Opaque, expiring and single-claim update plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import secrets

from wiki.page_operation_store import PageOperationError, PageOperationStore


class UpdatePlanError(ValueError):
    """A stable update-plan gate failure."""

    def __init__(self, code: str, message: str = "update plan is not usable", *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


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


class UpdatePlanStore:
    """Plan owner backed by the page-state database, never by archive state."""

    def __init__(self, vault_root: str | PageOperationStore):
        self.store = vault_root if isinstance(vault_root, PageOperationStore) else PageOperationStore(vault_root)

    @property
    def path(self):
        return self.store.database_path

    def issue(self, page_path: str, base_hash: str, intent_hash: str, *, ttl_seconds: int = 300) -> UpdatePlan:
        if not base_hash or not intent_hash or ttl_seconds <= 0:
            raise UpdatePlanError("update_plan_invalid")
        normalized_path = self.store.normalize_page_path(page_path)
        now = _now()
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        plan_id = secrets.token_urlsafe(32)
        try:
            with self.store.plan_connection() as connection:
                connection.execute(
                    "INSERT INTO update_plans(plan_id,page_path,base_hash,intent_hash,expires_at,state,operation_id,created_at,updated_at,consumed_hash) VALUES(?,?,?,?,?,'issued',NULL,?,?,NULL)",
                    (plan_id, normalized_path, base_hash, intent_hash, expires_at, now, now),
                )
                plan = self._get_with_connection(connection, plan_id)
                assert plan is not None
                return plan
        except PageOperationError as exc:
            raise UpdatePlanError(exc.code) from exc

    create = issue

    def get(self, plan_id: str) -> UpdatePlan | None:
        if not plan_id:
            return None
        with self.store.connection() as connection:
            return self._get_with_connection(connection, plan_id, missing_ok=True)

    inspect = get

    def claim(
        self,
        plan_id: str,
        *,
        page_path: str,
        base_hash: str,
        intent_hash: str,
        operation_id: str,
    ) -> UpdatePlan:
        normalized_path = self.store.normalize_page_path(page_path)
        with self.store.plan_connection() as connection:
            plan = self._get_with_connection(connection, plan_id, missing_ok=True)
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
            if plan.state == "expired" or _expired(plan.expires_at):
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
            updated_plan = self._get_with_connection(connection, plan_id)
            assert updated_plan is not None
            return updated_plan

    def consume(self, plan_id: str, *, operation_id: str, committed_hash: str) -> UpdatePlan:
        with self.store.plan_connection() as connection:
            plan = self._get_with_connection(connection, plan_id, missing_ok=True)
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
            updated_plan = self._get_with_connection(connection, plan_id)
            assert updated_plan is not None
            return updated_plan

    def _get_with_connection(self, connection, plan_id: str, *, missing_ok: bool = False) -> UpdatePlan | None:
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.now(UTC)
    except ValueError:
        return True


__all__ = ["UpdatePlan", "UpdatePlanError", "UpdatePlanStore"]

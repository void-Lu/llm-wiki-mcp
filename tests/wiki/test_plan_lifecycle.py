from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import sqlite3
from pathlib import Path

import pytest

from wiki.page_mutation import PageMutationCoordinator, PlanIntent, PlanLifecycle, plan_intent_hash
from wiki.page_operation_store import PageOperationStore, UpdatePlanError


PAGE_PATH = "wiki/concepts/general/plan.md"
PLAN_INTENT = PlanIntent(body="new", frontmatter={})


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _issued_plan(tmp_path: Path) -> tuple[PageOperationStore, PlanLifecycle, str, str, str]:
    store = PageOperationStore(tmp_path)
    lifecycle = PlanLifecycle(store)
    plan = lifecycle.issue(PAGE_PATH, _hash("old"), PLAN_INTENT)
    return store, lifecycle, plan.plan_id, _hash("old"), _hash("new")


def _claimed_operation(
    tmp_path: Path,
) -> tuple[PageOperationStore, PlanLifecycle, str, str, str, str]:
    store, lifecycle, plan_id, base_hash, intended_hash = _issued_plan(tmp_path)
    operation = store.create_operation(
        request_key="plan-operation",
        operation_kind="update",
        page_path=PAGE_PATH,
        base_hash=base_hash,
        intended_hash=intended_hash,
    )
    assert lifecycle.claim(plan_id, operation, PAGE_PATH, base_hash, plan_intent_hash(PAGE_PATH, base_hash, PLAN_INTENT)) is None
    return store, lifecycle, plan_id, operation.operation_id, base_hash, intended_hash


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("unknown", "plan_unknown"),
        ("expired", "plan_expired"),
        ("claimed_prepared", "plan_claimed"),
        ("consumed_drift", "plan_intent_drift"),
    ],
)
def test_plan_lifecycle_resolve_table(tmp_path: Path, case: str, expected_code: str) -> None:
    store, lifecycle, plan_id, base_hash, intended_hash = _issued_plan(tmp_path)

    if case == "unknown":
        resolution = lifecycle.resolve("missing", page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)
    elif case == "expired":
        connection = sqlite3.connect(store.path)
        connection.execute(
            "UPDATE update_plans SET expires_at=? WHERE plan_id=?",
            (datetime(2000, 1, 1, tzinfo=UTC).isoformat(), plan_id),
        )
        connection.commit()
        connection.close()
        resolution = lifecycle.resolve(plan_id, page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)
    elif case == "claimed_prepared":
        operation = store.create_operation(
            request_key="prepared-operation",
            operation_kind="update",
            page_path=PAGE_PATH,
            base_hash=base_hash,
            intended_hash=intended_hash,
        )
        assert lifecycle.claim(plan_id, operation, PAGE_PATH, base_hash, plan_intent_hash(PAGE_PATH, base_hash, PLAN_INTENT)) is None
        resolution = lifecycle.resolve(plan_id, page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)
    else:
        operation = store.create_operation(
            request_key="consumed-operation",
            operation_kind="update",
            page_path=PAGE_PATH,
            base_hash=base_hash,
            intended_hash=intended_hash,
        )
        assert lifecycle.claim(plan_id, operation, PAGE_PATH, base_hash, plan_intent_hash(PAGE_PATH, base_hash, PLAN_INTENT)) is None
        assert lifecycle.consume(plan_id, operation.operation_id, intended_hash, intended_hash) is None
        resolution = lifecycle.resolve(
            plan_id,
            page_path=PAGE_PATH,
            base_hash=base_hash,
            intended_hash=intended_hash,
            intent=PlanIntent(body="changed", frontmatter={}),
        )

    assert resolution.result is not None
    assert resolution.result.code == expected_code


@pytest.mark.parametrize(
    ("operation_state", "expected_code"),
    [("prepared", "plan_claimed"), ("completed", None)],
)
def test_plan_lifecycle_claimed_recovery_table(
    tmp_path: Path,
    operation_state: str,
    expected_code: str | None,
) -> None:
    store, lifecycle, plan_id, operation_id, base_hash, intended_hash = _claimed_operation(tmp_path)
    store.set_operation_state(operation_id, operation_state)

    resolution = lifecycle.resolve(plan_id, page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)

    if expected_code is None:
        assert resolution.result is None
        assert resolution.operation is not None
        assert resolution.operation.operation_id == operation_id
    else:
        assert resolution.result is not None
        assert resolution.result.code == expected_code
        assert resolution.result.operation_id == operation_id


def test_plan_lifecycle_issued_claimed_consumed_and_replay(tmp_path: Path) -> None:
    store, lifecycle, plan_id, operation_id, base_hash, intended_hash = _claimed_operation(tmp_path)
    store.set_operation_state(operation_id, "page_committed")

    resolved = lifecycle.resolve(plan_id, page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)
    assert resolved.result is None
    assert resolved.operation is not None
    assert resolved.operation.operation_id == operation_id
    assert lifecycle.consume(plan_id, operation_id, intended_hash, intended_hash) is None
    assert store.get_plan(plan_id).state == "consumed"

    replay = lifecycle.resolve(plan_id, page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)
    assert replay.result is not None
    assert replay.result.state == "already_applied"
    assert replay.result.already_applied is True
    assert replay.result.page_hash == intended_hash
    assert replay.consumed_base_applied is True
    assert replay.base_hash == base_hash


@pytest.mark.parametrize(
    ("base_hash", "intent_hash", "expected_code"),
    [
        ("wrong-base", "intent", "plan_base_mismatch"),
        (_hash("old"), "changed", "plan_intent_drift"),
    ],
)
def test_plan_lifecycle_claim_preserves_store_cas_codes(
    tmp_path: Path,
    base_hash: str,
    intent_hash: str,
    expected_code: str,
) -> None:
    store, lifecycle, plan_id, _, intended_hash = _issued_plan(tmp_path)
    operation = store.create_operation(
        request_key=f"claim-{expected_code}",
        operation_kind="update",
        page_path=PAGE_PATH,
        base_hash=_hash("old"),
        intended_hash=intended_hash,
    )

    result = lifecycle.claim(plan_id, operation, PAGE_PATH, base_hash, intent_hash)

    assert result is not None
    assert result.state == "failed_precommit"
    assert result.code == expected_code
    stored = store.get_operation(operation.operation_id)
    assert stored is not None
    assert stored.state == "failed_precommit"


def test_plan_lifecycle_consume_failure_is_repair_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, lifecycle, plan_id, operation_id, _, intended_hash = _claimed_operation(tmp_path)

    def fail_consume(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise UpdatePlanError("page_state_busy")

    monkeypatch.setattr(store, "consume_plan", fail_consume)
    result = lifecycle.consume(plan_id, operation_id, intended_hash, intended_hash)

    assert result is not None
    assert result.ok is True
    assert result.state == "repair_pending"
    assert result.code == "plan_consume_pending"
    assert result.repair_action == "repair_page_operation"
    operation = store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "repair_pending"


def test_plan_lifecycle_reads_durable_state_without_session_cache(tmp_path: Path) -> None:
    store, first, plan_id, base_hash, intended_hash = _issued_plan(tmp_path)
    operation = store.create_operation(
        request_key="durable-operation",
        operation_kind="update",
        page_path=PAGE_PATH,
        base_hash=base_hash,
        intended_hash=intended_hash,
    )
    assert first.claim(plan_id, operation, PAGE_PATH, base_hash, plan_intent_hash(PAGE_PATH, base_hash, PLAN_INTENT)) is None
    store.set_operation_state(operation.operation_id, "completed")

    second = PlanLifecycle(store)
    resolution = second.resolve(plan_id, page_path=PAGE_PATH, base_hash=base_hash, intended_hash=intended_hash, intent=PLAN_INTENT)

    assert resolution.result is None
    assert resolution.operation is not None
    assert resolution.operation.operation_id == operation.operation_id


def test_chat_source_plan_replay_keeps_operation_idempotency_separate(tmp_path: Path) -> None:
    page = tmp_path / "raw/sources/chat/2026/08/17/session/revision-000001.md"
    page.parent.mkdir(parents=True)
    page.write_text("old", encoding="utf-8")
    base_hash = _hash("old")
    store = PageOperationStore(tmp_path)
    lifecycle = PlanLifecycle(store)
    plan = lifecycle.issue(page.relative_to(tmp_path).as_posix(), base_hash, PLAN_INTENT)
    coordinator = PageMutationCoordinator(tmp_path, store=store)

    first = coordinator.write_and_project(
        request_key=plan.plan_id,
        operation_kind="chat_source",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan.plan_id,
        plan_intent=PLAN_INTENT,
    )
    replay = coordinator.write_and_project(
        request_key=plan.plan_id,
        operation_kind="chat_source",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan.plan_id,
        plan_intent=PLAN_INTENT,
    )

    assert first.ok is True
    assert replay.already_applied is True
    assert replay.state == "already_applied"
    assert store.get_plan(plan.plan_id).state == "consumed"

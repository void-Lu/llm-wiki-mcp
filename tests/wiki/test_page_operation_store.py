from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wiki.page_operation_store import PageOperationError, PageOperationStore, UpdatePlanError


def test_page_state_is_separate_from_archive_state_and_does_not_store_body(tmp_path: Path) -> None:
    store = PageOperationStore(tmp_path)
    archive_path = tmp_path / ".llm-wiki" / "state.sqlite3"
    archive_path.touch()
    operation = store.create_operation(
        request_key="request-1",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash="base",
        intended_hash="intended",
    )
    store.record_stage(operation.operation_id, "dependencies", "succeeded", result={"ok": True, "body": "secret", "title": "secret"})

    assert store.path == tmp_path / ".llm-wiki" / "page-state.sqlite3"
    assert store.path != archive_path
    connection = sqlite3.connect(store.path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(page_operations)")}
    stage_json = str(connection.execute("SELECT result_json FROM page_operation_stages").fetchone()[0])
    connection.close()
    assert "body" not in columns and "title" not in columns
    assert "secret" not in stage_json


def test_page_operation_store_exposes_public_path_and_connection_helpers(tmp_path: Path) -> None:
    store = PageOperationStore(tmp_path)

    assert store.database_path == store.path
    assert store.normalize_page_path(r"wiki\concepts\page.md") == "wiki/concepts/page.md"
    with store.connection() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_operation_state_and_stage_results_are_recoverable(tmp_path: Path) -> None:
    store = PageOperationStore(tmp_path)
    operation = store.create_operation(
        request_key="request-1",
        operation_kind="create",
        page_path="wiki/projects/p/plans/page.md",
        base_hash=None,
        intended_hash="intended",
    )
    store.set_operation_state(operation.operation_id, "page_committed")
    store.record_stage(operation.operation_id, "retrieval", "running")
    store.record_stage(operation.operation_id, "retrieval", "failed", code="retrieval_failed", result={"ok": False, "repair_action": "repair_page_operation"})

    loaded = store.get_operation(operation.operation_id)
    assert loaded is not None
    assert loaded.state == "page_committed"
    assert loaded.stages["retrieval"]["attempts"] == 2
    assert loaded.stages["retrieval"]["result"] == {"ok": False, "repair_action": "repair_page_operation"}


def test_stage_result_keeps_bounded_projection_summary_only(tmp_path: Path) -> None:
    store = PageOperationStore(tmp_path)
    operation = store.create_operation(
        request_key="request-summary",
        operation_kind="update",
        page_path="wiki/concepts/page.md",
        base_hash="base",
        intended_hash="intent",
    )
    store.record_stage(
        operation.operation_id,
        "navigation",
        "succeeded",
        result={
            "ok": True,
            "written": ["wiki/index.md", "C:\\secret\\absolute.md"],
            "changed": ["wiki/index.md"],
            "batch": {"kind": "navigation", "boundary": "page-submit", "secret": "hidden"},
            "body": "secret body",
        },
    )

    loaded = store.get_operation(operation.operation_id)
    assert loaded is not None
    assert loaded.stages["navigation"]["result"] == {
        "ok": True,
        "changed": ["wiki/index.md"],
        "batch": {"boundary": "page-submit", "kind": "navigation"},
    }


def test_stage_result_keeps_relative_navigation_changes(tmp_path: Path) -> None:
    store = PageOperationStore(tmp_path)
    operation = store.create_operation(
        request_key="request-navigation-changes",
        operation_kind="update",
        page_path="wiki/concepts/page.md",
        base_hash="base",
        intended_hash="intent",
    )
    store.record_stage(
        operation.operation_id,
        "navigation",
        "succeeded",
        result={"ok": True, "changed": ["wiki/concepts/index.md", "C:\\secret\\index.md"]},
    )

    loaded = store.get_operation(operation.operation_id)
    assert loaded is not None
    assert loaded.stages["navigation"]["result"] == {"ok": True}


def test_page_state_schema_version_mismatch_is_rejected(tmp_path: Path) -> None:
    store = PageOperationStore(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE page_state_meta SET schema_version=999")
    connection.commit()
    connection.close()

    with pytest.raises(PageOperationError) as error:
        PageOperationStore(tmp_path)
    assert error.value.code == "page_state_incompatible"


def test_update_plan_is_opaque_single_claim_and_single_consume(tmp_path: Path) -> None:
    store = PageOperationStore(str(tmp_path))
    plan = store.issue_plan("wiki/concepts/general/page.md", "base", "intent", ttl_seconds=300)
    assert len(plan.plan_id) >= 40
    assert "body" not in plan.to_dict()

    operation = store.create_operation(
        request_key=plan.plan_id,
        operation_kind="update",
        page_path=plan.page_path,
        base_hash=plan.base_hash,
        intended_hash="page-hash",
    )
    claimed = store.claim_plan(
        plan.plan_id,
        page_path=plan.page_path,
        base_hash="base",
        intent_hash="intent",
        operation_id=operation.operation_id,
    )
    assert claimed.state == "claimed"
    with pytest.raises(UpdatePlanError) as claim_error:
        store.claim_plan(
            plan.plan_id,
            page_path=plan.page_path,
            base_hash="base",
            intent_hash="intent",
            operation_id="another-operation",
        )
    assert claim_error.value.code == "plan_claimed"

    consumed = store.consume_plan(plan.plan_id, operation_id=operation.operation_id, committed_hash="page-hash")
    assert consumed.state == "consumed"
    assert store.consume_plan(plan.plan_id, operation_id=operation.operation_id, committed_hash="page-hash").state == "consumed"
    with pytest.raises(UpdatePlanError) as used_error:
        store.consume_plan(plan.plan_id, operation_id="other-operation", committed_hash="page-hash")
    assert used_error.value.code == "plan_used"


def test_update_plan_rejects_unknown_drift_and_expiry_codes(tmp_path: Path) -> None:
    store = PageOperationStore(str(tmp_path))
    with pytest.raises(UpdatePlanError) as unknown:
        store.claim_plan("unknown", page_path="wiki/concepts/page.md", base_hash="base", intent_hash="intent", operation_id="op")
    assert unknown.value.code == "plan_unknown"

    base_plan = store.issue_plan("wiki/concepts/base.md", "base", "intent")
    base_operation = store.create_operation(request_key="base-op", operation_kind="update", page_path=base_plan.page_path, base_hash="base", intended_hash="page")
    with pytest.raises(UpdatePlanError) as base_error:
        store.claim_plan(base_plan.plan_id, page_path=base_plan.page_path, base_hash="changed", intent_hash="intent", operation_id=base_operation.operation_id)
    assert base_error.value.code == "plan_base_mismatch"

    intent_plan = store.issue_plan("wiki/concepts/intent.md", "base", "intent")
    intent_operation = store.create_operation(request_key="intent-op", operation_kind="update", page_path=intent_plan.page_path, base_hash="base", intended_hash="page")
    with pytest.raises(UpdatePlanError) as intent_error:
        store.claim_plan(intent_plan.plan_id, page_path=intent_plan.page_path, base_hash="base", intent_hash="changed", operation_id=intent_operation.operation_id)
    assert intent_error.value.code == "plan_intent_drift"

    expired_plan = store.issue_plan("wiki/concepts/expired.md", "base", "intent")
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE update_plans SET expires_at=? WHERE plan_id=?", (datetime(2000, 1, 1, tzinfo=UTC).isoformat(), expired_plan.plan_id))
    connection.commit()
    connection.close()
    expired_operation = store.create_operation(request_key="expired-op", operation_kind="update", page_path=expired_plan.page_path, base_hash="base", intended_hash="page")
    with pytest.raises(UpdatePlanError) as expired_error:
        store.claim_plan(expired_plan.plan_id, page_path=expired_plan.page_path, base_hash="base", intent_hash="intent", operation_id=expired_operation.operation_id)
    assert expired_error.value.code == "plan_expired"


def test_only_one_thread_can_claim_the_same_plan(tmp_path: Path) -> None:
    store = PageOperationStore(str(tmp_path))
    plan = store.issue_plan("wiki/concepts/concurrent.md", "base", "intent")

    def claim(index: int) -> str:
        operation = store.create_operation(
            request_key=f"concurrent-{index}",
            operation_kind="update",
            page_path=plan.page_path,
            base_hash="base",
            intended_hash="page",
        )
        try:
            store.claim_plan(plan.plan_id, page_path=plan.page_path, base_hash="base", intent_hash="intent", operation_id=operation.operation_id)
        except UpdatePlanError as exc:
            return exc.code
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, (1, 2)))
    assert sorted(outcomes) == ["claimed", "plan_claimed"]

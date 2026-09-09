from __future__ import annotations

from hashlib import sha256
import sqlite3
from pathlib import Path

import pytest

from wiki.page_mutation import (
    MutationResult,
    PlanIntent,
    PageMutationCoordinator,
    plan_intent_hash,
    safe_stages_of,
)
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_operation_store import PageOperationStore, UpdatePlanError
from wiki.atomic_file import fault_context
from wiki.page_repair import PageRepairService
from wiki.wiki_index import refresh_navigation
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import create_wiki_root


def _operation(tmp_path: Path, old: str = "old", new: str = "new") -> tuple[PageMutationCoordinator, PageOperationStore, Path, str]:
    page = tmp_path / "wiki/concepts/general/page.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(old, encoding="utf-8")
    store = PageOperationStore(tmp_path)
    coordinator = PageMutationCoordinator(tmp_path, store=store)
    operation = coordinator.prepare(
        request_key=f"request-{old}-{new}",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash=sha256(old.encode()).hexdigest(),
        intended_hash=sha256(new.encode()).hexdigest(),
    )
    return coordinator, store, page, operation.operation_id


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_precommit_fault_keeps_old_page_and_is_not_repairable(stage: str, tmp_path: Path) -> None:
    coordinator, store, page, operation_id = _operation(tmp_path)

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with fault_context(fault):
        result = coordinator.commit(operation_id, "new")
    assert result.ok is False
    assert result.code == "write_failed_precommit"
    assert page.read_text(encoding="utf-8") == "old"
    operation = store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "failed_precommit"


def test_post_replace_fault_is_repair_pending_and_keeps_new_page(tmp_path: Path) -> None:
    coordinator, store, page, operation_id = _operation(tmp_path)

    def fault(stage: str) -> None:
        if stage == "post_replace":
            raise RuntimeError("crash after replace")

    with fault_context(fault):
        result = coordinator.commit(operation_id, "new")
    assert result.ok is True
    assert result.state == "repair_pending"
    assert page.read_text(encoding="utf-8") == "new"
    operation = store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "page_committed"


def test_journal_commit_fault_is_repair_pending_and_keeps_new_page(tmp_path: Path) -> None:
    coordinator, store, page, operation_id = _operation(tmp_path)

    def fault(stage: str) -> None:
        if stage == "journal_commit":
            raise RuntimeError("journal unavailable")

    with fault_context(fault):
        result = coordinator.commit(operation_id, "new")
    assert result.ok is True
    assert result.state == "repair_pending"
    assert page.read_text(encoding="utf-8") == "new"
    operation = store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "repair_pending"


@pytest.mark.parametrize("stage", ["dependencies", "retrieval", "navigation", "overview", "audit_log"])
def test_each_projection_fault_is_repairable_without_rewriting_page(tmp_path: Path, stage: str) -> None:
    coordinator, _, page, operation_id = _operation(tmp_path)
    assert coordinator.commit(operation_id, "new").ok is True
    before = page.read_bytes()
    projections = {name: (lambda: {"ok": True, "state": "ready"}) for name in ("dependencies", "retrieval", "navigation", "overview", "audit_log")}

    def fault(current: str) -> None:
        if current == f"projection:{stage}":
            raise RuntimeError("projection unavailable")

    with fault_context(fault):
        pending = coordinator.run_projections(operation_id, projections)
    assert pending.ok is True
    assert pending.state == "repair_pending"
    assert pending.failed_stage == stage
    assert page.read_bytes() == before
    assert coordinator.run_projections(operation_id, projections).state == "completed"
    assert page.read_bytes() == before


def test_formal_projection_coerces_invalid_lifecycle_to_review_required(tmp_path: Path) -> None:
    page = tmp_path / "wiki/concepts/general/page.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    text = "---\ngenerated: true\nlifecycle: invalid\n---\n\n# Page\n\n正文\n"
    page.write_text(text, encoding="utf-8")
    page_hash = sha256(text.encode()).hexdigest()
    coordinator = PageMutationCoordinator(tmp_path)
    operation = coordinator.prepare(
        request_key="invalid-lifecycle",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash=page_hash,
        intended_hash=page_hash,
    )

    projection = coordinator.projections_for(operation)["dependencies"]()

    assert projection == {"ok": True, "state": "ready"}
    stored = KnowledgeDependencies.read_page_projection(tmp_path, "wiki/concepts/general/page.md")
    assert stored["lifecycle"] == "review_required"


def test_recovery_classifies_base_intended_and_third_hashes(tmp_path: Path) -> None:
    coordinator, _, page, operation_id = _operation(tmp_path)
    page.write_text("new", encoding="utf-8")
    intended = coordinator.recover(operation_id)
    assert intended.ok is True
    assert intended.state == "repair_pending"

    coordinator, _, page, operation_id = _operation(tmp_path, old="old-2", new="new-2")
    base = coordinator.recover(operation_id)
    assert base.code == "write_failed_precommit"
    assert base.state == "failed_precommit"

    coordinator, _, page, operation_id = _operation(tmp_path, old="old-3", new="new-3")
    page.write_text("third", encoding="utf-8")
    third = coordinator.recover(operation_id)
    assert third.code == "operation_conflict"
    assert third.state == "conflict"


def test_projection_failure_is_repair_pending_and_retry_skips_succeeded_stages(tmp_path: Path) -> None:
    coordinator, _, _, operation_id = _operation(tmp_path)
    assert coordinator.commit(operation_id, "new").ok is True
    calls: list[str] = []

    def projection(name: str):
        def run() -> dict[str, object]:
            calls.append(name)
            return {"ok": True, "state": "ready"}

        return run

    def fault(stage: str) -> None:
        if stage == "projection:navigation":
            raise RuntimeError("navigation unavailable")

    projections = {name: projection(name) for name in ("dependencies", "retrieval", "navigation", "overview", "audit_log")}
    with fault_context(fault):
        pending = coordinator.run_projections(operation_id, projections)
    assert pending.to_dict() == {
        "ok": True,
        "state": "repair_pending",
        "operation_id": operation_id,
        "failed_stage": "navigation",
        "code": "projection_repair_required",
        "repair_action": "repair_page_operation",
        "stages": pending.to_dict()["stages"],
    }
    completed = coordinator.run_projections(operation_id, projections)
    assert completed.state == "completed"
    assert calls == ["dependencies", "retrieval", "navigation", "overview", "audit_log"]


@pytest.mark.parametrize(
    ("missing_stage", "missing_target", "failure_code", "full_target"),
    [
        (
            "navigation",
            "wiki/concepts/index.md",
            "incremental_navigation_index_missing",
            "wiki/concepts/index.md",
        ),
        (
            "overview",
            "wiki/overview.md",
            "incremental_overview_structure_missing",
            "wiki/overview.md",
        ),
    ],
)
def test_incremental_projection_structure_failure_is_fail_loud_then_repair_escalates_to_full(
    tmp_path: Path,
    missing_stage: str,
    missing_target: str,
    failure_code: str,
    full_target: str,
) -> None:
    create_wiki_root(tmp_path)
    page_path = "wiki/concepts/general/page.md"
    page = tmp_path / page_path
    page.parent.mkdir(parents=True, exist_ok=True)
    old_text = "---\ntype: concept\ntitle: Page\ngenerated: false\n---\n\n# Page\n\nold\n"
    new_text = old_text.replace("old", "new")
    page.write_text(old_text, encoding="utf-8")
    refresh_navigation(tmp_path)
    refresh_overview(tmp_path)
    (tmp_path / missing_target).unlink()

    store = PageOperationStore(tmp_path)
    coordinator = PageMutationCoordinator(tmp_path, store=store)
    base_hash = sha256(page.read_bytes()).hexdigest()
    result = coordinator.write_and_project(
        operation_kind="update",
        page_path=page_path,
        base_hash=base_hash,
        text=new_text,
        expected_hash=base_hash,
    )

    assert result.ok is True
    assert result.state == "repair_pending"
    assert result.failed_stage == missing_stage
    operation = store.get_operation(result.operation_id or "")
    assert operation is not None
    assert operation.page_path == page_path
    assert operation.stages[missing_stage]["state"] == "failed"
    assert operation.stages[missing_stage]["code"] == failure_code
    assert operation.stages[missing_stage]["result"] == {"ok": False, "code": failure_code}
    assert not (tmp_path / missing_target).exists()

    # Repair owns the explicit full-rebuild fallback; the first write remains
    # fail-loud and does not silently create the missing projection.
    repaired = PageRepairService(tmp_path).apply(operation.operation_id)
    assert repaired["ok"] is True
    assert repaired["state"] == "completed"
    repair_stage = repaired["stages"][missing_stage]
    assert repair_stage["state"] == "succeeded"
    assert repair_stage["result"]["ok"] is True
    assert repair_stage["result"]["escalated_to_full_rebuild"] is True
    assert repair_stage["result"]["escalated_from_code"] == failure_code
    final = PageOperationStore(tmp_path).get_operation(operation.operation_id)
    assert final is not None
    assert final.stages[missing_stage]["state"] == "succeeded"
    assert final.stages[missing_stage]["result"]["escalated_to_full_rebuild"] is True
    assert final.stages[missing_stage]["result"]["escalated_from_code"] == failure_code
    assert page.read_text(encoding="utf-8") == new_text

    expected_root = tmp_path / "expected-full-rebuild"
    create_wiki_root(expected_root)
    expected_page = expected_root / page_path
    expected_page.parent.mkdir(parents=True, exist_ok=True)
    expected_page.write_text(new_text, encoding="utf-8")
    if missing_stage == "navigation":
        assert refresh_navigation(expected_root)["ok"] is True
    else:
        assert refresh_overview(expected_root)["ok"] is True
    assert (tmp_path / full_target).read_bytes() == (expected_root / full_target).read_bytes()


def test_repair_and_project_existing_return_the_typed_mutation_result(tmp_path: Path) -> None:
    coordinator, _, page, operation_id = _operation(tmp_path)
    assert isinstance(coordinator.commit(operation_id, "new"), MutationResult)
    projections = {name: (lambda: {"ok": True, "state": "ready"}) for name in ("dependencies", "retrieval", "navigation", "overview", "audit_log")}

    repaired = coordinator.repair(operation_id, projections)
    assert isinstance(repaired, MutationResult)
    assert repaired.state == "completed"

    projected = coordinator.project_existing(operation_id, projections=projections)
    assert isinstance(projected, MutationResult)
    assert projected.state == "completed"
    assert projected.already_applied is False
    assert page.read_text(encoding="utf-8") == "new"


def _plan_inputs(tmp_path: Path) -> tuple[PageMutationCoordinator, PageOperationStore, Path, str, str, str]:
    page = tmp_path / "wiki/concepts/general/plan.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("old", encoding="utf-8")
    base_hash = sha256(b"old").hexdigest()
    text = "new"
    intended_hash = sha256(text.encode()).hexdigest()
    store = PageOperationStore(tmp_path)
    coordinator = PageMutationCoordinator(tmp_path, store=store)
    plan = coordinator.issue_plan(
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        intent=PlanIntent(body=text, frontmatter={}),
    )
    return coordinator, store, page, plan.plan_id, base_hash, intended_hash


def test_write_and_project_plan_claims_consumes_and_returns_safe_stages(tmp_path: Path) -> None:
    coordinator, store, page, plan_id, base_hash, intended_hash = _plan_inputs(tmp_path)
    result = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert result.ok is True
    assert result.state == "completed"
    assert result.stages["dependencies"]["state"] == "succeeded"
    assert store.get_plan(plan_id).state == "consumed"


def test_write_and_project_reuses_supplied_intended_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import wiki.page_mutation as page_mutation_module

    text = "new"
    intended_hash = sha256(text.encode()).hexdigest()
    hash_calls = 0
    original_sha256 = page_mutation_module.sha256

    def counting_sha256(value: bytes):
        nonlocal hash_calls
        hash_calls += 1
        return original_sha256(value)

    monkeypatch.setattr(page_mutation_module, "sha256", counting_sha256)
    coordinator = PageMutationCoordinator(tmp_path)
    monkeypatch.setattr(coordinator, "projections_for", lambda _operation: {})

    result = coordinator.write_and_project(
        operation_kind="note",
        page_path="wiki/concepts/general/page.md",
        base_hash=None,
        text=text,
        intended_hash=intended_hash,
    )

    assert result.ok is True
    assert result.page_hash == intended_hash
    assert hash_calls == 1  # the final commit integrity check remains in place


def test_mutation_result_exposes_only_neutral_safe_stage_views(tmp_path: Path) -> None:
    _, store, _, operation_id = _operation(tmp_path)
    store.record_stage(
        operation_id,
        "retrieval",
        "succeeded",
        result={"ok": True, "state": "ready", "retrieval_index": {"secret": "hidden"}, "written": ["wiki/page.md"]},
    )
    operation = store.get_operation(operation_id)
    assert operation is not None

    safe_stages = safe_stages_of(operation)
    assert safe_stages["retrieval"]["result"] == {"ok": True, "state": "ready", "written": ["wiki/page.md"]}

    result = MutationResult(
        ok=True,
        stages={
            "dependencies": {"state": "succeeded"},
            "retrieval": safe_stages["retrieval"],
            "navigation": {"state": "pending"},
        },
    )
    assert result.stage_result("dependencies") is None
    assert result.stage_result("retrieval") == {"ok": True, "state": "ready", "written": ["wiki/page.md"]}
    assert result.stage_result("navigation") is None
    assert not hasattr(result, "dependency_projection")
    assert not hasattr(result, "retrieval_index")
    assert not hasattr(result, "index_response")


def test_mutation_result_does_not_render_failed_stage_without_result() -> None:
    failed = MutationResult(
        ok=True,
        stages={"dependencies": {"state": "failed", "code": "dependency_unavailable"}},
    )
    pending = MutationResult(ok=True, stages={"dependencies": {"state": "pending"}})
    succeeded = MutationResult(ok=True, stages={"dependencies": {"state": "succeeded"}})

    assert failed.stage_result("dependencies") is None
    assert pending.stage_result("dependencies") is None
    assert succeeded.stage_result("dependencies") is None


@pytest.mark.parametrize(
    ("state", "already_applied", "expected"),
    [
        ("already_applied", True, {"ok": True, "state": "already_applied", "already_applied": True}),
        ("completed", True, {"ok": True, "state": "completed", "already_applied": True}),
        ("already_applied", False, {"ok": True, "state": "already_applied"}),
    ],
)
def test_mutation_result_to_dict_keeps_public_replay_representation(
    state: str, already_applied: bool, expected: dict[str, object]
) -> None:
    result = MutationResult(ok=True, state=state, already_applied=already_applied)
    assert result.to_dict() == expected


def test_mutation_result_from_mapping_omits_none_fields_and_empty_stages() -> None:
    replay = MutationResult.from_mapping(
        {"ok": True, "state": "already_applied", "operation_id": "op-1", "already_applied": True},
        stages={},
    )
    failed = MutationResult.from_mapping(
        {"ok": False, "state": None, "code": "plan_unknown", "page_hash": None},
        stages={},
    )
    legacy_bool = MutationResult.from_mapping(
        {"ok": True, "state": "completed", "already_applied": "completed"},
        stages={},
    )

    assert replay.to_dict() == {"ok": True, "state": "already_applied", "operation_id": "op-1", "already_applied": True}
    assert failed.to_dict() == {"ok": False, "code": "plan_unknown"}
    assert legacy_bool.already_applied is False


def test_write_and_project_claimed_prepared_plan_is_not_replayed(tmp_path: Path) -> None:
    coordinator, store, page, plan_id, base_hash, intended_hash = _plan_inputs(tmp_path)
    operation = store.create_operation(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        intended_hash=intended_hash,
    )
    store.claim_plan(
        plan_id,
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        intent_hash=plan_intent_hash(
            page.relative_to(tmp_path).as_posix(),
            base_hash,
            PlanIntent(body="new", frontmatter={}),
        ),
        operation_id=operation.operation_id,
    )
    result = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert result.code == "plan_claimed"
    assert result.operation_id == operation.operation_id


def test_write_and_project_rejects_expired_and_intent_drift(tmp_path: Path) -> None:
    coordinator, store, page, plan_id, base_hash, intended_hash = _plan_inputs(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE update_plans SET expires_at=? WHERE plan_id=?", ("2000-01-01T00:00:00+00:00", plan_id))
    connection.commit()
    connection.close()
    expired = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert expired.code == "plan_expired"

    fresh = coordinator.issue_plan(
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        intent=PlanIntent(body="new", frontmatter={}),
    )
    drift = coordinator.write_and_project(
        request_key=fresh.plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=fresh.plan_id,
        plan_intent=PlanIntent(body="changed", frontmatter={}),
    )
    assert drift.code == "plan_intent_drift"


def test_write_and_project_claim_failure_marks_operation_failed_precommit(tmp_path: Path) -> None:
    coordinator, store, page, plan_id, base_hash, intended_hash = _plan_inputs(tmp_path)
    result = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path="wiki/concepts/other.md",
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert result.code == "plan_intent_drift"
    operation = store.get_operation_by_request_key(plan_id)
    assert operation is not None
    assert operation.state == "failed_precommit"


def test_write_and_project_consumed_replay_exposes_repair_action(tmp_path: Path) -> None:
    coordinator, store, page, plan_id, base_hash, intended_hash = _plan_inputs(tmp_path)
    first = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert first.ok
    operation = store.get_operation(first.operation_id)
    assert operation is not None
    store.set_operation_state(operation.operation_id, "repair_pending")
    replay = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert replay.already_applied is True
    assert replay.repair_action == "repair_page_operation"


def test_write_and_project_plan_consume_failure_is_repair_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, store, page, plan_id, base_hash, intended_hash = _plan_inputs(tmp_path)

    def fail_consume(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise UpdatePlanError("plan_state_busy")

    monkeypatch.setattr(store, "consume_plan", fail_consume)
    result = coordinator.write_and_project(
        request_key=plan_id,
        operation_kind="update",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=base_hash,
        text="new",
        expected_hash=base_hash,
        plan_id=plan_id,
        plan_intent=PlanIntent(body="new", frontmatter={}),
    )
    assert result.ok is True
    assert result.state == "repair_pending"
    assert result.code == "plan_consume_pending"
    assert result.repair_action == "repair_page_operation"


def test_write_and_project_chat_source_replays_existing_request(tmp_path: Path) -> None:
    page = tmp_path / "raw/sources/chat/2026/08/13/session/revision-000001.md"
    page.parent.mkdir(parents=True)
    text = "raw"
    coordinator = PageMutationCoordinator(tmp_path)
    first = coordinator.write_and_project(
        request_key="chat:session:hash",
        operation_kind="chat_source",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=None,
        text=text,
    )
    second = coordinator.write_and_project(
        request_key="chat:session:hash",
        operation_kind="chat_source",
        page_path=page.relative_to(tmp_path).as_posix(),
        base_hash=None,
        text=text,
    )
    assert first.ok and second.ok
    assert second.already_applied is True
    assert second.operation_id == first.operation_id

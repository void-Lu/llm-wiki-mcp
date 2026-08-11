from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from wiki.page_mutation import PageMutationCoordinator


def _operation(tmp_path: Path, old: str = "old", new: str = "new") -> tuple[PageMutationCoordinator, Path, str]:
    page = tmp_path / "wiki/concepts/general/page.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(old, encoding="utf-8")
    coordinator = PageMutationCoordinator(tmp_path)
    operation = coordinator.prepare(
        request_key=f"request-{old}-{new}",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash=sha256(old.encode()).hexdigest(),
        intended_hash=sha256(new.encode()).hexdigest(),
    )
    return coordinator, page, operation.operation_id


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_precommit_fault_keeps_old_page_and_is_not_repairable(stage: str, tmp_path: Path) -> None:
    coordinator, page, operation_id = _operation(tmp_path)

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    result = coordinator.commit(operation_id, "new", fault=fault)
    assert result["ok"] is False
    assert result["code"] == "write_failed_precommit"
    assert page.read_text(encoding="utf-8") == "old"
    operation = coordinator.store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "failed_precommit"


def test_post_replace_fault_is_repair_pending_and_keeps_new_page(tmp_path: Path) -> None:
    coordinator, page, operation_id = _operation(tmp_path)

    def fault(stage: str) -> None:
        if stage == "post_replace":
            raise RuntimeError("crash after replace")

    result = coordinator.commit(operation_id, "new", fault=fault)
    assert result["ok"] is True
    assert result["state"] == "repair_pending"
    assert page.read_text(encoding="utf-8") == "new"
    operation = coordinator.store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "page_committed"


def test_journal_commit_fault_is_repair_pending_and_keeps_new_page(tmp_path: Path) -> None:
    coordinator, page, operation_id = _operation(tmp_path)

    def fault(stage: str) -> None:
        if stage == "journal_commit":
            raise RuntimeError("journal unavailable")

    result = coordinator.commit(operation_id, "new", fault=fault)
    assert result["ok"] is True
    assert result["state"] == "repair_pending"
    assert page.read_text(encoding="utf-8") == "new"
    operation = coordinator.store.get_operation(operation_id)
    assert operation is not None
    assert operation.state == "repair_pending"


@pytest.mark.parametrize("stage", ["dependencies", "retrieval", "navigation", "overview", "audit_log"])
def test_each_projection_fault_is_repairable_without_rewriting_page(tmp_path: Path, stage: str) -> None:
    coordinator, page, operation_id = _operation(tmp_path)
    assert coordinator.commit(operation_id, "new")["ok"] is True
    before = page.read_bytes()
    projections = {name: (lambda: {"ok": True, "state": "ready"}) for name in ("dependencies", "retrieval", "navigation", "overview", "audit_log")}

    def fault(current: str) -> None:
        if current == f"projection:{stage}":
            raise RuntimeError("projection unavailable")

    pending = coordinator.run_projections(operation_id, projections, fault=fault)
    assert pending["ok"] is True
    assert pending["state"] == "repair_pending"
    assert pending["failed_stage"] == stage
    assert page.read_bytes() == before
    assert coordinator.run_projections(operation_id, projections)["state"] == "completed"
    assert page.read_bytes() == before


def test_recovery_classifies_base_intended_and_third_hashes(tmp_path: Path) -> None:
    coordinator, page, operation_id = _operation(tmp_path)
    page.write_text("new", encoding="utf-8")
    intended = coordinator.recover(operation_id)
    assert intended["hash_classification"] == "intended"
    assert intended["state"] == "repair_pending"

    coordinator, page, operation_id = _operation(tmp_path, old="old-2", new="new-2")
    base = coordinator.recover(operation_id)
    assert base["code"] == "write_failed_precommit"
    assert base["hash_classification"] == "base"

    coordinator, page, operation_id = _operation(tmp_path, old="old-3", new="new-3")
    page.write_text("third", encoding="utf-8")
    third = coordinator.recover(operation_id)
    assert third["code"] == "operation_conflict"
    assert third["hash_classification"] == "third"


def test_projection_failure_is_repair_pending_and_retry_skips_succeeded_stages(tmp_path: Path) -> None:
    coordinator, _, operation_id = _operation(tmp_path)
    assert coordinator.commit(operation_id, "new")["ok"] is True
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
    pending = coordinator.run_projections(operation_id, projections, fault=fault)
    assert pending == {
        "ok": True,
        "state": "repair_pending",
        "operation_id": operation_id,
        "failed_stage": "navigation",
        "code": "projection_repair_required",
        "repair_action": "repair_page_operation",
    }
    completed = coordinator.run_projections(operation_id, projections)
    assert completed["state"] == "completed"
    assert calls == ["dependencies", "retrieval", "navigation", "overview", "audit_log"]

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from wiki.page_mutation import PageMutationCoordinator, safe_stages_of
from wiki.page_operation_store import PageOperationStore
from wiki.page_repair import PageRepairService


def test_page_repair_rebuilds_only_projections_and_audits_once(tmp_path: Path) -> None:
    page = tmp_path / "wiki/concepts/general/page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: concept\ntitle: Page\ngenerated: false\nproject: demo\n---\n\n# Page\n\nbody\n",
        encoding="utf-8",
    )
    before = page.read_bytes()
    store = PageOperationStore(tmp_path)
    coordinator = PageMutationCoordinator(tmp_path, store=store)
    intended_hash = sha256(before).hexdigest()
    operation = coordinator.prepare(
        request_key="repair-request",
        operation_kind="update",
        page_path="wiki/concepts/general/page.md",
        base_hash=intended_hash,
        intended_hash=intended_hash,
    )
    store.set_operation_state(operation.operation_id, "page_committed")

    service = PageRepairService(tmp_path)
    plan = service.plan(operation.operation_id)
    assert plan["ok"] is True
    assert "body" not in str(plan)
    summary = plan["operations"][0]
    assert set(summary) == set(operation.to_dict())
    assert summary["request_key"] == "repair-request"
    assert summary["created_at"] == operation.created_at
    assert "body" not in str(summary)
    assert summary["stages"] == safe_stages_of(operation)
    first = service.apply(operation.operation_id)
    second = service.apply(operation.operation_id)

    assert first["ok"] is True and first["state"] == "completed"
    assert second["ok"] is True and second["state"] == "completed"
    assert page.read_bytes() == before
    log_text = (tmp_path / "wiki/log.md").read_text(encoding="utf-8")
    assert log_text.count(f"- operation_id: {operation.operation_id}") == 1

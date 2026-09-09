from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import wiki.repair_plan as repair_plan
from wiki.atomic_file import AtomicFileError, atomic_write_bytes
from wiki.repair_plan import RepairPlanError, RepairPlanHooks, RepairPlanOwner
from wiki.wiki_paths import WikiPathError, admin_wiki_page_file, validate_wiki_page_path


def _page(root: Path, relative: str, content: str = "before") -> Path:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _owner(root: Path) -> RepairPlanOwner:
    return RepairPlanOwner(
        root,
        kind="test_repair",
        plan_prefix="test-repair-",
        audit_dir=".llm-wiki/test-repair",
    )


def _plan(owner: RepairPlanOwner, pages: list[Path]) -> dict[str, object]:
    entries = [
        {
            "page_path": page.relative_to(owner.root).as_posix(),
            "expected_page_hash": hashlib.sha256(page.read_bytes()).hexdigest(),
        }
        for page in pages
    ]
    return owner.create_plan({"entries": entries, "summary": {"pages": len(entries)}})


def _hooks(*, fail_on: str | None = None) -> RepairPlanHooks:
    def apply(context):
        if context.page_path == fail_on:
            raise RepairPlanError("domain_write_failed")
        atomic_write_bytes(context.source, context.original + b"\nchanged")
        context.wrote = True
        return {"page_path": context.page_path, "page_written": True}

    def rollback(context):
        if context.wrote:
            atomic_write_bytes(context.source, context.original)

    return RepairPlanHooks(apply=apply, rollback=rollback)


def test_owner_rejects_page_cas_drift_before_any_hook_runs(tmp_path: Path) -> None:
    page = _page(tmp_path, "wiki/maintenance/repair.md")
    owner = _owner(tmp_path)
    plan = _plan(owner, [page])
    page.write_text("drift", encoding="utf-8")

    result = owner.apply(str(plan["plan_id"]), hooks=_hooks())

    assert result["code"] == "repair_plan_cas_mismatch"
    assert result["writes"] == 0
    assert page.read_text(encoding="utf-8") == "drift"


def test_owner_compensates_partial_page_failure_and_records_audit(tmp_path: Path) -> None:
    pages = [_page(tmp_path, f"wiki/maintenance/part-{index}.md") for index in (1, 2)]
    owner = _owner(tmp_path)
    plan = _plan(owner, pages)
    originals = [page.read_bytes() for page in pages]

    result = owner.apply(
        str(plan["plan_id"]),
        hooks=_hooks(fail_on="wiki/maintenance/part-2.md"),
    )

    assert result["ok"] is False
    assert result["rolled_back"] is True
    assert result["rollback_errors"] == []
    assert [page.read_bytes() for page in pages] == originals
    audit = owner.read_audit(str(plan["plan_id"]))
    assert audit["state"] == "rolled_back"
    assert audit["rolled_back"] is True


def test_owner_apply_is_idempotent_after_success(tmp_path: Path) -> None:
    page = _page(tmp_path, "wiki/maintenance/repeat.md")
    owner = _owner(tmp_path)
    plan = _plan(owner, [page])

    first = owner.apply(str(plan["plan_id"]), hooks=_hooks())
    second = owner.apply(str(plan["plan_id"]), hooks=_hooks())

    assert first["ok"] is True
    assert second == {
        "ok": True,
        "already_applied": True,
        "plan_id": plan["plan_id"],
        "audit_state": "applied",
    }


def test_owner_audit_write_failure_is_not_silent_and_marks_repair_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page(tmp_path, "wiki/maintenance/audit.md")
    owner = _owner(tmp_path)
    plan = _plan(owner, [page])
    original_page_bytes = page.read_bytes()
    original_writer = repair_plan.atomic_write_text

    def fail_audit(target, _text):
        if str(target).endswith(".audit.json"):
            raise AtomicFileError("audit_backend_secret")
        return original_writer(target, _text)

    monkeypatch.setattr(repair_plan, "atomic_write_text", fail_audit)
    result = owner.apply(str(plan["plan_id"]), hooks=_hooks())

    assert result["ok"] is False
    assert result["state"] == "repair_pending"
    assert "audit_write_failed" in result["rollback_errors"]
    assert result["warnings"] == [
        {
            "page_path": "",
            "stage": "audit",
            "code": "audit_write_failed",
            "message": "repair audit was not written",
        }
    ]
    assert page.read_bytes() == original_page_bytes


def test_admin_page_file_allows_maintenance_prefix_and_navigation_index(tmp_path: Path) -> None:
    maintenance = _page(tmp_path, "wiki/maintenance/repair.md")
    index = _page(tmp_path, "wiki/concepts/index.md")

    assert admin_wiki_page_file(tmp_path, "wiki/maintenance/repair.md") == maintenance
    assert admin_wiki_page_file(tmp_path, "wiki/concepts/index.md") == index
    with pytest.raises(WikiPathError) as escaped:
        admin_wiki_page_file(tmp_path, "wiki/../outside.md")
    assert escaped.value.code == "path_escape"
    with pytest.raises(WikiPathError) as ordinary:
        validate_wiki_page_path("wiki/maintenance/repair.md")
    assert ordinary.value.code == "invalid_wiki_path"

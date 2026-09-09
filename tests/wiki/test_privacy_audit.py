from __future__ import annotations

from pathlib import Path
from typing import Any, cast
import json

import wiki.privacy_audit as audit_module
from wiki.privacy_audit import PrivacyAuditService
from retrieval.retrieval_index import RetrievalIndexStore


def _page(root: Path, relative: str, title: str = "安全标题", body: str = "正文") -> Path:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\npage_ref: ref-1\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_privacy_plan_reports_hits_without_storing_body_and_apply_is_repeatable(tmp_path: Path) -> None:
    page = _page(tmp_path, "wiki/privacy.md", title="owner@example.com", body="token=secret-token-1234567890")
    before = page.read_bytes()

    plan = PrivacyAuditService(tmp_path).plan()
    entries = cast(list[dict[str, Any]], plan["entries"])
    entry = next(item for item in entries if item["page_path"] == "wiki/privacy.md")
    plan_text = (tmp_path / ".llm-wiki/admin-plans" / f"privacy-audit-{plan['plan_id']}.json").read_text(encoding="utf-8")

    assert page.read_bytes() == before
    assert "owner@example.com" not in plan_text
    assert "secret-token-1234567890" not in plan_text
    assert "title" in entry["hit_fields"]
    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is True
    redacted = page.read_text(encoding="utf-8")
    assert "owner@example.com" not in redacted
    assert "secret-token-1234567890" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))["already_applied"] is True


def test_privacy_audit_refuses_locator_changes_without_writes(tmp_path: Path) -> None:
    page = _page(tmp_path, "wiki/contact@corp.com.md", body="[[wiki/contact@corp.com.md]]")
    before = page.read_bytes()

    plan = PrivacyAuditService(tmp_path).plan()
    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is False
    assert applied["code"] == "privacy_locator_review_required"
    assert page.exists()
    assert page.read_bytes() == before


def test_privacy_audit_locator_changes_require_opt_in_and_update_wikilinks(tmp_path: Path) -> None:
    page = _page(tmp_path, "wiki/contact@corp.com.md", body="secret owner@example.com")
    link_page = _page(tmp_path, "wiki/index.md", body="See [[wiki/contact@corp.com.md]]")

    plan = PrivacyAuditService(tmp_path).plan()
    entries = cast(list[dict[str, Any]], plan["entries"])
    contact_entry = next(item for item in entries if item["page_path"] == "wiki/contact@corp.com.md")
    new_path = tmp_path / Path(*contact_entry["filename_change"]["new_page_path"].split("/"))
    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]), allow_locator_changes=True)

    assert applied["ok"] is True
    assert not page.exists()
    assert new_path.is_file()
    assert contact_entry["filename_change"]["new_page_path"] in link_page.read_text(encoding="utf-8")


def test_privacy_audit_rejects_cas_drift(tmp_path: Path) -> None:
    page = _page(tmp_path, "wiki/drift.md", body="email owner@example.com")
    plan = PrivacyAuditService(tmp_path).plan()
    page.write_text(page.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    changed = page.read_bytes()

    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is False
    assert applied["code"] == "privacy_audit_cas_mismatch"
    assert page.read_bytes() == changed


def test_privacy_partial_failure_rolls_back_all_pages(tmp_path: Path, monkeypatch) -> None:
    pages = [_page(tmp_path, f"wiki/part-{index}.md", body="owner@example.com") for index in (1, 2)]
    originals = [page.read_bytes() for page in pages]
    plan = PrivacyAuditService(tmp_path).plan()
    original_write = audit_module.atomic_write_bytes
    calls = 0

    def fail_on_second(target, content, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise audit_module.AtomicFileError("fault")
        return original_write(target, content, **kwargs)

    monkeypatch.setattr(audit_module, "atomic_write_bytes", fail_on_second)
    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is False
    assert applied["rolled_back"] is True
    assert [page.read_bytes() for page in pages] == originals


def test_privacy_apply_refreshes_active_retrieval_after_page_write(tmp_path: Path, monkeypatch) -> None:
    _page(tmp_path, "wiki/concepts/privacy.md", body="owner@example.com")
    store = RetrievalIndexStore(tmp_path)
    store.build(store.iter_vault_pages())
    plan = PrivacyAuditService(tmp_path).plan()
    calls: list[str] = []
    original = audit_module.RetrievalIndexStore.update_page_from_file

    def record(self, target, **kwargs):
        calls.append(Path(target).relative_to(tmp_path).as_posix())
        return original(self, target, **kwargs)

    monkeypatch.setattr(audit_module.RetrievalIndexStore, "update_page_from_file", record)
    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is True
    assert calls == ["wiki/concepts/privacy.md"]
    assert "warnings" not in applied
    audit_path = tmp_path / ".llm-wiki/privacy-audit" / f"{plan['plan_id']}.audit.json"
    assert json.loads(audit_path.read_text(encoding="utf-8"))["warnings"] == []


def test_privacy_retrieval_failure_is_a_safe_nonfatal_audit_warning(tmp_path: Path, monkeypatch) -> None:
    page = _page(tmp_path, "wiki/concepts/privacy.md", body="owner@example.com")
    plan = PrivacyAuditService(tmp_path).plan()

    def fail(_self, _target, **_kwargs):
        raise RuntimeError("C:/private/source/secret-token")

    monkeypatch.setattr(audit_module.RetrievalIndexStore, "update_page_from_file", fail)
    applied = PrivacyAuditService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is True
    warnings = cast(list[dict[str, str]], applied["warnings"])
    assert warnings == [
        {
            "page_path": "wiki/concepts/privacy.md",
            "stage": "retrieval",
            "code": "retrieval_projection_failed",
            "message": "retrieval projection was not refreshed",
        }
    ]
    assert "owner@example.com" not in page.read_text(encoding="utf-8")
    audit_path = tmp_path / ".llm-wiki/privacy-audit" / f"{plan['plan_id']}.audit.json"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "secret-token" not in audit_text
    assert json.loads(audit_text)["warnings"] == warnings

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import yaml

import wiki.provenance_migration as migration
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.provenance_migration import ProvenanceMigrationService
from retrieval.retrieval_index import RetrievalIndexStore


def _write_page(root: Path, relative: str, frontmatter: dict[str, object], body: str = "正文") -> Path:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{rendered}\n---\n\n{body}\n", encoding="utf-8")
    return path


def _source(root: Path, relative: str = "raw/sources/file/demo.txt", content: str = "source") -> tuple[str, str]:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return relative, hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_plan_classifies_exact_missing_hash_mismatch_invalid_and_legacy(tmp_path: Path) -> None:
    source_path, source_hash = _source(tmp_path)
    _write_page(tmp_path, "wiki/exact.md", {"sources": [source_path], "source_hashes": {source_path: source_hash}})
    _write_page(tmp_path, "wiki/missing.md", {"sources": [source_path]})
    _write_page(tmp_path, "wiki/mismatch.md", {"sources": [source_path], "source_hashes": {source_path: "0" * 64}})
    _write_page(tmp_path, "wiki/invalid.md", {"sources": ["../outside.txt"]})
    _write_page(tmp_path, "wiki/legacy.md", {"source_capsule": {"path": "wiki/sources/old.md"}})

    result = ProvenanceMigrationService(tmp_path).plan()
    entries = cast(list[dict[str, object]], result["entries"])
    categories = {entry["page_path"]: entry["category"] for entry in entries}

    assert categories == {
        "wiki/exact.md": "exact",
        "wiki/invalid.md": "invalid",
        "wiki/legacy.md": "legacy",
        "wiki/mismatch.md": "hash_mismatch",
        "wiki/missing.md": "missing",
    }
    missing = next(entry for entry in entries if entry["page_path"] == "wiki/missing.md")
    assert missing["applyable"] is True
    dependency_diff = cast(dict[str, object], missing["dependency_diff"])
    assert dependency_diff["mode"] == "full"
    assert (tmp_path / ".llm-wiki/admin-plans" / f"provenance-migration-{result['plan_id']}.json").is_file()


def test_provenance_apply_is_cas_checked_repeatable_and_updates_dependency_projection(tmp_path: Path) -> None:
    source_path, source_hash = _source(tmp_path)
    page = _write_page(tmp_path, "wiki/migrate.md", {"sources": [source_path]})
    before = page.read_bytes()
    result = ProvenanceMigrationService(tmp_path).plan("wiki/migrate.md")

    assert page.read_bytes() == before
    applied = ProvenanceMigrationService(tmp_path).apply(str(result["plan_id"]))

    assert applied["ok"] is True
    frontmatter = yaml.safe_load(page.read_text(encoding="utf-8").split("---")[1])
    assert frontmatter["source_hashes"] == {source_path: source_hash}
    projection = KnowledgeDependencies.read_page_projection(tmp_path, "wiki/migrate.md")
    assert projection["state"] == "ready"
    assert projection["edges"]
    assert ProvenanceMigrationService(tmp_path).apply(str(result["plan_id"]))["already_applied"] is True


def test_provenance_apply_rejects_page_cas_drift_without_writing(tmp_path: Path) -> None:
    source_path, _ = _source(tmp_path)
    page = _write_page(tmp_path, "wiki/drift.md", {"sources": [source_path]})
    result = ProvenanceMigrationService(tmp_path).plan("wiki/drift.md")
    page.write_text(page.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
    changed = page.read_bytes()

    applied = ProvenanceMigrationService(tmp_path).apply(str(result["plan_id"]))

    assert applied["ok"] is False
    assert applied["code"] == "provenance_migration_cas_mismatch"
    assert page.read_bytes() == changed


def test_provenance_partial_failure_restores_all_pages(tmp_path: Path, monkeypatch) -> None:
    source_path, _ = _source(tmp_path)
    pages = [
        _write_page(tmp_path, f"wiki/part-{index}.md", {"sources": [source_path]})
        for index in (1, 2)
    ]
    originals = [page.read_bytes() for page in pages]
    result = ProvenanceMigrationService(tmp_path).plan()
    original_write = migration.atomic_write_text
    calls = 0

    def fail_on_second(target, text, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise migration.AtomicFileError("fault")
        return original_write(target, text, **kwargs)

    monkeypatch.setattr(migration, "atomic_write_text", fail_on_second)
    applied = ProvenanceMigrationService(tmp_path).apply(str(result["plan_id"]))

    assert applied["ok"] is False
    assert applied["rolled_back"] is True
    assert [page.read_bytes() for page in pages] == originals


def test_provenance_apply_refreshes_active_retrieval_after_page_write(tmp_path: Path, monkeypatch) -> None:
    source_path, _ = _source(tmp_path)
    _write_page(tmp_path, "wiki/concepts/migrate.md", {"sources": [source_path]})
    store = RetrievalIndexStore(tmp_path)
    store.build(store.iter_vault_pages())
    plan = ProvenanceMigrationService(tmp_path).plan("wiki/concepts/migrate.md")
    calls: list[str] = []
    original = migration.RetrievalIndexStore.update_page_from_file

    def record(self, target, **kwargs):
        calls.append(Path(target).relative_to(tmp_path).as_posix())
        return original(self, target, **kwargs)

    monkeypatch.setattr(migration.RetrievalIndexStore, "update_page_from_file", record)
    applied = ProvenanceMigrationService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is True
    assert calls == ["wiki/concepts/migrate.md"]
    assert "warnings" not in applied
    audit_path = tmp_path / ".llm-wiki/migrations" / f"{plan['plan_id']}.audit.json"
    assert json.loads(audit_path.read_text(encoding="utf-8"))["warnings"] == []


def test_provenance_retrieval_failure_is_a_safe_nonfatal_audit_warning(tmp_path: Path, monkeypatch) -> None:
    source_path, _ = _source(tmp_path)
    page = _write_page(tmp_path, "wiki/concepts/migrate.md", {"sources": [source_path]})
    plan = ProvenanceMigrationService(tmp_path).plan("wiki/concepts/migrate.md")

    def fail(_self, _target, **_kwargs):
        raise RuntimeError("C:/private/source/secret-token")

    monkeypatch.setattr(migration.RetrievalIndexStore, "update_page_from_file", fail)
    applied = ProvenanceMigrationService(tmp_path).apply(str(plan["plan_id"]))

    assert applied["ok"] is True
    warnings = cast(list[dict[str, str]], applied["warnings"])
    assert warnings == [
        {
            "page_path": "wiki/concepts/migrate.md",
            "stage": "retrieval",
            "code": "retrieval_projection_failed",
            "message": "retrieval projection was not refreshed",
        }
    ]
    assert page.read_text(encoding="utf-8").count("source_hashes:") == 1
    audit_path = tmp_path / ".llm-wiki/migrations" / f"{plan['plan_id']}.audit.json"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "secret-token" not in audit_text
    assert json.loads(audit_text)["warnings"] == warnings

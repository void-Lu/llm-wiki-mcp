from __future__ import annotations

from pathlib import Path

import pytest

from wiki.spec_reuse import SharedSpecService, SpecMirrorService
from wiki.wiki_io import split_frontmatter


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_spec_mirror_preview_and_apply_syncs_nested_tree_and_removes_stale_page(tmp_path: Path) -> None:
    source = tmp_path / "project/.trellis/spec"
    vault = tmp_path / "vault"
    _write(source / "backend/index.md", "# Backend\n")
    _write(source / "guides/shared.md", "# Shared\n")
    stale = vault / "wiki/projects/demo/specs/old.md"
    _write(stale, "# Old\n")

    service = SpecMirrorService(vault)
    preview = service.plan(source, "demo")

    assert preview["ok"] is True
    assert preview["summary"] == {"create": 2, "update": 0, "delete": 1, "unchanged": 0}

    result = service.apply(str(preview["plan_id"]))

    assert result["ok"] is True
    assert (vault / "wiki/projects/demo/specs/backend/index.md").read_text(encoding="utf-8") == "# Backend\n"
    assert (vault / "wiki/projects/demo/specs/guides/shared.md").is_file()
    assert not stale.exists()


def test_spec_mirror_apply_rejects_target_drift_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "project/.trellis/spec"
    vault = tmp_path / "vault"
    target = vault / "wiki/projects/demo/specs/backend/index.md"
    _write(source / "backend/index.md", "# Backend\n")
    _write(target, "# Original\n")

    service = SpecMirrorService(vault)
    preview = service.plan(source, "demo")
    target.write_text("# Drift\n", encoding="utf-8")

    result = service.apply(str(preview["plan_id"]))

    assert result == {
        "ok": False,
        "code": "target_changed",
        "plan_id": preview["plan_id"],
        "writes": 0,
        "completed": [],
    }
    assert target.read_text(encoding="utf-8") == "# Drift\n"


def test_spec_mirror_apply_retries_after_projection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "project/.trellis/spec"
    vault = tmp_path / "vault"
    _write(source / "backend/index.md", "# Backend\n")

    service = SpecMirrorService(vault)
    preview = service.plan(source, "demo")
    calls = {"n": 0}

    def flaky_navigation(root, changed_path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "code": "navigation_failed"}
        from wiki.wiki_index import refresh_navigation

        return refresh_navigation(root, changed_path=changed_path)

    monkeypatch.setattr("wiki.page_mutation_adapters.refresh_navigation", flaky_navigation)

    first = service.apply(str(preview["plan_id"]))
    assert first["code"] == "projection_repair_required"
    assert first["writes"] == 0
    assert (vault / "wiki/projects/demo/specs/backend/index.md").is_file()

    result = service.apply(str(preview["plan_id"]))

    assert result["ok"] is True
    assert result["writes"] == 0


def test_shared_spec_upsert_normalizes_scope_and_can_delete(tmp_path: Path) -> None:
    _write(tmp_path / "vault/wiki/projects/demo/specs/backend/style.md", "style\n")
    service = SharedSpecService(tmp_path / "vault")
    preview = service.plan(
        "upsert",
        "wiki/entities/shared-specs/python-style.md",
        title="Python style",
        body="# Python style\n\nUse one owner for each boundary.\n",
        applies_to={"languages": ["python", "Python"], "frameworks": ["pytest"]},
        conditions="Only for application code.",
        derived_from=[{"project": "demo", "path": "wiki/projects/demo/specs/backend/style.md", "rule": "style"}],
    )

    assert preview["ok"] is True
    assert preview["summary"] == {"create": 1, "update": 0}
    applied = service.apply(str(preview["plan_id"]))
    assert applied["ok"] is True

    page = tmp_path / "vault/wiki/entities/shared-specs/python-style.md"
    text = page.read_text(encoding="utf-8")
    frontmatter, _ = split_frontmatter(text)
    assert frontmatter["applies_to"] == {"frameworks": ["pytest"], "languages": ["Python", "python"]}
    assert "Use one owner for each boundary." in text

    delete_preview = service.plan("delete", "wiki/entities/shared-specs/python-style.md")
    assert service.apply(str(delete_preview["plan_id"]))["ok"] is True
    assert not page.exists()


def test_shared_spec_apply_rejects_target_drift(tmp_path: Path) -> None:
    _write(tmp_path / "vault/wiki/projects/demo/specs/style.md", "original\n")
    service = SharedSpecService(tmp_path / "vault")
    preview = service.plan(
        "upsert",
        "wiki/entities/shared-specs/python-style.md",
        title="Python style",
        body="Keep boundaries explicit.",
        derived_from=[{"project": "demo", "path": "wiki/projects/demo/specs/style.md", "rule": "style"}],
    )
    page = tmp_path / "vault/wiki/entities/shared-specs/python-style.md"
    page.parent.mkdir(parents=True)
    page.write_text("drift\n", encoding="utf-8")

    result = service.apply(str(preview["plan_id"]))

    assert result["ok"] is False
    assert result["code"] == "target_changed"


def test_shared_spec_apply_rejects_origin_drift(tmp_path: Path) -> None:
    origin = tmp_path / "vault/wiki/projects/demo/specs/style.md"
    _write(origin, "original\n")
    service = SharedSpecService(tmp_path / "vault")
    preview = service.plan(
        "upsert",
        "wiki/entities/shared-specs/python-style.md",
        title="Python style",
        body="Keep boundaries explicit.",
        derived_from=[{"project": "demo", "path": "wiki/projects/demo/specs/style.md", "rule": "style"}],
    )
    origin.write_text("changed\n", encoding="utf-8")

    result = service.apply(str(preview["plan_id"]))

    assert result["ok"] is False
    assert result["code"] == "source_changed"


def test_discard_removes_transient_plan_without_touching_target(tmp_path: Path) -> None:
    _write(tmp_path / "vault/wiki/projects/demo/specs/style.md", "original\n")
    service = SharedSpecService(tmp_path / "vault")
    preview = service.plan(
        "upsert",
        "wiki/entities/shared-specs/python-style.md",
        title="Python style",
        body="rule",
        derived_from=[{"project": "demo", "path": "wiki/projects/demo/specs/style.md", "rule": "style"}],
    )

    result = service.discard(str(preview["plan_id"]))

    assert result == {"ok": True, "plan_id": preview["plan_id"], "discarded": True}
    assert service.apply(str(preview["plan_id"]))["code"] == "plan_not_found"
    assert not (tmp_path / "vault/wiki/entities/shared-specs/python-style.md").exists()

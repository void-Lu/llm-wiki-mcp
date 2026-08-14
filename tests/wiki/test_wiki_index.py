from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import wiki.wiki_index as wiki_index
from wiki.atomic_file import AtomicFileError
from wiki.wiki_index import rebuild_retrieval_index, refresh_indexes, refresh_navigation
from tests.helpers import write_test_page
from wiki.wiki_paths import create_wiki_root
from retrieval.query_pipeline import run_query_v2


def _write(root: Path, path: str, title: str, summary: str = "") -> None:
    write_test_page(
        root,
        path,
        {"title": title, "summary": summary, "generated": True, "sources": []},
        summary or title,
    )


def test_refresh_indexes_groups_only_active_wiki_categories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/specs/spec.md", "Spec", "spec summary")
    _write(root, "wiki/concepts/suitescript/module.md", "SuiteScript", "concept summary")
    _write(root, "wiki/entities/customer/customer.md", "Customer", "entity summary")

    result = refresh_indexes(root)

    assert result["ok"] is True
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    for heading in ["## Projects", "## Concepts", "## Entities"]:
        assert heading in index
    assert "## Sources" not in index
    assert "sources/index.md" not in index
    assert "[[projects/alpha/index.md|alpha]]" in index
    assert "[[concepts/index.md|Concepts]]" in index
    assert "[[entities/index.md|Entities]]" in index
    assert "[[archives/log.md|Archives Log]]" in index
    assert not (root / "wiki/archives").exists()
    concepts_index = (root / "wiki/concepts/index.md").read_text(encoding="utf-8")
    assert "[[suitescript/index.md|suitescript]]" in concepts_index
    entities_index = (root / "wiki/entities/index.md").read_text(encoding="utf-8")
    assert "[[customer/customer.md|Customer]]" in entities_index


def test_navigation_projection_has_no_retrieval_rebuild_and_admin_path_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "invoice marker")
    calls: list[str] = []

    from wiki import ingest_service

    monkeypatch.setattr(ingest_service.RetrievalIndexStore, "build", lambda self, pages: calls.append(self.scope) or {"ok": True, "scope": self.scope, "operation": "build"})

    navigation = refresh_navigation(root)
    assert navigation["ok"] is True
    assert navigation["batch"] == {"kind": "navigation", "affected_count": len(navigation["written"])}
    assert calls == []

    rebuilt = cast(dict[str, Any], rebuild_retrieval_index(root))
    assert rebuilt["active"]["operation"] == "build"
    assert rebuilt["raw"]["operation"] == "build"
    assert calls == ["active", "raw"]


def test_refresh_navigation_diffs_utf8_content_before_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    page = root / "wiki/concepts/invoice.md"
    page.write_text("---\ntitle: Invoice\ngenerated: true\n---\n\n# Invoice\n", encoding="utf-8")
    calls: list[str] = []
    original_atomic_write = wiki_index.atomic_write_text

    def recording_atomic_write(target: Path, text: str, **kwargs: object) -> object:
        comparable_target = Path(str(target).removeprefix("\\\\?\\"))
        calls.append(comparable_target.relative_to(root).as_posix())
        return original_atomic_write(target, text, **kwargs)

    monkeypatch.setattr(wiki_index, "atomic_write_text", recording_atomic_write)

    first = refresh_navigation(root)
    first_call_count = len(calls)
    assert first_call_count > 0
    assert set(first["changed"]) == set(calls)

    calls.clear()
    second = refresh_navigation(root)
    assert calls == []
    assert second["written"] == first["written"]
    assert second["changed"] == []

    page.write_text(page.read_text(encoding="utf-8").replace("title: Invoice", "title: Invoice 更新"), encoding="utf-8")
    third = refresh_navigation(root)
    assert third["changed"] == ["wiki/concepts/index.md"]
    assert calls == ["wiki/concepts/index.md"]

def test_refresh_indexes_creates_project_index_grouped_by_subdirectories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    for subdir, filename, title in (
        ("specs", "spec.md", "Spec"),
        ("plans", "plan.md", "Plan"),
        ("architecture", "arch.md", "Architecture"),
        ("pipelines", "pipeline.md", "Pipeline"),
        ("troubleshooting", "issue.md", "Issue"),
        ("researches", "investigation.md", "Investigation"),
    ):
        _write(root, f"wiki/projects/alpha/{subdir}/{filename}", title, f"{subdir} summary")

    refresh_indexes(root)

    project_index = (root / "wiki/projects/alpha/index.md").read_text(encoding="utf-8")
    for heading in ["## Specs", "## Plans", "## Architecture", "## Pipelines", "## Troubleshooting", "## Researches"]:
        assert heading in project_index
    assert "## Sources" not in project_index


def test_refresh_indexes_refuses_to_overwrite_manual_top_index(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/index.md").write_text("---\ngenerated: false\n---\n\n# Manual Index\n", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is False
    assert result["code"] == "manual_page_exists"
    assert "Manual Index" in (root / "wiki/index.md").read_text(encoding="utf-8")


def test_refresh_indexes_does_not_crash_on_malformed_frontmatter_page(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/concepts/bad.md"
    target.write_text("---\ntitle: [broken\n---\n\n# Bad\n\nsummary", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is True
    assert "[[bad.md|Bad]]" in (root / "wiki/concepts/index.md").read_text(encoding="utf-8")


def test_refresh_indexes_never_recreates_retired_source_namespace(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    legacy = root / "wiki/sources/old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ntype: source_capsule\ngenerated: true\n---\n\n# old\n\nlegacy noise", encoding="utf-8")

    result = refresh_indexes(root)

    assert result["ok"] is True
    assert legacy.exists()
    assert not (root / "wiki/sources/index.md").exists()
    assert run_query_v2(root, "legacy noise", top_k=5, retrieval_mode="lexical")["results"] == []


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_top_index_atomic_fault_keeps_existing_index(stage: str, tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/index.md"
    target.write_text("---\ntype: index\ngenerated: true\n---\n\nold index\n", encoding="utf-8")

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with pytest.raises(AtomicFileError):
        wiki_index._write_top_index(root, fault=fault)

    assert target.read_text(encoding="utf-8") == "---\ntype: index\ngenerated: true\n---\n\nold index\n"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_top_index_post_replace_fault_keeps_complete_new_index(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/index.md"
    target.write_text("---\ntype: index\ngenerated: true\n---\n\nold index\n", encoding="utf-8")

    def fault(current: str) -> None:
        if current == "post_replace":
            raise RuntimeError("injected")

    with pytest.raises(AtomicFileError):
        wiki_index._write_top_index(root, fault=fault)

    assert target.read_text(encoding="utf-8") != "---\ntype: index\ngenerated: true\n---\n\nold index\n"
    assert target.read_text(encoding="utf-8").endswith("# Index\n\n") is False
    assert "# Index" in target.read_text(encoding="utf-8")
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []

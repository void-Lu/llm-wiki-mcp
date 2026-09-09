from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import wiki.wiki_index as wiki_index
import wiki.wiki_overview as wiki_overview
from wiki.atomic_file import AtomicFileError, fault_context
from wiki.wiki_index import rebuild_retrieval_index, refresh_indexes, refresh_navigation
from tests.helpers import write_test_page
from wiki.wiki_paths import INITIALIZED_PROJECTION_FILES, create_wiki_root, projection_files_initialized
from retrieval.query_pipeline import run_query_v2


def _write(root: Path, path: str, title: str, summary: str = "") -> None:
    write_test_page(
        root,
        path,
        {"title": title, "summary": summary, "generated": True, "sources": []},
        summary or title,
    )


def test_projection_initialization_has_one_owner_and_complete_file_set(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    wiki_root = root / "wiki"
    wiki_root.mkdir(parents=True)

    assert wiki_index.projection_files_initialized is projection_files_initialized
    assert wiki_overview.projection_files_initialized is projection_files_initialized
    assert INITIALIZED_PROJECTION_FILES == (
        Path("wiki/index.md"),
        Path("wiki/overview.md"),
        Path("wiki/log.md"),
    )
    assert projection_files_initialized(root) is False

    for relative in INITIALIZED_PROJECTION_FILES:
        (root / relative).touch()
        assert projection_files_initialized(root) is (relative == INITIALIZED_PROJECTION_FILES[-1])


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


def test_incremental_project_navigation_is_scoped_and_matches_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    alpha_pages = ["wiki/projects/alpha/specs/a.md", "wiki/projects/alpha/plans/b.md"]
    beta_pages = [f"wiki/projects/beta/specs/page-{index}.md" for index in range(12)]
    for path in [*alpha_pages, *beta_pages]:
        _write(root, path, Path(path).stem, "summary")
    refresh_navigation(root)
    unrelated_before = (root / "wiki/projects/beta/index.md").read_bytes()

    page = root / alpha_pages[0]
    page.write_text(page.read_text(encoding="utf-8").replace("title: a", "title: Alpha updated"), encoding="utf-8")
    metadata_paths: list[str] = []
    writes: list[str] = []
    original_metadata = wiki_index._read_page_metadata
    original_atomic_write = wiki_index.atomic_write_text

    def counted_metadata(path: Path) -> tuple[dict[str, Any], str]:
        comparable = Path(str(path).removeprefix("\\\\?\\"))
        metadata_paths.append(comparable.relative_to(root).as_posix())
        return original_metadata(path)

    def counted_write(target: Path, text: str, **kwargs: object) -> object:
        comparable = Path(str(target).removeprefix("\\\\?\\"))
        writes.append(comparable.relative_to(root).as_posix())
        return original_atomic_write(target, text, **kwargs)

    monkeypatch.setattr(wiki_index, "_read_page_metadata", counted_metadata)
    monkeypatch.setattr(wiki_index, "atomic_write_text", counted_write)
    result = refresh_navigation(root, changed_path=alpha_pages[0])

    assert result["ok"] is True
    assert result["changed"] == ["wiki/projects/alpha/index.md"]
    assert writes == ["wiki/projects/alpha/index.md"]
    assert metadata_paths
    assert all(path.startswith("wiki/projects/alpha/") for path in metadata_paths)
    assert (root / "wiki/projects/beta/index.md").read_bytes() == unrelated_before
    incremental = (root / "wiki/projects/alpha/index.md").read_bytes()

    refresh_navigation(root)
    assert (root / "wiki/projects/alpha/index.md").read_bytes() == incremental


def test_incremental_navigation_create_and_delete_matches_full_for_domains_and_entities(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/stable/page.md", "Stable")
    _write(root, "wiki/entities/customer/customer.md", "Customer")
    refresh_navigation(root)

    direct_page = root / "wiki/concepts/root.md"
    _write(root, direct_page.relative_to(root).as_posix(), "Root Concept")
    direct_created = refresh_navigation(root, changed_path="wiki/concepts/root.md")
    assert direct_created["ok"] is True
    direct_concepts = (root / "wiki/concepts/index.md").read_bytes()
    direct_top = (root / "wiki/index.md").read_bytes()
    refresh_navigation(root)
    assert (root / "wiki/concepts/index.md").read_bytes() == direct_concepts
    assert (root / "wiki/index.md").read_bytes() == direct_top

    direct_page.unlink()
    direct_deleted = refresh_navigation(root, changed_path="wiki/concepts/root.md")
    assert direct_deleted["ok"] is True
    direct_concepts_deleted = (root / "wiki/concepts/index.md").read_bytes()
    refresh_navigation(root)
    assert (root / "wiki/concepts/index.md").read_bytes() == direct_concepts_deleted

    new_domain_page = root / "wiki/concepts/transient/page.md"
    _write(root, new_domain_page.relative_to(root).as_posix(), "Transient")
    created = refresh_navigation(root, changed_path="wiki/concepts/transient/page.md")
    assert created["ok"] is True
    created_domain = (root / "wiki/concepts/transient/index.md").read_bytes()
    created_concepts = (root / "wiki/concepts/index.md").read_bytes()
    refresh_navigation(root)
    assert (root / "wiki/concepts/transient/index.md").read_bytes() == created_domain
    assert (root / "wiki/concepts/index.md").read_bytes() == created_concepts

    new_domain_page.unlink()
    (new_domain_page.parent / "index.md").unlink()
    new_domain_page.parent.rmdir()
    deleted = refresh_navigation(root, changed_path="wiki/concepts/transient/page.md")
    assert deleted["ok"] is True
    deleted_concepts = (root / "wiki/concepts/index.md").read_bytes()
    refresh_navigation(root)
    assert (root / "wiki/concepts/index.md").read_bytes() == deleted_concepts

    entity_page = root / "wiki/entities/customer/customer.md"
    entity_page.write_text(entity_page.read_text(encoding="utf-8").replace("title: Customer", "title: Customer Updated"), encoding="utf-8")
    entity_result = refresh_navigation(root, changed_path="wiki/entities/customer/customer.md")
    assert entity_result["ok"] is True
    incremental_entities = (root / "wiki/entities/index.md").read_bytes()
    refresh_navigation(root)
    assert (root / "wiki/entities/index.md").read_bytes() == incremental_entities


def test_incremental_navigation_bootstraps_a_new_project_without_touching_other_projects(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/existing/specs/page.md", "Existing")
    refresh_navigation(root)
    existing_before = (root / "wiki/projects/existing/index.md").read_bytes()

    new_path = "wiki/projects/new-project/specs/page.md"
    _write(root, new_path, "New Project")
    result = refresh_navigation(root, changed_path=new_path)

    assert result["ok"] is True
    assert set(result["written"]) == {
        "wiki/projects/new-project/index.md",
        "wiki/index.md",
    }
    assert (root / "wiki/projects/existing/index.md").read_bytes() == existing_before
    incremental_project = (root / "wiki/projects/new-project/index.md").read_bytes()
    incremental_top = (root / "wiki/index.md").read_bytes()
    refresh_navigation(root)
    assert (root / "wiki/projects/new-project/index.md").read_bytes() == incremental_project
    assert (root / "wiki/index.md").read_bytes() == incremental_top


def test_incremental_navigation_missing_index_fails_without_full_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice")
    refresh_navigation(root)
    (root / "wiki/concepts/index.md").unlink()
    top_before = (root / "wiki/index.md").read_bytes()

    result = refresh_navigation(root, changed_path="wiki/concepts/invoice.md")

    assert result == {
        "ok": False,
        "code": "incremental_navigation_index_missing",
        "path": "wiki/concepts/index.md",
        "error": "concept navigation index is missing; run 'uv run llm-wiki-mcp repair page-operation apply --vault <vault> --operation-id <operation-id>'",
    }
    assert (root / "wiki/index.md").read_bytes() == top_before

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

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            wiki_index._write_top_index(root)

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

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            wiki_index._write_top_index(root)

    assert target.read_text(encoding="utf-8") != "---\ntype: index\ngenerated: true\n---\n\nold index\n"
    assert target.read_text(encoding="utf-8").endswith("# Index\n\n") is False
    assert "# Index" in target.read_text(encoding="utf-8")
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []

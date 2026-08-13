from __future__ import annotations

from pathlib import Path

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.codegraph_removal import apply_codegraph_removal, plan_codegraph_removal


def _write_page(root: Path, relative: str, frontmatter: str, body: str = "正文") -> Path:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# Page\n\n{body}\n", encoding="utf-8")
    return path


def _marker(*, generated: bool = True, source_name: str = "codegraph") -> str:
    return (
        f"generated: {'true' if generated else 'false'}\n"
        "managed_by: codegraph\n"
        f"source_name: {source_name}\n"
        "retrieval_scope: project_code"
    )


def test_plan_lists_only_legacy_pages_and_raw_directories_without_writing(tmp_path: Path) -> None:
    matching = _write_page(tmp_path, "wiki/projects/demo/architecture/code-facts/src/app.md", _marker())
    _write_page(tmp_path, "wiki/projects/demo/architecture/pipelines/deploy.md", _marker())
    _write_page(tmp_path, "wiki/projects/demo/architecture/code-overview.md", _marker())
    _write_page(tmp_path, "archives/bundles/bundle-1/pages/code-facts.md", _marker())
    manual = _write_page(tmp_path, "wiki/projects/demo/architecture/notes.md", _marker(generated=False))
    non_codegraph = _write_page(tmp_path, "wiki/projects/demo/architecture/code-facts/manual.md", _marker(source_name="manual"))
    raw = tmp_path / "raw/sources/projects/demo/codegraph/graph.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}", encoding="utf-8")

    result = plan_codegraph_removal(tmp_path)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["pages"] == [
        "archives/bundles/bundle-1/pages/code-facts.md",
        "wiki/projects/demo/architecture/code-facts/src/app.md",
        "wiki/projects/demo/architecture/code-overview.md",
        "wiki/projects/demo/architecture/pipelines/deploy.md",
    ]
    assert result["directories"] == ["raw/sources/projects/demo/codegraph"]
    assert result["count"] == 5
    assert matching.is_file() and manual.is_file() and non_codegraph.is_file() and raw.is_file()


def test_apply_removes_pages_and_raw_directory_but_keeps_architecture_and_manual_pages(tmp_path: Path) -> None:
    matching = _write_page(tmp_path, "wiki/projects/demo/architecture/code-facts/src/app.md", _marker())
    _write_page(tmp_path, "wiki/projects/demo/architecture/pipelines/deploy.md", _marker())
    _write_page(tmp_path, "archives/bundles/bundle-1/pages/code-facts.md", _marker())
    manual = _write_page(tmp_path, "wiki/projects/demo/architecture/notes.md", _marker(generated=False))
    architecture = tmp_path / "wiki/projects/demo/architecture"
    raw_dir = tmp_path / "raw/sources/projects/demo/codegraph"
    raw_dir.mkdir(parents=True)
    (raw_dir / "graph.json").write_text("{}", encoding="utf-8")

    result = apply_codegraph_removal(tmp_path)

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["pages"] == [
        "archives/bundles/bundle-1/pages/code-facts.md",
        "wiki/projects/demo/architecture/code-facts/src/app.md",
        "wiki/projects/demo/architecture/pipelines/deploy.md",
    ]
    assert result["directories"] == ["raw/sources/projects/demo/codegraph"]
    assert not matching.exists()
    assert not (tmp_path / "archives/bundles/bundle-1/pages/code-facts.md").exists()
    assert not raw_dir.exists()
    assert architecture.is_dir()
    assert manual.is_file()

    second = apply_codegraph_removal(tmp_path)
    assert second["pages"] == []
    assert second["directories"] == []
    assert second["count"] == 0


def test_apply_removes_rows_from_existing_active_archive_and_raw_indexes(tmp_path: Path) -> None:
    active_page = _write_page(tmp_path, "wiki/projects/demo/architecture/code-overview.md", _marker(), "active marker")
    archive_page = _write_page(tmp_path, "archives/bundles/bundle-1/code-overview.md", _marker(), "archive marker")
    raw_file = tmp_path / "raw/sources/projects/demo/codegraph/graph.json"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_text('{"marker": "raw marker"}', encoding="utf-8")

    active_store = RetrievalIndexStore(tmp_path)
    archive_store = RetrievalIndexStore(tmp_path, scope="archive")
    raw_store = RetrievalIndexStore(tmp_path, scope="raw")
    active_store.build(active_store.iter_vault_pages())
    archive_store.build(archive_store.iter_vault_pages())
    raw_store.build(raw_store.iter_vault_pages())

    result = apply_codegraph_removal(tmp_path)

    assert result["ok"] is True
    assert set(result["projection"]) == {"active", "archive", "raw"}
    assert not active_store.search_fts("active marker")
    assert not archive_store.search_fts("archive marker")
    assert not raw_store.search_fts("raw marker")
    assert not active_page.exists()
    assert not archive_page.exists()
    assert not raw_file.exists()

from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_paths import WikiPathError, WikiPaths, create_wiki_root, safe_segment


EXPECTED_DIRS = [
    "raw/sources",
    "raw/sources/projects",
    "raw/sources/file",
    "raw/sources/references",
    "raw/sources/chat",
    "raw/assets",
    "wiki/projects",
    "wiki/concepts",
    "wiki/sources",
    "wiki/entities",
    "archives/bundles",
    ".llm-wiki/ingest-cache",
    ".llm-wiki/graph-index",
    ".llm-wiki/relation-candidates",
    ".obsidian",
]

EXPECTED_FILES = [
    "purpose.md",
    "schema.md",
    "wiki/index.md",
    "wiki/log.md",
    "wiki/overview.md",
    "wiki/concepts/index.md",
    "wiki/sources/index.md",
    "wiki/entities/index.md",
    "archives/log.md",
]


def test_create_wiki_root_creates_confirmed_directory_structure(tmp_path: Path):
    root = tmp_path / "Wiki root"

    paths = create_wiki_root(root)

    assert isinstance(paths, WikiPaths)
    assert paths.root == root.resolve()
    for relative in EXPECTED_DIRS:
        assert (root / relative).is_dir(), relative
    for relative in EXPECTED_FILES:
        assert (root / relative).is_file(), relative


def test_create_wiki_root_preserves_existing_core_files(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "purpose.md").write_text("custom purpose", encoding="utf-8")

    create_wiki_root(root)

    assert (root / "purpose.md").read_text(encoding="utf-8") == "custom purpose"


def test_create_wiki_root_writes_actionable_schema_template(tmp_path: Path):
    root = tmp_path / "vault"

    create_wiki_root(root)

    schema = (root / "schema.md").read_text(encoding="utf-8")
    assert "LLM Wiki 维护原则" in schema
    assert "raw/sources/" in schema
    assert "raw/sources/projects/<project>/codegraph/" in schema
    assert "raw/projects/<project>/codegraph/" not in schema
    assert "wiki/projects/<project>/specs/" in schema
    assert "wiki/projects/<project>/plans/" in schema
    assert "wiki/index.md" in schema
    assert "wiki_lint" in schema
    assert "generated: false" in schema


def test_project_helpers_return_confirmed_project_substructure(tmp_path: Path):
    paths = create_wiki_root(tmp_path / "vault")

    assert paths.project_root("alpha") == paths.root / "wiki" / "projects" / "alpha"
    assert paths.raw_project_root("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha"
    assert paths.raw_project_codegraph_dir("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha" / "codegraph"
    assert paths.raw_project_requirements_dir("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha" / "requirements"
    assert paths.raw_project_assets_dir("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha" / "assets"
    assert paths.project_specs_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "specs"
    assert paths.project_plans_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "plans"
    assert paths.project_architecture_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "architecture"
    assert paths.project_pipelines_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "pipelines"
    assert paths.project_troubleshooting_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "troubleshooting"
    assert paths.project_researches_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "researches"
    assert paths.concepts_dir() == paths.root / "wiki" / "concepts"
    assert paths.sources_dir() == paths.root / "wiki" / "sources"
    assert paths.entities_dir() == paths.root / "wiki" / "entities"
    assert paths.archives_dir() == paths.root / "archives"


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../escape",
        "bad/name",
        "bad\\name",
        str(Path("/") / "absolute"),
        "bad:name",
        "bad<name",
        "bad>name",
        "bad\"name",
        "bad|name",
        "bad?name",
        "bad*name",
        "bad\x00name",
        "bad\x1fname",
        "CON",
        "con.md",
        "COM1",
        "LPT9",
        "trailing.",
        "trailing ",
    ],
)
def test_safe_segment_rejects_path_escape_and_windows_invalid_values(value: str):
    with pytest.raises(WikiPathError):
        safe_segment(value)


def test_safe_segment_returns_valid_single_segment():
    assert safe_segment("Project-A_01") == "Project-A_01"

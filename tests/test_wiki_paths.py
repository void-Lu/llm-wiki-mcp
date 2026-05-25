from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_rag_mcp.wiki_paths import WikiPathError, WikiPaths, create_wiki_root, safe_segment


EXPECTED_DIRS = [
    "raw/sources",
    "raw/assets",
    "wiki/projects",
    "wiki/concepts",
    "wiki/sources",
    "wiki/queries",
    "wiki/synthesis",
    "wiki/comparisons",
    ".obsidian",
    ".llm-wiki",
]

EXPECTED_FILES = [
    "purpose.md",
    "schema.md",
    "wiki/index.md",
    "wiki/log.md",
    "wiki/overview.md",
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


def test_project_helpers_return_confirmed_project_substructure(tmp_path: Path):
    paths = create_wiki_root(tmp_path / "vault")

    assert paths.project_root("alpha") == paths.root / "wiki" / "projects" / "alpha"
    assert paths.project_code_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "code"
    assert paths.project_decisions_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "decisions"
    assert paths.project_troubleshooting_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "troubleshooting"
    assert paths.project_requirements_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "requirements"
    assert paths.concepts_dir() == paths.root / "wiki" / "concepts"
    assert paths.sources_dir() == paths.root / "wiki" / "sources"
    assert paths.queries_dir() == paths.root / "wiki" / "queries"
    assert paths.synthesis_dir() == paths.root / "wiki" / "synthesis"
    assert paths.comparisons_dir() == paths.root / "wiki" / "comparisons"


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

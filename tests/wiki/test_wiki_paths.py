from __future__ import annotations

from pathlib import Path

import pytest

from retrieval.retrieval_index import RetrievalIndexStore
from retrieval.vector_index import default_vector_index_path
from wiki.wiki_paths import (
    KNOWLEDGE_DEPENDENCIES_DB,
    PAGE_STATE_DB,
    RETRIEVAL_DB_BY_SCOPE,
    STATE_DB,
    VECTOR_INDEX,
    VECTOR_INDEX_BY_CORPUS,
    WikiPathError,
    WikiPaths,
    create_wiki_root,
    resolve_within_root,
    safe_segment,
    slug,
    translate_path_error,
    validate_wiki_page_path,
)


EXPECTED_DIRS = [
    "raw/sources",
    "raw/sources/projects",
    "raw/sources/file",
    "raw/sources/references",
    "raw/sources/chat",
    "raw/assets",
    "wiki/projects",
    "wiki/concepts",
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
    "wiki/entities/index.md",
    "archives/log.md",
]

PATH_ERROR_CODES = (
    "path_escape",
    "invalid_wiki_path",
    "navigation_index_forbidden",
    "invalid_path_component",
    "empty_segment",
)
EXPECTED_SURFACE_CODES = {
    "update": (
        "path_escape",
        "update_path_not_allowed",
        "update_path_not_allowed",
        "invalid_path_component",
        "empty_segment",
    ),
    "note_filename": (
        "path_escape",
        "invalid_wiki_path",
        "navigation_index_forbidden",
        "invalid_filename",
        "invalid_filename",
    ),
    "note_segment": (
        "path_escape",
        "invalid_wiki_path",
        "navigation_index_forbidden",
        "invalid_path_component",
        "invalid_path_component",
    ),
    "reference": (
        "path_escape",
        "path_not_allowed",
        "path_not_allowed",
        "invalid_path_component",
        "empty_segment",
    ),
    "io": (
        "path_escape",
        "invalid_wiki_path",
        "invalid_wiki_path",
        "invalid_path_component",
        "empty_segment",
    ),
    "ingest": PATH_ERROR_CODES,
    "mutation": PATH_ERROR_CODES,
    "provenance": PATH_ERROR_CODES,
}


@pytest.mark.parametrize(
    ("surface", "code", "expected"),
    [
        (surface, code, expected)
        for surface, expected_codes in EXPECTED_SURFACE_CODES.items()
        for code, expected in zip(PATH_ERROR_CODES, expected_codes, strict=True)
    ],
)
def test_translate_path_error_preserves_each_surface_contract(surface: str, code: str, expected: str) -> None:
    assert translate_path_error(code, surface) == expected


def test_translate_path_error_passes_through_unknown_surface_and_code() -> None:
    assert translate_path_error("future_code", "update") == "future_code"
    assert translate_path_error("path_escape", "future_surface") == "path_escape"


def test_resolve_within_root_returns_resolved_child(tmp_path: Path) -> None:
    root = tmp_path.resolve()

    assert resolve_within_root(root, Path("wiki/page.md")) == root / "wiki/page.md"


def test_resolve_within_root_rejects_logical_escape(tmp_path: Path) -> None:
    root = (tmp_path / "vault").resolve()
    root.mkdir()

    with pytest.raises(WikiPathError, match="resolved path escapes vault root") as raised:
        resolve_within_root(root, Path("../outside.md"))

    assert raised.value.code == "path_escape"


def test_resolve_within_root_rejects_symlink_escape(tmp_path: Path) -> None:
    root = (tmp_path / "vault").resolve()
    outside = (tmp_path / "outside").resolve()
    root.mkdir()
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(WikiPathError) as raised:
        resolve_within_root(root, Path("linked/page.md"))

    assert raised.value.code == "path_escape"


def test_create_wiki_root_creates_confirmed_directory_structure(tmp_path: Path):
    root = tmp_path / "Wiki root"

    paths = create_wiki_root(root)

    assert isinstance(paths, WikiPaths)
    assert paths.root == root.resolve()
    for relative in EXPECTED_DIRS:
        assert (root / relative).is_dir(), relative
    for relative in EXPECTED_FILES:
        assert (root / relative).is_file(), relative
    assert not (root / "wiki/archives").exists()


def test_create_wiki_root_preserves_existing_core_files(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "purpose.md").write_text("custom purpose", encoding="utf-8")

    create_wiki_root(root)

    assert (root / "purpose.md").read_text(encoding="utf-8") == "custom purpose"


def test_state_path_constants_match_retrieval_and_vector_owners(tmp_path: Path) -> None:
    root = (tmp_path / "vault").resolve()

    assert root / STATE_DB == root / Path(".llm-wiki/state.sqlite3")
    assert root / PAGE_STATE_DB == root / Path(".llm-wiki/page-state.sqlite3")
    assert root / KNOWLEDGE_DEPENDENCIES_DB == root / Path(".llm-wiki/knowledge-dependencies.sqlite3")
    for scope, relative_path in RETRIEVAL_DB_BY_SCOPE.items():
        assert RetrievalIndexStore(root, scope=scope).path == root / relative_path
    assert default_vector_index_path(root) == root / VECTOR_INDEX
    assert default_vector_index_path(root, corpus="archive") == root / VECTOR_INDEX_BY_CORPUS["archive"]


def test_llm_wiki_path_literals_have_one_source_owner() -> None:
    source_root = Path(__file__).parents[2] / "src"
    owner = source_root / "wiki" / "wiki_paths.py"
    ingest_snapshot = source_root / "wiki" / "ingest_snapshot.py"
    offenders: list[str] = []

    for path in sorted(source_root.rglob("*.py")):
        if path == owner:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ".llm-wiki" not in line:
                continue
            if path == ingest_snapshot and "tempfile.mkstemp(prefix=" in line:
                continue
            offenders.append(f"{path.relative_to(source_root)}:{line_number}")

    assert offenders == []


def test_create_wiki_root_writes_actionable_schema_template(tmp_path: Path):
    root = tmp_path / "vault"

    create_wiki_root(root)

    schema = (root / "schema.md").read_text(encoding="utf-8")
    assert "LLM Wiki 维护原则" in schema
    assert "raw/sources/" in schema
    assert "wiki_ingest" in schema
    assert "wiki_update" in schema
    assert "wiki_archive" in schema
    assert "wiki/projects/<project>/specs/" in schema
    assert "wiki/projects/<project>/plans/" in schema
    assert "wiki/index.md" in schema
    assert "archives/bundles/<yyyy>/<mm>/<archive-id>/" in schema
    assert "wiki/archives/" not in schema
    assert "generated: false" in schema


def test_project_helpers_return_confirmed_project_substructure(tmp_path: Path):
    paths = create_wiki_root(tmp_path / "vault")

    assert paths.project_root("alpha") == paths.root / "wiki" / "projects" / "alpha"
    assert paths.raw_project_root("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha"
    assert paths.raw_project_requirements_dir("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha" / "requirements"
    assert paths.raw_project_assets_dir("alpha") == paths.root / "raw" / "sources" / "projects" / "alpha" / "assets"
    assert paths.project_specs_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "specs"
    assert paths.project_plans_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "plans"
    assert paths.project_architecture_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "architecture"
    assert paths.project_code_facts_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "architecture" / "code-facts"
    assert paths.project_pipelines_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "architecture" / "pipelines"
    assert paths.project_code_overview_path("alpha") == paths.root / "wiki" / "projects" / "alpha" / "architecture" / "code-overview.md"
    assert paths.project_troubleshooting_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "troubleshooting"
    assert paths.project_researches_dir("alpha") == paths.root / "wiki" / "projects" / "alpha" / "researches"
    assert paths.concepts_dir() == paths.root / "wiki" / "concepts"
    assert paths.entities_dir() == paths.root / "wiki" / "entities"
    assert paths.archives_dir() == paths.root / "archives"


@pytest.mark.parametrize(
    ("value", "expected_code"),
    [
        ("", "empty_segment"),
        (".", "path_escape"),
        ("..", "path_escape"),
        ("../escape", "path_escape"),
        ("bad/name", "path_escape"),
        ("bad\\name", "path_escape"),
        (str(Path("/") / "absolute"), "path_escape"),
        ("bad:name", "invalid_path_component"),
        ("bad<name", "invalid_path_component"),
        ("bad>name", "invalid_path_component"),
        ("bad\"name", "invalid_path_component"),
        ("bad|name", "invalid_path_component"),
        ("bad?name", "invalid_path_component"),
        ("bad*name", "invalid_path_component"),
        ("bad\x00name", "invalid_path_component"),
        ("bad\x1fname", "invalid_path_component"),
        ("CON", "invalid_path_component"),
        ("con.md", "invalid_path_component"),
        ("COM1", "invalid_path_component"),
        ("LPT9", "invalid_path_component"),
        ("trailing.", "invalid_path_component"),
        ("trailing ", "invalid_path_component"),
    ],
)
def test_safe_segment_rejects_path_escape_and_windows_invalid_values(value: str, expected_code: str) -> None:
    with pytest.raises(WikiPathError) as raised:
        safe_segment(value)

    assert raised.value.code == expected_code


def test_safe_segment_returns_valid_single_segment():
    assert safe_segment("Project-A_01") == "Project-A_01"


@pytest.mark.parametrize(
    ("value", "expected_code"),
    [
        ("../outside.md", "path_escape"),
        ("wiki/overview.md", "invalid_wiki_path"),
        ("wiki/concepts/general/page.txt", "invalid_wiki_path"),
        ("wiki/concepts/index.md", "navigation_index_forbidden"),
        ("wiki/concepts/bad:name/page.md", "invalid_path_component"),
    ],
)
def test_validate_wiki_page_path_exposes_stable_error_codes(value: str, expected_code: str) -> None:
    with pytest.raises(WikiPathError) as raised:
        validate_wiki_page_path(value)

    assert raised.value.code == expected_code


def test_validate_wiki_page_path_navigation_index_policy_has_both_outcomes() -> None:
    with pytest.raises(WikiPathError) as raised:
        validate_wiki_page_path("wiki/concepts/index.md", allow_navigation_index=False)

    assert raised.value.code == "navigation_index_forbidden"
    assert validate_wiki_page_path("wiki/concepts/index.md", allow_navigation_index=True) == Path("wiki/concepts/index.md")
    assert validate_wiki_page_path("wiki/concepts/general/page.md") == Path("wiki/concepts/general/page.md")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("MapReduce 上下文对象 API 详解", "mapreduce-上下文对象-api-详解"),
        ("Upper_CASE", "upper_case"),
        ("!!!", "page"),
    ],
)
def test_slug_is_the_canonical_lowercase_wikilink_policy(value: str, expected: str):
    assert slug(value) == expected


def test_slug_exposes_explicit_legacy_note_filename_compatibility():
    assert slug("  修复 RESTlet: 订单/同步!!!  ", lowercase=False, fallback="", ascii_punctuation=True) == "修复-RESTlet-订单-同步"
    assert slug("Upper_CASE", lowercase=False, fallback="", ascii_punctuation=True) == "Upper-CASE"
    assert slug("!!!", lowercase=False, fallback="", ascii_punctuation=True) == ""

from pathlib import Path

import pytest

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.models import SourceConfig
from netsuite_rag_mcp.runtime_config import RuntimeConfigError, resolve_runtime_config


def test_load_config_resolves_paths(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "vault_root: .",
                "include:",
                "  - projects",
                "  - knowledge",
                "exclude:",
                "  - .git",
                "  - .obsidian",
                "  - .superpowers",
                "  - .rag-index",
                "chroma_path: .rag-index/chroma",
                "collection_name: netsuite_notes",
                "embedding_model: BAAI/bge-m3",
                "embedding_cache_path: .models",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=tmp_path / "user-data")
    config = load_config(vault, runtime_config=runtime)

    assert config.vault_root == vault.resolve()
    assert config.include_paths == [vault / "projects", vault / "knowledge"]
    assert config.exclude_names == {".git", ".obsidian", ".superpowers", ".rag-index"}
    assert config.chroma_path == runtime.chroma_path
    assert config.collection_name == "netsuite_notes"
    assert config.embedding_model == "BAAI/bge-m3"
    assert config.embedding_cache_path == runtime.embedding_cache_path
    assert config.manifest_path == runtime.manifest_path


def test_load_config_uses_runtime_paths_when_index_section_missing(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    collection: netsuite_knowledge",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=tmp_path / "user-data")
    config = load_config(vault, runtime_config=runtime)

    assert config.vault_root == vault.resolve()
    assert config.include_paths == [vault / "projects", vault / "knowledge"]
    assert config.exclude_names == {".git", ".obsidian", ".superpowers", ".rag-index"}
    assert config.chroma_path == runtime.chroma_path
    assert config.collection_name == "netsuite_knowledge"
    assert config.embedding_model == "BAAI/bge-m3"
    assert config.embedding_cache_path == runtime.embedding_cache_path
    assert config.manifest_path == runtime.manifest_path
    assert len(config.sources) == 1
    assert config.sources[0].collection == "netsuite_knowledge"


def test_load_config_requires_sources_yaml_by_default(tmp_path: Path):
    with pytest.raises(RuntimeConfigError) as exc_info:
        load_config(tmp_path)

    assert exc_info.value.code == "missing_sources_config"
    assert "rag/sources.yaml" in str(exc_info.value)


def test_load_config_uses_existing_explicit_config_path(tmp_path: Path):
    vault = tmp_path / "vault"
    explicit_config = tmp_path / "explicit-sources.yaml"
    vault.mkdir()
    explicit_config.write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  embedding_model: fake",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [knowledge]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_notes",
                "    authority: curated_note_source",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(vault, config_path=explicit_config)

    assert config.embedding_model == "fake"
    assert config.collection_name == "netsuite_notes"
    assert config.sources[0].include == ["knowledge"]


def test_load_config_rejects_missing_explicit_config_path(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "schema_version: 2\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError) as exc_info:
        load_config(vault, config_path=tmp_path / "missing-sources.yaml")

    assert exc_info.value.code == "missing_sources_config"
    assert "missing-sources.yaml" in str(exc_info.value)


def test_load_config_resolves_custom_embedding_cache_path(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "vault_root: .",
                "embedding_model: BAAI/bge-m3",
                "embedding_cache_path: models/bge",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=tmp_path / "user-data")
    config = load_config(vault, runtime_config=runtime)

    assert config.embedding_model == "BAAI/bge-m3"
    assert config.embedding_cache_path == runtime.embedding_cache_path
    assert config.manifest_path == runtime.manifest_path


def test_load_config_v1_auto_migrates_to_sources(tmp_path: Path):
    """v1 flat config should auto-migrate to a single obsidian source entry."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "vault_root: .",
                "include:",
                "  - projects",
                "  - knowledge",
                "exclude:",
                "  - .git",
                "  - .obsidian",
                "  - .superpowers",
                "  - .rag-index",
                "chroma_path: .rag-index/chroma",
                "collection_name: netsuite_notes",
                "embedding_model: BAAI/bge-m3",
                "embedding_cache_path: .models",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=tmp_path / "user-data")
    config = load_config(vault, runtime_config=runtime)

    # Source semantics still migrate; generated-state paths come from runtime storage.
    assert config.vault_root == vault.resolve()
    assert config.include_paths == [vault / "projects", vault / "knowledge"]
    assert config.exclude_names == {".git", ".obsidian", ".superpowers", ".rag-index"}
    assert config.chroma_path == runtime.chroma_path
    assert config.collection_name == "netsuite_notes"
    assert config.embedding_model == "BAAI/bge-m3"
    assert config.embedding_cache_path == runtime.embedding_cache_path
    assert config.manifest_path == runtime.manifest_path

    # sources[] is populated with single obsidian source
    assert len(config.sources) == 1
    src = config.sources[0]
    assert isinstance(src, SourceConfig)
    assert src.source_name == "obsidian"
    assert src.source_kind == "note"
    assert src.root == vault.resolve()
    assert src.include == ["projects", "knowledge"]
    assert src.exclude == [".git", ".obsidian", ".superpowers", ".rag-index"]
    assert src.file_types == ["md"]
    assert src.parser == "markdown_frontmatter_h2"
    assert src.collection == "netsuite_notes"
    assert src.authority == "curated_note_source"


def test_load_config_v2_multi_source(tmp_path: Path):
    """v2 schema with sources[] array should parse into list[SourceConfig]."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "",
                "index:",
                "  chroma_path: .rag-index/chroma",
                "  embedding_model: BAAI/bge-m3",
                "  embedding_cache_path: .models",
                "  collections:",
                "    default: netsuite_knowledge",
                "",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include:",
                "      - projects",
                "      - knowledge",
                "    exclude:",
                "      - .git",
                "      - .obsidian",
                "      - .superpowers",
                "      - .rag-index",
                "    file_types:",
                "      - md",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_knowledge",
                "    authority: curated_note_source",
                "  - source_name: suitecode",
                "    source_kind: code",
                "    root: ./scripts",
                "    include:",
                "      - scripts",
                "    exclude:",
                "      - .git",
                "    file_types:",
                "      - js",
                "    parser: suitescript_function",
                "    collection: netsuite_knowledge",
                "    authority: curated_code_source",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=tmp_path / "user-data")
    config = load_config(vault, runtime_config=runtime)

    # v2 overrides flat fields from first source
    assert config.vault_root == vault.resolve()
    assert config.collection_name == "netsuite_knowledge"
    assert config.chroma_path == runtime.chroma_path
    assert config.embedding_cache_path == runtime.embedding_cache_path
    assert config.manifest_path == runtime.manifest_path

    # sources[] has two entries
    assert len(config.sources) == 2

    obsidian_src = config.sources[0]
    assert obsidian_src.source_name == "obsidian"
    assert obsidian_src.source_kind == "note"
    assert obsidian_src.root == vault.resolve()
    assert obsidian_src.include == ["projects", "knowledge"]
    assert obsidian_src.exclude == [".git", ".obsidian", ".superpowers", ".rag-index"]
    assert obsidian_src.file_types == ["md"]
    assert obsidian_src.parser == "markdown_frontmatter_h2"
    assert obsidian_src.collection == "netsuite_knowledge"
    assert obsidian_src.authority == "curated_note_source"

    suitecode_src = config.sources[1]
    assert suitecode_src.source_name == "suitecode"
    assert suitecode_src.source_kind == "code"
    assert suitecode_src.root == vault / "scripts"
    assert suitecode_src.include == ["scripts"]
    assert suitecode_src.exclude == [".git"]
    assert suitecode_src.file_types == ["js"]
    assert suitecode_src.parser == "suitescript_function"
    assert suitecode_src.collection == "netsuite_knowledge"
    assert suitecode_src.authority == "curated_code_source"


def test_load_config_parses_wiki_library_rules(tmp_path: Path):
    vault = tmp_path / "vault"
    repo = tmp_path / "suitecloud"
    vault.mkdir()
    repo.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  collections:",
                "    default: netsuite_knowledge",
                "sources:",
                "  - source_name: huideng",
                "    source_kind: code",
                f"    root: {repo.as_posix()}",
                "    include: [src/FileCabinet/SuiteScripts, src/Objects]",
                "    exclude: [.git, node_modules, dist, build]",
                "    file_types: [js, ts, xml, json]",
                "    parser: suitescript_code_and_config",
                "    collection: netsuite_knowledge",
                "    authority: implementation_source_of_truth",
                "    file_exclude_patterns:",
                "      - src/deploy.xml",
                "      - src/manifest.xml",
                "    library_exclude_patterns:",
                "      - src/FileCabinet/SuiteScripts/tools/moment.js",
                "      - src/FileCabinet/SuiteScripts/tools/crypto-js.js",
                "    utility_allowlist:",
                "      - src/FileCabinet/SuiteScripts/tools/common_api.js",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=tmp_path / "user-data")
    config = load_config(vault, runtime_config=runtime)

    source = config.sources[0]
    assert source.file_exclude_patterns == [
        "src/deploy.xml",
        "src/manifest.xml",
    ]
    assert source.library_exclude_patterns == [
        "src/FileCabinet/SuiteScripts/tools/moment.js",
        "src/FileCabinet/SuiteScripts/tools/crypto-js.js",
    ]
    assert source.utility_allowlist == ["src/FileCabinet/SuiteScripts/tools/common_api.js"]


def test_load_config_uses_runtime_storage_even_when_sources_yaml_contains_vault_local_index_paths(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  chroma_path: .rag-index/chroma",
                "  embedding_model: BAAI/bge-m3",
                "  embedding_cache_path: .models",
                "  collections:",
                "    default: netsuite_knowledge",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [knowledge]",
                "    exclude: [.git, .obsidian, .rag-index]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_knowledge",
                "    authority: curated_note_source",
            ]
        ),
        encoding="utf-8",
    )

    runtime = resolve_runtime_config(vault_root_arg=vault)
    config = load_config(vault, runtime_config=runtime)

    # Runtime config always takes precedence over sources.yaml paths
    assert config.chroma_path == runtime.chroma_path
    assert config.manifest_path == runtime.manifest_path
    assert config.embedding_cache_path == runtime.embedding_cache_path

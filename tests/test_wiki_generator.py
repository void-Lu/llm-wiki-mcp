from dataclasses import replace
from pathlib import Path

import yaml

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.runtime_config import resolve_runtime_config
from netsuite_rag_mcp.wiki_generator import (
    _collect_wiki_source_files,
    _is_library_file,
    _is_utility_file,
    generate_suitecloud_wiki,
)


RESTLET_JS = """/**
 * @NScriptType Restlet
 * @NApiVersion 2.1
 */
define(["N/record", "./tools/common_api"], function(record, commonApi) {
  function get(context) {
    return record.load({ type: "salesorder", id: context.id });
  }
  return { get: get };
});
"""

UTILITY_JS = """define([], function() {
  function normalize(value) {
    return String(value || "").trim();
  }
  return { normalize: normalize };
});
"""

MOMENT_JS = """//! moment.js
function moment() { return "third party"; }
"""

CUSTOMSCRIPT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<script scriptid="customscript_order_sync_restlet">
  <name>Order Sync RESTlet</name>
</script>
"""


def write_sources_yaml(vault: Path, repo: Path) -> None:
    (vault / "rag").mkdir(parents=True, exist_ok=True)
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "index:",
                "  embedding_model: fake",
                "  collections:",
                "    default: netsuite_knowledge",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [projects]",
                "    exclude: [.git, .obsidian, .rag-index]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_knowledge",
                "    authority: curated_note_source",
                "  - source_name: huideng",
                "    source_kind: code",
                f"    root: {repo.as_posix()}",
                "    include: [src/FileCabinet/SuiteScripts, src/Objects]",
                "    exclude: [.git, node_modules, dist, build]",
                "    file_types: [js, ts, xml, json]",
                "    parser: suitescript_code_and_config",
                "    collection: netsuite_knowledge",
                "    authority: implementation_source_of_truth",
                "    library_exclude_patterns:",
                "      - src/FileCabinet/SuiteScripts/tools/extra-lib.js",
                "    utility_allowlist:",
                "      - src/FileCabinet/SuiteScripts/tools/common_api.js",
            ]
        ),
        encoding="utf-8",
    )


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    repo = tmp_path / "HuiDeng"
    vault.mkdir()
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL").mkdir(parents=True)
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "tools").mkdir(parents=True)
    (repo / "src" / "Objects" / "Objects_GL").mkdir(parents=True)
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "rl_order_sync.js").write_text(
        RESTLET_JS,
        encoding="utf-8",
    )
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "tools" / "common_api.js").write_text(
        UTILITY_JS,
        encoding="utf-8",
    )
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "tools" / "moment.js").write_text(MOMENT_JS, encoding="utf-8")
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "tools" / "extra-lib.js").write_text(
        MOMENT_JS,
        encoding="utf-8",
    )
    (repo / "src" / "Objects" / "Objects_GL" / "customscript_order_sync_restlet.xml").write_text(
        CUSTOMSCRIPT_XML,
        encoding="utf-8",
    )
    write_sources_yaml(vault, repo)
    return vault, repo


def load_huideng_source(vault: Path):
    runtime = resolve_runtime_config(vault_root_arg=vault, data_root=vault / ".test-data")
    config = load_config(vault, runtime_config=runtime)
    return next(source for source in config.sources if source.source_name == "huideng")


def test_collect_wiki_source_files_excludes_third_party_libraries(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    source = load_huideng_source(vault)

    files = _collect_wiki_source_files(source)
    relative_paths = [path.relative_to(repo).as_posix() for path in files]

    assert "src/FileCabinet/SuiteScripts/SuiteScripts_GL/rl_order_sync.js" in relative_paths
    assert "src/Objects/Objects_GL/customscript_order_sync_restlet.xml" in relative_paths
    assert "src/FileCabinet/SuiteScripts/tools/common_api.js" in relative_paths
    assert "src/FileCabinet/SuiteScripts/tools/moment.js" not in relative_paths
    assert "src/FileCabinet/SuiteScripts/tools/extra-lib.js" not in relative_paths


def test_is_utility_file_detects_tools_allowlist(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    source = load_huideng_source(vault)
    utility_path = repo / "src" / "FileCabinet" / "SuiteScripts" / "tools" / "common_api.js"
    script_path = repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "rl_order_sync.js"

    assert _is_utility_file(utility_path, source) is True
    assert _is_utility_file(script_path, source) is False


def test_library_detection_respects_case_insensitive_utility_allowlist(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    source = load_huideng_source(vault)
    source = replace(
        source,
        utility_allowlist=["SRC/FileCabinet/SuiteScripts/tools/COMMON_API.JS"],
        library_exclude_patterns=["src/FileCabinet/SuiteScripts/tools/common_api.js"],
    )
    utility_path = repo / "src" / "FileCabinet" / "SuiteScripts" / "tools" / "common_api.js"

    assert _is_library_file(utility_path, source) is False


def test_library_detection_treats_outside_source_root_as_excluded(tmp_path: Path):
    vault, _repo = make_repo(tmp_path)
    source = load_huideng_source(vault)
    outside_file = tmp_path / "outside.js"
    outside_file.write_text("function outside() {}", encoding="utf-8")

    assert _is_library_file(outside_file, source) is True


def frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "---"
    end = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(loaded, dict)
    return loaded


def test_generate_suitecloud_wiki_writes_script_object_flow_and_index_pages(tmp_path: Path):
    vault, repo = make_repo(tmp_path)

    result = generate_suitecloud_wiki(
        vault_root=vault,
        project="huideng",
        source_name="huideng",
        auto_index=False,
        generated_at="2026-05-24T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["written"] >= 4
    index_path = vault / "projects" / "huideng" / "wiki" / "index.md"
    script_pages = list((vault / "projects" / "huideng" / "wiki" / "scripts").glob("*.md"))
    object_pages = list((vault / "projects" / "huideng" / "wiki" / "objects").glob("*.md"))
    flow_pages = list((vault / "projects" / "huideng" / "wiki" / "flows").glob("*.md"))

    assert index_path.is_file()
    assert len(script_pages) == 2
    assert len(object_pages) == 1
    assert len(flow_pages) == 1

    script_fm = frontmatter(next(path for path in script_pages if "rl-order-sync" in path.name))
    assert script_fm["type"] == "generated_wiki"
    assert script_fm["project"] == "huideng"
    assert script_fm["generated"] is True
    assert script_fm["do_not_edit"] is True
    assert script_fm["source_repo"] == "huideng"
    assert script_fm["archived"] is False
    assert script_fm["script_type"] == "restlet"
    assert "src/FileCabinet/SuiteScripts/SuiteScripts_GL/rl_order_sync.js" in script_fm["source_path"]

    utility_fm = frontmatter(next(path for path in script_pages if "common-api" in path.name))
    assert utility_fm["script_type"] == "utility"

    index_text = index_path.read_text(encoding="utf-8")
    assert "rl_order_sync.js" in index_text
    assert "customscript_order_sync_restlet.xml" in index_text
    assert "inferred-relationships.md" in index_text


def test_generate_suitecloud_wiki_redacts_sensitive_values(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    sensitive = repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "rl_order_sync.js"
    sensitive.write_text(RESTLET_JS + "\nvar token = 'sk-abc1234567890';\n", encoding="utf-8")

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)

    assert result["ok"] is True
    generated_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (vault / "projects" / "huideng" / "wiki").rglob("*.md")
    )
    assert "sk-abc1234567890" not in generated_text
    assert "[REDACTED_SECRET]" in generated_text

from dataclasses import replace
import os
from pathlib import Path

import pytest
import yaml

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.indexer import index_sources
from netsuite_rag_mcp.retriever import search_netsuite_knowledge
from netsuite_rag_mcp.runtime_config import resolve_runtime_config
from netsuite_rag_mcp.vector_store import FakeEmbedder
from netsuite_rag_mcp.wiki_generator import (
    _collect_wiki_source_files,
    _is_library_file,
    _is_utility_file,
    generate_suitecloud_wiki,
    write_wiki_summaries,
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
    # Place the secret in the file header (a comment) so it appears in the truncated excerpt
    sensitive.write_text(
        "/**\n * @NScriptType Restlet\n * @NApiVersion 2.1\n * token: sk-abc1234567890\n */\n"
        + 'define(["N/record"], function(record) { return {}; });\n',
        encoding="utf-8",
    )

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)

    assert result["ok"] is True
    generated_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (vault / "projects" / "huideng" / "wiki").rglob("*.md")
    )
    assert "sk-abc1234567890" not in generated_text
    assert "[REDACTED_SECRET]" in generated_text


def test_generate_suitecloud_wiki_refuses_to_overwrite_manual_page(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    manual = vault / "projects" / "huideng" / "wiki" / "index.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("---\ntype: manual\n---\n\n# Manual", encoding="utf-8")

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)

    assert result["ok"] is False
    assert result["code"] == "manual_wiki_page_exists"
    assert manual.read_text(encoding="utf-8") == "---\ntype: manual\n---\n\n# Manual"


def test_removed_script_page_is_archived_not_deleted(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    first = generate_suitecloud_wiki(
        vault,
        "huideng",
        "huideng",
        auto_index=False,
        generated_at="2026-05-24T00:00:00+00:00",
    )
    assert first["ok"] is True
    script_dir = vault / "projects" / "huideng" / "wiki" / "scripts"
    script_page = next(path for path in script_dir.glob("*.md") if "rl-order-sync" in path.name)
    original_name = script_page.name

    (repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "rl_order_sync.js").unlink()
    second = generate_suitecloud_wiki(
        vault,
        "huideng",
        "huideng",
        auto_index=False,
        generated_at="2026-05-25T00:00:00+00:00",
    )

    assert second["ok"] is True
    assert second["archived"] == 1
    assert not (script_dir / original_name).exists()
    archive_page = vault / "projects" / "huideng" / "wiki" / "archive" / "scripts" / original_name
    assert archive_page.is_file()
    fm = frontmatter(archive_page)
    assert fm["archived"] is True
    assert fm["archived_reason"] == "source_removed"
    assert fm["former_source_path"] == "src/FileCabinet/SuiteScripts/SuiteScripts_GL/rl_order_sync.js"

    index_text = (vault / "projects" / "huideng" / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "rl_order_sync.js" not in index_text


def test_generated_wiki_pages_can_be_indexed_and_searched(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)
    assert result["ok"] is True

    index_result = index_sources(vault, source_names=["obsidian"], mode="full", embedder=FakeEmbedder())
    assert index_result["total_indexed"] >= 1

    search_result = search_netsuite_knowledge(
        vault,
        "Order Sync RESTlet",
        filters={"type": "generated_wiki"},
        top_k=5,
        embedder=FakeEmbedder(),
        content_type="generated_wiki",
    )

    assert search_result["results"]
    assert all(row["metadata"].get("type") == "generated_wiki" for row in search_result["results"])


def test_archived_wiki_pages_are_excluded_from_current_search_by_default(tmp_path: Path):
    vault, repo = make_repo(tmp_path)
    first = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)
    assert first["ok"] is True
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "rl_order_sync.js").unlink()
    second = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)
    assert second["ok"] is True

    index_sources(vault, source_names=["obsidian"], mode="full", embedder=FakeEmbedder())

    current = search_netsuite_knowledge(
        vault,
        "Order Sync RESTlet",
        filters={"type": "generated_wiki"},
        top_k=10,
        embedder=FakeEmbedder(),
        content_type="generated_wiki",
    )
    historical = search_netsuite_knowledge(
        vault,
        "Order Sync RESTlet",
        filters={"type": "generated_wiki"},
        top_k=10,
        embedder=FakeEmbedder(),
        content_type="generated_wiki",
        include_archived=True,
    )

    assert all(row["metadata"].get("archived") is not True for row in current["results"])
    assert any(row["metadata"].get("archived") is True for row in historical["results"])


def test_huideng_repo_structure_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo_root = os.environ.get("NETSUITE_RAG_HUIDENG_REPO_ROOT")
    if not repo_root:
        pytest.skip("NETSUITE_RAG_HUIDENG_REPO_ROOT is not set")

    repo = Path(repo_root)
    if not repo.exists():
        pytest.skip(f"HuiDeng repo does not exist: {repo}")

    vault = tmp_path / "vault"
    vault.mkdir()
    write_sources_yaml(vault, repo)

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)

    assert result["ok"] is True
    assert result["written"] > 0
    generated_paths = set(result["paths"])
    assert any(path.startswith("projects/huideng/wiki/scripts/") for path in generated_paths)
    assert any(path.startswith("projects/huideng/wiki/objects/") for path in generated_paths)
    source_paths = [str(frontmatter(path).get("source_path", "")) for path in (vault / "projects" / "huideng" / "wiki").rglob("*.md")]
    assert not any(path.endswith("src/FileCabinet/SuiteScripts/tools/moment.js") for path in source_paths)
    assert not any(path.endswith("src/FileCabinet/SuiteScripts/tools/crypto-js.js") for path in source_paths)


def test_wiki_extracts_related_objects_and_scripts(tmp_path: Path):
    """Static analysis should populate related_objects and related_scripts from code."""
    vault, repo = make_repo(tmp_path)
    script_with_refs = (
        '/**\n * @NScriptType MapReduceScript\n * @NApiVersion 2.1\n */\n'
        'define(["N/record", "N/search", "N/task"], function(record, search, task) {\n'
        '  const REC_TYPE = "customrecord_con_deposit_received";\n'
        '  function getInputData() {\n'
        '    var s = search.load({id: "customsearch_con_intermediate_table"});\n'
        '    task.create({taskType: task.TaskType.MAP_REDUCE, scriptId: "customscript_hc_mr_vendpay"});\n'
        '    return s;\n'
        '  }\n'
        '  return {getInputData: getInputData};\n'
        '});\n'
    )
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "mr_test_refs.js").write_text(
        script_with_refs, encoding="utf-8"
    )

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)
    assert result["ok"] is True

    wiki_dir = vault / "projects" / "huideng" / "wiki" / "scripts"
    page = next(p for p in wiki_dir.glob("*.md") if "mr-test-refs" in p.name)
    fm = frontmatter(page)
    content = page.read_text(encoding="utf-8")

    assert "customrecord_con_deposit_received" in fm["related_objects"]
    assert "customsearch_con_intermediate_table" in fm["related_objects"]
    assert "customscript_hc_mr_vendpay" in fm["related_scripts"]
    assert "## 关联对象" in content
    assert "## 关联脚本" in content


def test_wiki_llm_summary_returns_prompts_when_enabled(tmp_path: Path):
    """When llm_summary=True, result includes summary_prompts for each script."""
    vault, repo = make_repo(tmp_path)

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False, llm_summary=True)
    assert result["ok"] is True
    assert "summary_prompts" in result

    prompts = result["summary_prompts"]
    assert len(prompts) >= 1
    # Check structure of a prompt entry
    prompt = prompts[0]
    assert "wiki_path" in prompt
    assert "script_name" in prompt
    assert "script_type" in prompt
    assert "dependencies" in prompt
    assert "functions" in prompt
    assert "header_excerpt" in prompt


def test_wiki_llm_summary_not_returned_when_disabled(tmp_path: Path):
    """When llm_summary=False (default), result does not include summary_prompts."""
    vault, repo = make_repo(tmp_path)

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False, llm_summary=False)
    assert result["ok"] is True
    assert "summary_prompts" not in result


def test_wiki_source_excerpt_is_truncated(tmp_path: Path):
    """Source excerpt should only contain the file header, not full source."""
    vault, repo = make_repo(tmp_path)
    # Write a long script
    long_script = (
        '/**\n * @NScriptType Suitelet\n * @NApiVersion 2.1\n */\n'
        'define(["N/ui/serverWidget"], function(serverWidget) {\n'
        + '  function onRequest(ctx) {\n' + '    // line\n' * 100
        + '  }\n  return {onRequest: onRequest};\n});\n'
    )
    (repo / "src" / "FileCabinet" / "SuiteScripts" / "SuiteScripts_GL" / "sl_long.js").write_text(
        long_script, encoding="utf-8"
    )

    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False)
    assert result["ok"] is True

    wiki_dir = vault / "projects" / "huideng" / "wiki" / "scripts"
    page = next(p for p in wiki_dir.glob("*.md") if "sl-long" in p.name)
    content = page.read_text(encoding="utf-8")

    assert "// ... (完整源码见源文件)" in content
    # The full 100 repeated lines should NOT be in the wiki
    assert content.count("// line") < 40


def test_write_wiki_summaries_inserts_summary_section(tmp_path: Path):
    """write_wiki_summaries should insert a business summary section into existing wiki pages."""
    vault, repo = make_repo(tmp_path)
    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False, llm_summary=True)
    assert result["ok"] is True

    prompts = result["summary_prompts"]
    assert len(prompts) >= 1

    # Simulate model generating summaries
    summaries = [
        {"wiki_path": prompts[0]["wiki_path"], "summary": "这是一个测试业务摘要。"}
    ]
    write_result = write_wiki_summaries(vault, "huideng", summaries)
    assert write_result["ok"] is True
    assert write_result["written"] == 1

    # Verify the summary was written into the wiki page
    page = vault / prompts[0]["wiki_path"]
    content = page.read_text(encoding="utf-8")
    assert "## 业务语义摘要" in content
    assert "这是一个测试业务摘要。" in content
    # Summary should appear before dependencies
    assert content.index("## 业务语义摘要") < content.index("## 依赖模块")


def test_write_wiki_summaries_replaces_existing_summary(tmp_path: Path):
    """write_wiki_summaries should replace an existing summary if called again."""
    vault, repo = make_repo(tmp_path)
    result = generate_suitecloud_wiki(vault, "huideng", "huideng", auto_index=False, llm_summary=True)
    prompts = result["summary_prompts"]

    # Write first summary
    write_wiki_summaries(vault, "huideng", [
        {"wiki_path": prompts[0]["wiki_path"], "summary": "第一版摘要。"}
    ])
    # Write second summary (should replace)
    write_wiki_summaries(vault, "huideng", [
        {"wiki_path": prompts[0]["wiki_path"], "summary": "第二版摘要。"}
    ])

    page = vault / prompts[0]["wiki_path"]
    content = page.read_text(encoding="utf-8")
    assert "第一版摘要。" not in content
    assert "第二版摘要。" in content
    # Should only have one summary section
    assert content.count("## 业务语义摘要") == 1

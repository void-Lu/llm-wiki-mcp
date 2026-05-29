from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_ingest import ingest_codegraph, rescan_source, staged_wiki_ingest
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


class FakeCodeGraphClient:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def status(self) -> dict[str, Any]:
        self.calls.append(("status", ""))
        return self.responses.get("status", {"ok": True, "data": {"indexed": True, "files": 2}})

    def files(self) -> dict[str, Any]:
        self.calls.append(("files", ""))
        return self.responses.get("files", {"ok": True, "data": {"files": ["src/a.js"]}})

    def context(self, query: str) -> dict[str, Any]:
        self.calls.append(("context", query))
        return self.responses.get(
            "context",
            {
                "ok": True,
                "data": {
                    "nodes": [
                        {
                            "symbol": "onRequest",
                            "name": "onRequest",
                            "file": "src/FileCabinet/SuiteScripts/sl.js",
                            "path": "src/FileCabinet/SuiteScripts/sl.js",
                            "line_start": 10,
                            "line_end": 20,
                            "snippet": "function onRequest(context) { return true; }",
                        }
                    ]
                },
            },
        )

    def impact(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("impact", symbol))
        return self.responses.get("impact", {"ok": True, "data": {"symbol": symbol, "affected": []}})

    def graph_snapshot(self) -> dict[str, Any]:
        self.calls.append(("graph_snapshot", ""))
        return self.responses.get("graph_snapshot", {"ok": False, "code": "unsupported", "error": "unsupported"})

    def callers(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("callers", symbol))
        return self.responses.get("callers", {"ok": True, "data": {}})

    def callees(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("callees", symbol))
        return self.responses.get("callees", {"ok": True, "data": {}})


def test_ingest_codegraph_writes_snapshot_source_page_code_page_and_indexes(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient()

    result = ingest_codegraph(root, project="alpha", source_name="main", query="Suitelet entry", client=client)

    assert result["ok"] is True
    snapshot = root / "raw/sources/codegraph/alpha/context.json"
    assert snapshot.is_file()
    source_page = root / "wiki/sources/projects/alpha/main.md"
    assert source_page.is_file()
    code_page = root / "wiki/projects/alpha/code/onrequest.md"
    assert code_page.is_file()
    project_index = root / "wiki/projects/alpha/index.md"
    assert project_index.is_file()
    assert (root / "wiki/index.md").is_file()
    assert (root / "wiki/overview.md").is_file()
    assert "## [" in (root / "wiki/log.md").read_text(encoding="utf-8")
    assert not (root / "wiki/projects/alpha/objects").exists()
    assert not (root / "wiki/code").exists()

    frontmatter = yaml.safe_load(code_page.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["type"] == "code_fact"
    assert frontmatter["generated"] is True
    assert frontmatter["sources"] == ["raw/sources/codegraph/alpha/context.json"]
    assert frontmatter["codegraph_tool"] == "context"
    assert frontmatter["source_path"] == "src/FileCabinet/SuiteScripts/sl.js"
    assert frontmatter["symbol"] == "onRequest"


def test_ingest_codegraph_returns_unavailable_error(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient({"status": {"ok": False, "code": "codegraph_unavailable", "error": "missing"}})

    result = ingest_codegraph(root, project="alpha", source_name="main", client=client)

    assert result["ok"] is False
    assert result["code"] == "codegraph_unavailable"


def test_ingest_codegraph_extracts_camelcase_fields(tmp_path: Path):
    """CodeGraph returns filePath/startLine/endLine/signature in camelCase."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient({
        "context": {
            "ok": True,
            "data": {
                "nodes": [
                    {
                        "name": "save_obsidian_note",
                        "qualifiedName": "save_obsidian_note",
                        "kind": "function",
                        "filePath": "src/netsuite_llm_wiki_mcp/note_writer.py",
                        "startLine": 142,
                        "endLine": 229,
                        "signature": "(note_type: str, title: str, content: str) -> dict[str, Any]",
                    }
                ]
            },
        }
    })

    result = ingest_codegraph(root, project="mywiki", source_name="cg", client=client)
    assert result["ok"] is True

    code_page = root / "wiki/projects/mywiki/code/save_obsidian_note.md"
    assert code_page.is_file()
    content = code_page.read_text(encoding="utf-8")
    fm = yaml.safe_load(content.split("---", 2)[1])
    assert fm["source_path"] == "src/netsuite_llm_wiki_mcp/note_writer.py"
    assert fm["line_start"] == 142
    assert fm["line_end"] == 229
    assert fm["symbol"] == "save_obsidian_note"
    assert "Kind: `function`" in content
    assert "## Signature" in content
    assert "(note_type: str, title: str, content: str)" in content


def test_ingest_codegraph_uses_codeblocks_and_edges_for_code_facts(tmp_path: Path):
    """Code fact pages should include CodeGraph source code blocks and call relationships."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient({
        "context": {
            "ok": True,
            "data": {
                "nodes": [
                    {
                        "id": "function:save",
                        "name": "save_obsidian_note",
                        "qualifiedName": "save_obsidian_note",
                        "kind": "function",
                        "filePath": "src/netsuite_llm_wiki_mcp/note_writer.py",
                        "startLine": 142,
                        "endLine": 229,
                        "signature": "(note_type: str, title: str) -> dict[str, Any]",
                    },
                    {
                        "id": "function:overview",
                        "name": "refresh_overview",
                        "qualifiedName": "refresh_overview",
                        "kind": "function",
                        "filePath": "src/netsuite_llm_wiki_mcp/wiki_overview.py",
                        "startLine": 10,
                        "endLine": 54,
                    },
                ],
                "edges": [
                    {"source": "function:save", "target": "function:overview", "kind": "calls", "line": 217},
                ],
                "codeBlocks": [
                    {
                        "filePath": "src/netsuite_llm_wiki_mcp/note_writer.py",
                        "startLine": 142,
                        "endLine": 229,
                        "language": "python",
                        "content": "def save_obsidian_note(...):\n    refresh_overview(root)\n    return {'ok': True}",
                        "nodeName": "save_obsidian_note",
                        "nodeKind": "function",
                    }
                ],
            },
        }
    })

    result = ingest_codegraph(root, project="mywiki", source_name="cg", client=client)
    assert result["ok"] is True

    content = (root / "wiki/projects/mywiki/code/save_obsidian_note.md").read_text(encoding="utf-8")
    assert "## Source Code" in content
    assert "def save_obsidian_note" in content
    assert "refresh_overview(root)" in content
    assert "## Relationships" in content
    assert "calls → `refresh_overview`" in content


def test_ingest_codegraph_prefers_full_graph_snapshot_for_project_structure(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient({
        "graph_snapshot": {
            "ok": True,
            "data": {
                "files": [
                    {"path": "src/pkg/a.py", "language": "python", "nodeCount": 2, "size": 80},
                    {"path": "src/pkg/b.py", "language": "python", "nodeCount": 1, "size": 50},
                ],
                "nodes": [
                    {
                        "id": "function:a",
                        "kind": "function",
                        "name": "a",
                        "qualifiedName": "a",
                        "filePath": "src/pkg/a.py",
                        "language": "python",
                        "startLine": 1,
                        "endLine": 3,
                        "signature": "() -> None",
                    },
                    {
                        "id": "function:b",
                        "kind": "function",
                        "name": "b",
                        "qualifiedName": "b",
                        "filePath": "src/pkg/b.py",
                        "language": "python",
                        "startLine": 1,
                        "endLine": 2,
                    },
                ],
                "edges": [
                    {"source": "function:a", "target": "function:b", "kind": "calls", "line": 2},
                ],
            },
        }
    })

    result = ingest_codegraph(root, project="alpha", source_name="main", client=client)

    assert result["ok"] is True
    assert (root / "raw/sources/codegraph/alpha/graph.json").is_file()
    overview_page = root / "wiki/projects/alpha/code/overview.md"
    assert overview_page.is_file()
    overview = overview_page.read_text(encoding="utf-8")
    assert "## Global Logic Chain" in overview
    assert "`a` calls → `b`" in overview
    a_page = root / "wiki/projects/alpha/code/src/pkg/a.md"
    assert a_page.is_file()
    content = a_page.read_text(encoding="utf-8")
    assert "## Symbols" in content
    assert "`a`" in content
    assert "## Outgoing Relationships" in content
    assert "calls → `b`" in content




def test_staged_wiki_ingest_returns_analysis_prompt_and_cache_hit(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    result = staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source, language="zh-CN")

    assert result["ok"] is True
    assert result["stage"] == "prepare_analysis"
    assert result["status"] == "needs_model"
    assert result["source_hash"]
    assert result["classification_context"] == ["notes.md"]
    assert result["next_call"]["tool"] == "wiki_ingest_llm"
    assert "prompt" in result
    assert (root / "raw/sources/file/alpha/docs/notes.md").is_file()

    second = staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    assert second["status"] == "skipped"
    assert second["code"] == "source_unchanged"



def test_rescan_source_writes_snapshot_cache_and_reports_changed(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    result = rescan_source(root, project="alpha", source_name="docs", source_path=source)

    assert result["ok"] is True
    assert result["status"] == "changed"
    assert result["stage"] == "rescan"
    assert result["source_hash"]
    assert result["classification_context"] == ["notes.md"]
    assert result["paths"] == ["raw/sources/file/alpha/docs/notes.md", "raw/sources/file/alpha/docs/manifest.json"]
    assert (root / ".llm-wiki/ingest-cache/file/alpha/docs.json").is_file()
    assert (root / "raw/sources/file/alpha/docs/notes.md").read_text(encoding="utf-8").startswith("# Notes")



def test_rescan_source_reports_unchanged_for_same_hash(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    first = rescan_source(root, project="alpha", source_name="docs", source_path=source)
    second = rescan_source(root, project="alpha", source_name="docs", source_path=source)

    assert first["status"] == "changed"
    assert second["ok"] is True
    assert second["status"] == "unchanged"
    assert second["source_hash"] == first["source_hash"]
    assert second["paths"] == []



def test_rescan_source_removes_stale_snapshots_after_source_deletion(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("# A\n\nFirst", encoding="utf-8")
    (source / "b.md").write_text("# B\n\nRemove me", encoding="utf-8")

    first = rescan_source(root, project="alpha", source_name="docs", source_path=source)
    (source / "b.md").unlink()
    (source / "a.md").write_text("# A\n\nChanged", encoding="utf-8")
    second = rescan_source(root, project="alpha", source_name="docs", source_path=source)

    assert first["status"] == "changed"
    assert second["status"] == "changed"
    assert not (root / "raw/sources/file/alpha/docs/b.md").exists()
    manifest = json.loads((root / "raw/sources/file/alpha/docs/manifest.json").read_text(encoding="utf-8"))
    assert [item["relative_path"] for item in manifest] == ["a.md"]



def test_rescan_source_cache_is_source_type_aware(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    file_result = rescan_source(root, project="alpha", source_name="docs", source_path=source, source_type="file")
    manual_result = rescan_source(root, project="alpha", source_name="docs", source_path=source, source_type="manual")

    assert file_result["status"] == "changed"
    assert manual_result["status"] == "changed"
    assert manual_result["source_hash"] == file_result["source_hash"]
    assert manual_result["paths"] == ["raw/sources/manual/alpha/docs/notes.md", "raw/sources/manual/alpha/docs/manifest.json"]
    assert (root / "raw/sources/manual/alpha/docs/notes.md").is_file()



def test_rescan_source_reuses_source_validation(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("TOKEN=secret", encoding="utf-8")

    result = rescan_source(root, project="alpha", source_name="docs", source_path=source)

    assert result["ok"] is False
    assert result["code"] == "no_supported_sources"



def test_ingest_codegraph_skips_unchanged_context(tmp_path: Path):
    import time

    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient()

    first = ingest_codegraph(root, project="alpha", source_name="main", query="Suitelet entry", client=client)
    code_page = root / "wiki/projects/alpha/code/onrequest.md"
    first_mtime = code_page.stat().st_mtime

    time.sleep(0.05)

    second = ingest_codegraph(root, project="alpha", source_name="main", query="Suitelet entry", client=client)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second.get("status") == "unchanged"
    assert code_page.stat().st_mtime == first_mtime



def test_staged_wiki_ingest_requires_analysis_and_generation(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    missing_analysis = staged_wiki_ingest(root, "prepare_generation", project="alpha", source_name="docs")
    missing_generation = staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs")

    assert missing_analysis["code"] == "missing_analysis"
    assert missing_generation["code"] == "missing_generation"


def test_staged_wiki_ingest_prepare_generation_points_back_to_llm_tool(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nAlpha content", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    result = staged_wiki_ingest(root, "prepare_generation", project="alpha", source_name="docs", analysis={"concepts": ["Alpha"]})

    assert result["ok"] is True
    assert result["next_call"]["tool"] == "wiki_ingest_llm"




def test_staged_wiki_ingest_rejects_invalid_source_type(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nAlpha content", encoding="utf-8")

    result = staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source, source_type="../escape")

    assert result["ok"] is False
    assert result["code"] == "path_escape"

    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("TOKEN=secret", encoding="utf-8")

    result = staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    assert result["ok"] is False
    assert result["code"] == "no_supported_sources"


def test_staged_wiki_ingest_rejects_cross_project_generation_path(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nAlpha content", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    result = staged_wiki_ingest(
        root,
        "apply_generation",
        project="alpha",
        source_name="docs",
        generation={"pages": [{"path": "wiki/projects/beta/code/x.md", "title": "Wrong Project"}]},
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_generated_path"


def test_staged_wiki_ingest_rejects_invalid_project_subdir(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nAlpha content", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    result = staged_wiki_ingest(
        root,
        "apply_generation",
        project="alpha",
        source_name="docs",
        generation={"pages": [{"path": "wiki/projects/alpha/random/x.md", "title": "Bad Subdir"}]},
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_generated_path"


def test_staged_wiki_ingest_applies_generation_with_summary_fallback(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nAlpha content", encoding="utf-8")

    prepared = staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)
    assert prepared["ok"] is True
    generation = {
        "pages": [
            {
                "path": "wiki/concepts/alpha/generated.md",
                "title": "Generated Concept",
                "type": "concept",
                "summary": "Generated summary",
                "body": "Generated body",
            }
        ]
    }

    result = staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    assert result["ok"] is True
    assert "wiki/sources/concepts/alpha/docs.md" in result["paths"]
    assert "wiki/concepts/alpha/generated.md" in result["paths"]
    generated = root / "wiki/concepts/alpha/generated.md"
    frontmatter = yaml.safe_load(generated.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["sources"] == ["raw/sources/file/alpha/docs/source.md"]
    assert (root / "wiki/overview.md").is_file()
    assert "llm_ingest" in (root / "wiki/log.md").read_text(encoding="utf-8")


def test_prepare_analysis_resolves_relative_source_path_against_vault_root(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw_dir = root / "raw" / "sources" / "suitescript-modules"
    raw_dir.mkdir(parents=True)
    (raw_dir / "n-action.md").write_text("# N/action\n\nContent", encoding="utf-8")

    result = staged_wiki_ingest(
        root,
        "prepare_analysis",
        project="suitescript-modules",
        source_name="n-action",
        source_path="raw/sources/suitescript-modules/n-action.md",
    )

    assert result["ok"] is True
    assert result["stage"] == "prepare_analysis"


def test_apply_generation_writes_source_summary_in_hierarchical_directory(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nContent", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    generation = {"source_summary": {"title": "Title", "summary": "Summary", "body": "Body"}, "pages": []}
    result = staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    assert result["ok"] is True
    assert "wiki/sources/concepts/alpha/docs.md" in result["paths"]
    assert (root / "wiki/sources/concepts/alpha/docs.md").is_file()


def test_apply_generation_accepts_string_source_summary(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nContent", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    generation = {"source_summary": "This is a plain string summary", "pages": []}
    result = staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    assert result["ok"] is True
    written = (root / "wiki/sources/concepts/alpha/docs.md").read_text(encoding="utf-8")
    assert "plain string summary" in written


def test_apply_generation_collects_top_level_concept_key(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nContent", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    generation = {
        "source_summary": {"title": "T", "summary": "S", "body": "B"},
        "concept": {"path": "wiki/concepts/alpha/my-concept.md", "title": "My Concept", "type": "concept", "summary": "CS", "body": "CB"},
    }
    result = staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    assert result["ok"] is True
    assert "wiki/concepts/alpha/my-concept.md" in result["paths"]
    assert (root / "wiki/concepts/alpha/my-concept.md").is_file()


def test_two_stage_prepare_and_apply(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Module\n\nSome content about N/action", encoding="utf-8")

    prepared = staged_wiki_ingest(root, "prepare", project="alpha", source_name="docs", source_path=source)
    assert prepared["ok"] is True
    assert prepared["stage"] == "prepare"
    assert prepared["status"] == "needs_model"
    assert "prompt" in prepared
    assert "expected_response_schema" in prepared
    assert prepared["next_call"]["stage"] == "apply"

    generation = {
        "source_summary": {"title": "Alpha Docs", "summary": "Summary of alpha docs", "body": "Body content"},
        "pages": [{"path": "wiki/concepts/alpha/my-concept.md", "title": "My Concept", "type": "concept", "summary": "CS", "body": "CB"}],
    }
    result = staged_wiki_ingest(root, "apply", project="alpha", source_name="docs", generation=generation)
    assert result["ok"] is True
    assert "wiki/sources/concepts/alpha/docs.md" in result["paths"]
    assert "wiki/concepts/alpha/my-concept.md" in result["paths"]


def test_two_stage_prepare_skips_unchanged_source(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Module\n\nContent", encoding="utf-8")

    r1 = staged_wiki_ingest(root, "prepare", project="alpha", source_name="docs", source_path=source)
    assert r1["ok"] is True
    assert r1["status"] == "needs_model"

    r2 = staged_wiki_ingest(root, "prepare", project="alpha", source_name="docs", source_path=source)
    assert r2["ok"] is True
    assert r2["status"] == "skipped"
    assert r2["code"] == "source_unchanged"


def test_ingest_codegraph_filters_vendored_files_from_overview(tmp_path: Path):
    """Vendored/minified libraries should not dominate the overview logic chain."""
    root = tmp_path / "vault"
    create_wiki_root(root)

    # papaparse.js has 150 nodes (above threshold) and many internal edges
    vendor_nodes = [
        {"id": f"v{i}", "name": f"_internal{i}", "kind": "function", "filePath": "src/lib/papaparse.js", "startLine": i, "endLine": i + 5}
        for i in range(150)
    ]
    vendor_edges = [
        {"source": f"v{i}", "target": f"v{i+1}", "kind": "calls"}
        for i in range(149)
    ]
    # Business code: 3 nodes across 2 files with cross-file calls
    biz_nodes = [
        {"id": "b1", "name": "handleOrder", "kind": "function", "filePath": "src/scripts/order.js", "startLine": 1, "endLine": 20},
        {"id": "b2", "name": "calcTotal", "kind": "function", "filePath": "src/scripts/calc.js", "startLine": 1, "endLine": 10},
        {"id": "b3", "name": "saveRecord", "kind": "function", "filePath": "src/scripts/order.js", "startLine": 25, "endLine": 40},
    ]
    biz_edges = [
        {"source": "b1", "target": "b2", "kind": "calls"},
        {"source": "b1", "target": "b3", "kind": "calls"},
    ]

    graph_data = {
        "files": [
            {"path": "src/lib/papaparse.js", "language": "javascript", "nodeCount": 150},
            {"path": "src/scripts/order.js", "language": "javascript", "nodeCount": 2},
            {"path": "src/scripts/calc.js", "language": "javascript", "nodeCount": 1},
        ],
        "nodes": vendor_nodes + biz_nodes,
        "edges": vendor_edges + biz_edges,
    }

    client = FakeCodeGraphClient({
        "status": {"ok": True, "data": {"indexed": True}},
        "files": {"ok": True, "data": {"files": graph_data["files"]}},
        "context": {"ok": True, "data": {}},
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    result = ingest_codegraph(root, project="demo", source_name="main", client=client)
    assert result["ok"] is True

    overview = (root / "wiki/projects/demo/code/overview.md").read_text(encoding="utf-8")
    assert "handleOrder" in overview
    assert "calcTotal" in overview
    # Vendor internal functions should be filtered out
    assert "_internal0" not in overview
    assert "_internal50" not in overview

    # No code fact page generated for the vendored file
    assert not (root / "wiki/projects/demo/code/src/lib/papaparse.md").exists()
    # Business files still get pages
    assert (root / "wiki/projects/demo/code/src/scripts/order.md").exists()
    assert (root / "wiki/projects/demo/code/src/scripts/calc.md").exists()


def test_is_project_source_filters_known_vendor_stems():
    from netsuite_llm_wiki_mcp.wiki_ingest import _is_project_source

    assert _is_project_source("src/scripts/order.js") is True
    assert _is_project_source("src/lib/papaparse.js") is False
    assert _is_project_source("src/vendor/lodash.min.js") is False
    assert _is_project_source("node_modules/express/index.js") is False
    assert _is_project_source("") is False


def test_apply_generation_normalizes_wikilink_targets_to_lowercase(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nContent", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    generation = {
        "pages": [{
            "path": "wiki/concepts/alpha/my-concept.md",
            "title": "My Concept",
            "type": "concept",
            "summary": "S",
            "body": "See [[User-Event-Script]] and [[RESTlet]] for details.",
        }]
    }
    staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    content = (root / "wiki/concepts/alpha/my-concept.md").read_text(encoding="utf-8")
    assert "[[user-event-script]]" in content
    assert "[[restlet]]" in content
    assert "[[User-Event-Script]]" not in content
    assert "[[RESTlet]]" not in content


def test_apply_generation_preserves_wikilink_display_text(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nContent", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    generation = {
        "pages": [{
            "path": "wiki/concepts/alpha/alias-test.md",
            "title": "Alias Test",
            "type": "concept",
            "summary": "S",
            "body": "Use [[Suitelet|Suitelet Script]] to handle requests.",
        }]
    }
    staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    content = (root / "wiki/concepts/alpha/alias-test.md").read_text(encoding="utf-8")
    assert "[[suitelet|Suitelet Script]]" in content
    assert "[[Suitelet|" not in content


def test_apply_generation_does_not_normalize_wikilinks_inside_code_blocks(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nContent", encoding="utf-8")
    staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    body = (
        "Normal link: [[User-Event-Script]]\n\n"
        "```javascript\n"
        "// [[User-Event-Script]] should not be changed\n"
        "```\n\n"
        "Inline: `[[Suitelet]]` should not be changed."
    )
    generation = {
        "pages": [{
            "path": "wiki/concepts/alpha/code-test.md",
            "title": "Code Test",
            "type": "concept",
            "summary": "S",
            "body": body,
        }]
    }
    staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs", generation=generation)

    content = (root / "wiki/concepts/alpha/code-test.md").read_text(encoding="utf-8")
    assert "[[user-event-script]]" in content
    assert "[[User-Event-Script]]" in content  # inside fenced block
    assert "`[[Suitelet]]`" in content  # inside inline code


def test_ingest_codegraph_generates_pipeline_pages_when_full_graph(tmp_path: Path):
    """When graph_snapshot has cross-file calls, pipeline pages are generated."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    graph_data = {
        "files": [
            {"path": "src/mr_hc_vendpay_import.js", "language": "javascript", "nodeCount": 3},
            {"path": "src/mr_hc_vendpay_export.js", "language": "javascript", "nodeCount": 2},
            {"path": "src/lib_vendpay_util.js", "language": "javascript", "nodeCount": 2},
        ],
        "nodes": [
            {"id": "f:a1", "kind": "function", "name": "getInputData", "qualifiedName": "getInputData", "filePath": "src/mr_hc_vendpay_import.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:a2", "kind": "function", "name": "map", "qualifiedName": "map", "filePath": "src/mr_hc_vendpay_import.js", "startLine": 11, "endLine": 20, "language": "javascript"},
            {"id": "f:b1", "kind": "function", "name": "reduce", "qualifiedName": "reduce", "filePath": "src/mr_hc_vendpay_export.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:c1", "kind": "function", "name": "processPayment", "qualifiedName": "processPayment", "filePath": "src/lib_vendpay_util.js", "startLine": 1, "endLine": 10, "language": "javascript"},
        ],
        "edges": [
            {"source": "f:a1", "target": "f:c1", "kind": "calls", "line": 5},
            {"source": "f:a2", "target": "f:c1", "kind": "calls", "line": 15},
            {"source": "f:b1", "target": "f:c1", "kind": "calls", "line": 5},
        ],
    }
    client = FakeCodeGraphClient(responses={
        "files": {"ok": True, "data": {"files": graph_data["files"]}},
        "graph_snapshot": {"ok": True, "data": graph_data},
    })
    result = ingest_codegraph(root, project="vendpay", source_name="main", client=client, profile="suitescript")
    assert result["ok"] is True

    pipelines_dir = root / "wiki" / "projects" / "vendpay" / "code" / "pipelines"
    assert pipelines_dir.exists()
    pipeline_files = list(pipelines_dir.glob("*.md"))
    assert len(pipeline_files) >= 1

    call_graph = root / "wiki" / "projects" / "vendpay" / "code" / "call-graph.md"
    assert call_graph.is_file()

    overview = root / "wiki" / "projects" / "vendpay" / "code" / "overview.md"
    overview_content = overview.read_text(encoding="utf-8")
    assert "Business Pipelines" in overview_content
    assert "vendpay" in overview_content.lower()


def test_ingest_codegraph_generates_code_facts_without_src_prefix(tmp_path: Path):
    """Files without src/ prefix should still get code fact pages generated."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    graph_data = {
        "files": [
            {"path": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "language": "javascript", "nodeCount": 3},
            {"path": "SuiteScripts_GL/mr_hc_vendpay_import.js", "language": "javascript", "nodeCount": 2},
        ],
        "nodes": [
            {"id": "f:1", "kind": "function", "name": "getInputData", "qualifiedName": "getInputData", "filePath": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:2", "kind": "function", "name": "map", "qualifiedName": "map", "filePath": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "startLine": 11, "endLine": 20, "language": "javascript"},
            {"id": "f:3", "kind": "function", "name": "reduce", "qualifiedName": "reduce", "filePath": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "startLine": 21, "endLine": 30, "language": "javascript"},
            {"id": "f:4", "kind": "function", "name": "getInputData", "qualifiedName": "getInputData", "filePath": "SuiteScripts_GL/mr_hc_vendpay_import.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:5", "kind": "function", "name": "map", "qualifiedName": "map", "filePath": "SuiteScripts_GL/mr_hc_vendpay_import.js", "startLine": 11, "endLine": 20, "language": "javascript"},
        ],
        "edges": [
            {"source": "f:1", "target": "f:4", "kind": "calls", "line": 5},
        ],
    }
    client = FakeCodeGraphClient(responses={
        "files": {"ok": True, "data": {"files": graph_data["files"]}},
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    result = ingest_codegraph(root, project="huideng", source_name="codegraph", client=client)

    assert result["ok"] is True
    verify_page = root / "wiki/projects/huideng/code/SuiteScripts_GL/mr_hc_vendpay_verify.md"
    assert verify_page.is_file(), f"Expected code fact page at {verify_page}"
    import_page = root / "wiki/projects/huideng/code/SuiteScripts_GL/mr_hc_vendpay_import.md"
    assert import_page.is_file(), f"Expected code fact page at {import_page}"
    content = verify_page.read_text(encoding="utf-8")
    assert "## Symbols" in content
    assert "`getInputData`" in content


def test_pipeline_page_wikilinks_resolve_to_code_fact_pages(tmp_path: Path):
    """Pipeline page wikilinks should resolve to actual code fact page filenames."""
    import re as _re
    root = tmp_path / "vault"
    create_wiki_root(root)
    graph_data = {
        "files": [
            {"path": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "language": "javascript", "nodeCount": 3},
            {"path": "SuiteScripts_GL/mr_hc_vendpay_import.js", "language": "javascript", "nodeCount": 2},
            {"path": "SuiteScripts_GL/lib_vendpay_util.js", "language": "javascript", "nodeCount": 2},
        ],
        "nodes": [
            {"id": "f:1", "kind": "function", "name": "getInputData", "filePath": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:2", "kind": "function", "name": "map", "filePath": "SuiteScripts_GL/mr_hc_vendpay_verify.js", "startLine": 11, "endLine": 20, "language": "javascript"},
            {"id": "f:3", "kind": "function", "name": "reduce", "filePath": "SuiteScripts_GL/mr_hc_vendpay_import.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:4", "kind": "function", "name": "processPayment", "filePath": "SuiteScripts_GL/lib_vendpay_util.js", "startLine": 1, "endLine": 10, "language": "javascript"},
        ],
        "edges": [
            {"source": "f:1", "target": "f:4", "kind": "calls", "line": 5},
            {"source": "f:2", "target": "f:4", "kind": "calls", "line": 15},
            {"source": "f:3", "target": "f:4", "kind": "calls", "line": 5},
        ],
    }
    client = FakeCodeGraphClient(responses={
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    result = ingest_codegraph(root, project="huideng", source_name="codegraph", client=client)
    assert result["ok"] is True

    pipelines_dir = root / "wiki/projects/huideng/code/pipelines"
    if not pipelines_dir.exists():
        return  # no pipelines detected — acceptable for small graph

    code_dir = root / "wiki/projects/huideng/code"
    all_code_stems = {p.stem for p in code_dir.rglob("*.md")}

    for pipeline_file in pipelines_dir.glob("*.md"):
        content = pipeline_file.read_text(encoding="utf-8")
        wikilinks = _re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]", content)
        for link in wikilinks:
            assert link in all_code_stems, \
                f"Dangling wikilink [[{link}]] in {pipeline_file.name} — no matching code fact page"


def test_ingest_codegraph_pipeline_includes_client_script_module_path(tmp_path: Path):
    """Suitelet form.clientScriptModulePath should link the mounted Client Script."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    project = tmp_path / "project"
    scripts = project / "src" / "SuiteScripts"
    scripts.mkdir(parents=True)
    (scripts / "sl_order_page.js").write_text(
        "var form = serverWidget.createForm({title: 'Order'});\n"
        "form.clientScriptModulePath = './cs_order_page.js';\n",
        encoding="utf-8",
    )
    (scripts / "cs_order_page.js").write_text(
        "function pageInit(context) { return true; }\n",
        encoding="utf-8",
    )
    graph_data = {
        "files": [
            {"path": "src/SuiteScripts/sl_order_page.js", "language": "javascript", "nodeCount": 1},
            {"path": "src/SuiteScripts/cs_order_page.js", "language": "javascript", "nodeCount": 1},
        ],
        "nodes": [
            {"id": "f:sl", "kind": "function", "name": "onRequest", "filePath": "src/SuiteScripts/sl_order_page.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:cs", "kind": "function", "name": "pageInit", "filePath": "src/SuiteScripts/cs_order_page.js", "startLine": 1, "endLine": 10, "language": "javascript"},
        ],
        "edges": [],
    }
    client = FakeCodeGraphClient(responses={
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    result = ingest_codegraph(
        root,
        project="huideng",
        source_name="codegraph",
        client=client,
        codegraph_project_path=project,
        profile="suitescript",
    )

    assert result["ok"] is True
    call_graph = (root / "wiki/projects/huideng/code/call-graph.md").read_text(encoding="utf-8")
    assert "`sl_order_page` → `src/SuiteScripts/cs_order_page.js` (client_script_module_path)" in call_graph


def test_ingest_codegraph_generic_profile_skips_suitescript_pipeline_pages(tmp_path: Path):
    """Generic codegraph ingest should not run SuiteScript-specific pipeline analysis."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    graph_data = {
        "files": [
            {"path": "src/sl_order_page.js", "language": "javascript", "nodeCount": 1},
            {"path": "src/cs_order_page.js", "language": "javascript", "nodeCount": 1},
        ],
        "nodes": [
            {"id": "f:sl", "kind": "function", "name": "onRequest", "filePath": "src/sl_order_page.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:cs", "kind": "function", "name": "pageInit", "filePath": "src/cs_order_page.js", "startLine": 1, "endLine": 10, "language": "javascript"},
        ],
        "edges": [
            {"source": "f:sl", "target": "f:cs", "kind": "calls", "line": 5},
        ],
    }
    client = FakeCodeGraphClient(responses={
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    result = ingest_codegraph(root, project="generic", source_name="codegraph", client=client, profile="generic")

    assert result["ok"] is True
    assert (root / "wiki/projects/generic/code/src/sl_order_page.md").is_file()
    assert not (root / "wiki/projects/generic/code/pipelines").exists()
    assert not (root / "wiki/projects/generic/code/call-graph.md").exists()


def test_ingest_codegraph_profile_is_part_of_cache_key(tmp_path: Path):
    """Switching from generic to suitescript should rewrite pages even if graph data is unchanged."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    graph_data = {
        "files": [
            {"path": "src/sl_order_page.js", "language": "javascript", "nodeCount": 1},
            {"path": "src/cs_order_page.js", "language": "javascript", "nodeCount": 1},
        ],
        "nodes": [
            {"id": "f:sl", "kind": "function", "name": "onRequest", "filePath": "src/sl_order_page.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:cs", "kind": "function", "name": "pageInit", "filePath": "src/cs_order_page.js", "startLine": 1, "endLine": 10, "language": "javascript"},
        ],
        "edges": [
            {"source": "f:sl", "target": "f:cs", "kind": "calls", "line": 5},
        ],
    }
    client = FakeCodeGraphClient(responses={
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    first = ingest_codegraph(root, project="alpha", source_name="codegraph", client=client, profile="generic")
    second = ingest_codegraph(root, project="alpha", source_name="codegraph", client=client, profile="suitescript")

    assert first["ok"] is True
    assert second["ok"] is True
    assert second.get("status") != "unchanged"
    assert (root / "wiki/projects/alpha/code/call-graph.md").is_file()


def test_ingest_codegraph_returns_clear_error_for_nonexistent_path(tmp_path: Path):
    """When codegraph_project_path doesn't exist, return a clear error instead of crash."""
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = ingest_codegraph(
        root,
        project="alpha",
        source_name="main",
        codegraph_project_path=str(tmp_path / "nonexistent_project"),
    )

    assert result["ok"] is False
    assert "not" in result.get("error", "").lower() or "exist" in result.get("error", "").lower()


def test_ingest_codegraph_filters_by_include_extensions(tmp_path: Path):
    """When include_extensions is specified, only matching files get code fact pages."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    graph_data = {
        "files": [
            {"path": "src/FileCabinet/SuiteScripts/sl_main.js", "language": "javascript", "nodeCount": 2},
            {"path": "Objects/custrecord_payment.xml", "language": "xml", "nodeCount": 5},
            {"path": "Objects/custscript_verify.xml", "language": "xml", "nodeCount": 3},
        ],
        "nodes": [
            {"id": "f:1", "kind": "function", "name": "onRequest", "filePath": "src/FileCabinet/SuiteScripts/sl_main.js", "startLine": 1, "endLine": 10, "language": "javascript"},
            {"id": "f:2", "kind": "function", "name": "init", "filePath": "src/FileCabinet/SuiteScripts/sl_main.js", "startLine": 11, "endLine": 20, "language": "javascript"},
            {"id": "x:1", "kind": "element", "name": "custrecord_payment", "filePath": "Objects/custrecord_payment.xml", "startLine": 1, "endLine": 50, "language": "xml"},
            {"id": "x:2", "kind": "element", "name": "scriptid", "filePath": "Objects/custrecord_payment.xml", "startLine": 2, "endLine": 2, "language": "xml"},
            {"id": "x:3", "kind": "element", "name": "name", "filePath": "Objects/custrecord_payment.xml", "startLine": 3, "endLine": 3, "language": "xml"},
            {"id": "x:4", "kind": "element", "name": "recordtype", "filePath": "Objects/custrecord_payment.xml", "startLine": 4, "endLine": 4, "language": "xml"},
            {"id": "x:5", "kind": "element", "name": "description", "filePath": "Objects/custrecord_payment.xml", "startLine": 5, "endLine": 5, "language": "xml"},
            {"id": "x:6", "kind": "element", "name": "custscript_verify", "filePath": "Objects/custscript_verify.xml", "startLine": 1, "endLine": 30, "language": "xml"},
            {"id": "x:7", "kind": "element", "name": "scriptfile", "filePath": "Objects/custscript_verify.xml", "startLine": 2, "endLine": 2, "language": "xml"},
            {"id": "x:8", "kind": "element", "name": "notifyadmins", "filePath": "Objects/custscript_verify.xml", "startLine": 3, "endLine": 3, "language": "xml"},
        ],
        "edges": [],
    }
    client = FakeCodeGraphClient(responses={
        "files": {"ok": True, "data": {"files": graph_data["files"]}},
        "graph_snapshot": {"ok": True, "data": graph_data},
    })

    result = ingest_codegraph(
        root, project="alpha", source_name="main",
        client=client, include_extensions=[".js", ".ts"],
    )

    assert result["ok"] is True
    # JS file should have a code fact page
    js_page = root / "wiki/projects/alpha/code/src/FileCabinet/SuiteScripts/sl_main.md"
    assert js_page.is_file()
    # XML files should NOT have code fact pages
    xml_pages = list((root / "wiki/projects/alpha/code").rglob("*payment*"))
    assert len(xml_pages) == 0
    xml_pages2 = list((root / "wiki/projects/alpha/code").rglob("*custscript*"))
    assert len(xml_pages2) == 0

    files_snapshot = json.loads((root / "raw/sources/codegraph/alpha/files.json").read_text(encoding="utf-8"))
    snapshot_files = files_snapshot.get("files", files_snapshot)
    assert [item["path"] for item in snapshot_files] == ["src/FileCabinet/SuiteScripts/sl_main.js"]

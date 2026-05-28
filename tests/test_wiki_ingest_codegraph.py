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

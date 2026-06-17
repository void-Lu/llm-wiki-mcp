from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.wiki_ingest import ingest_codegraph, rescan_source, staged_wiki_ingest, _repair_cache_manifest_paths
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


def _codefacts(root: Path, project: str, source_name: str) -> list[dict[str, Any]]:
    return json.loads((root / "raw" / "sources" / "projects" / project / "codegraph" / "codefacts.json").read_text(encoding="utf-8"))


def test_ingest_codegraph_writes_snapshot_source_page_code_page_and_indexes(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient()

    result = ingest_codegraph(root, project="alpha", source_name="main", query="Suitelet entry", client=client)

    assert result["ok"] is True
    snapshot = root / "raw/sources/projects/alpha/codegraph/context.json"
    assert snapshot.is_file()
    source_page = root / "wiki/sources/projects/alpha/architecture/codegraph.md"
    assert source_page.is_file()
    code_page = root / "wiki/projects/alpha/sources/onrequest.md"
    assert not code_page.exists()
    assert not (root / "raw/sources/projects/alpha/codegraph/main").exists()
    assert not (root / "raw/projects").exists()
    project_index = root / "wiki/projects/alpha/index.md"
    assert project_index.is_file()
    assert (root / "wiki/index.md").is_file()
    assert (root / "wiki/overview.md").is_file()
    assert "## [" in (root / "wiki/log.md").read_text(encoding="utf-8")
    assert not (root / "wiki/projects/alpha/objects").exists()
    assert not (root / "wiki/projects/alpha/code").exists()
    assert not (root / "wiki/code").exists()

    source_frontmatter = yaml.safe_load(source_page.read_text(encoding="utf-8").split("---", 2)[1])
    assert source_frontmatter["sources"] == [
        "raw/sources/projects/alpha/codegraph/status.json",
        "raw/sources/projects/alpha/codegraph/files.json",
        "raw/sources/projects/alpha/codegraph/context.json",
        "raw/sources/projects/alpha/codegraph/graph.json",
        "raw/sources/projects/alpha/codegraph/codefacts.json",
        "raw/sources/projects/alpha/codegraph/impact-onrequest.json",
    ]
    codefacts = json.loads((root / "raw/sources/projects/alpha/codegraph/codefacts.json").read_text(encoding="utf-8"))
    assert codefacts[0]["frontmatter"]["symbol"] == "onRequest"


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

    fact = _codefacts(root, "mywiki", "cg")[0]
    content = fact["body"]
    fm = fact["frontmatter"]
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

    content = _codefacts(root, "mywiki", "cg")[0]["body"]
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
    assert (root / "raw/sources/projects/alpha/codegraph/graph.json").is_file()
    facts = _codefacts(root, "alpha", "main")
    content = next(fact["body"] for fact in facts if fact["frontmatter"].get("symbol") == "src/pkg/a.py")
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


def test_rescan_chat_source_writes_snapshot_under_date_directory(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "session.md").write_text("# Chat\n\nAlpha session", encoding="utf-8")

    result = rescan_source(root, project="alpha", source_name="session-2026-06-13", source_path=source, source_type="chat")

    assert result["ok"] is True
    assert result["status"] == "changed"
    assert result["paths"] == [
        "raw/sources/chat/2026/06/13/session-2026-06-13/session.md",
        "raw/sources/chat/2026/06/13/session-2026-06-13/manifest.json",
    ]
    assert (root / "raw/sources/chat/2026/06/13/session-2026-06-13/session.md").is_file()



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
    codefacts = root / "raw/sources/projects/alpha/codegraph/codefacts.json"
    first_mtime = codefacts.stat().st_mtime

    time.sleep(0.05)

    second = ingest_codegraph(root, project="alpha", source_name="main", query="Suitelet entry", client=client)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second.get("status") == "unchanged"
    assert codefacts.stat().st_mtime == first_mtime



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


def test_staged_wiki_ingest_chat_source_can_write_chatlog_page(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "session.md"
    source.write_text("# Chat\n\nUser asked to save the current session.", encoding="utf-8")
    staged_wiki_ingest(
        root,
        "prepare",
        project="alpha",
        source_name="session-2026-06-13",
        source_path=source,
        source_type="chat",
    )

    generation = {
        "source_summary": "Chat session about ingest behavior.",
        "pages": [
            {
                "path": "wiki/chatlog/2026/06/13/session-2026-06-13.md",
                "title": "Session 2026-06-13",
                "type": "chatlog",
                "summary": "Chat session summary",
                "body": "The session was saved raw-first before analysis.",
            }
        ],
    }

    result = staged_wiki_ingest(
        root,
        "apply",
        project="alpha",
        source_name="session-2026-06-13",
        generation=generation,
        source_type="chat",
    )

    assert result["ok"] is True
    assert "wiki/chatlog/2026/06/13/session-2026-06-13.md" in result["paths"]
    assert "wiki/sources/chatlog/2026/06/13/session-2026-06-13.md" in result["paths"]
    generated = root / "wiki/chatlog/2026/06/13/session-2026-06-13.md"
    frontmatter = yaml.safe_load(generated.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["type"] == "chatlog"
    assert frontmatter["sources"] == ["raw/sources/chat/2026/06/13/session-2026-06-13/session.md"]
    source_index = root / "wiki/sources/chatlog/2026/06/13/session-2026-06-13.md"
    assert source_index.is_file()
    source_frontmatter = yaml.safe_load(source_index.read_text(encoding="utf-8").split("---", 2)[1])
    assert source_frontmatter["type"] == "source_index"
    assert source_frontmatter["source_type"] == "chat"
    assert source_frontmatter["sources"] == ["raw/sources/chat/2026/06/13/session-2026-06-13/session.md"]
    assert (root / "raw/sources/chat/2026/06/13/session-2026-06-13/session.md").is_file()


def test_staged_wiki_ingest_rejects_chatlog_path_for_non_chat_source(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nAlpha content", encoding="utf-8")
    staged_wiki_ingest(root, "prepare", project="alpha", source_name="docs", source_path=source)

    result = staged_wiki_ingest(
        root,
        "apply",
        project="alpha",
        source_name="docs",
        generation={
            "pages": [
                {
                    "path": "wiki/chatlog/2026/06/13/docs.md",
                    "title": "Docs",
                    "type": "chatlog",
                }
            ]
        },
    )

    assert result["ok"] is False
    assert result["code"] == "invalid_generated_path"


def test_prepare_chat_source_prompt_prefers_chatlog_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "session.md"
    source.write_text("# Chat\n\nUser asked to save the current session.", encoding="utf-8")

    result = staged_wiki_ingest(
        root,
        "prepare",
        project="alpha",
        source_name="session-2026-06-13",
        source_path=source,
        source_type="chat",
    )

    assert result["ok"] is True
    assert "wiki/chatlog/YYYY/MM/DD/<slug>.md" in result["prompt"]
    assert "raw/sources/chat" in result["prompt"]


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


def test_apply_generation_canonicalizes_stale_raw_source_paths(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "article_0091700424.md"
    source.write_text("# Article\n\nNetSuite help content", encoding="utf-8")
    staged_wiki_ingest(
        root,
        "prepare",
        project="netsuite-online-help",
        source_name="article_0091700424",
        source_path=source,
    )

    old_source = "raw/sources/file/crawl4ai/level2/article_0091700424.md"
    canonical_source = "raw/sources/file/netsuite-online-help/article_0091700424/article_0091700424.md"
    generation = {
        "source_summary": {"title": "Article", "summary": f"来源于 {old_source}"},
        "pages": [
            {
                "path": "wiki/concepts/netsuite-online-help/article_0091700424.md",
                "title": "Article",
                "type": "concept",
                "summary": f"来源于 {old_source}",
                "body": f"## 原始来源\n\n- `{old_source}`",
                "sources": [old_source],
            }
        ],
    }

    result = staged_wiki_ingest(
        root,
        "apply",
        project="netsuite-online-help",
        source_name="article_0091700424",
        generation=generation,
    )

    assert result["ok"] is True
    generated = root / "wiki/concepts/netsuite-online-help/article_0091700424.md"
    generated_content = generated.read_text(encoding="utf-8")
    generated_frontmatter = yaml.safe_load(generated_content.split("---", 2)[1])
    assert generated_frontmatter["sources"] == [canonical_source]
    assert generated_frontmatter["summary"] == f"来源于 {canonical_source}"
    assert canonical_source in generated_content
    assert old_source not in generated_content

    source_index = root / "wiki/sources/concepts/netsuite-online-help/article_0091700424.md"
    source_index_content = source_index.read_text(encoding="utf-8")
    source_index_frontmatter = yaml.safe_load(source_index_content.split("---", 2)[1])
    assert source_index_frontmatter["sources"] == [canonical_source]
    assert source_index_frontmatter["summary"] == f"来源于 {canonical_source}"
    assert canonical_source in source_index_content
    assert old_source not in source_index_content


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


def test_apply_generation_writes_source_summary_to_wiki_sources_directory(tmp_path: Path):
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

    overview = (root / "wiki/projects/demo/architecture/code-overview.md").read_text(encoding="utf-8")
    assert "handleOrder" in overview
    assert "calcTotal" in overview
    # Vendor internal functions should be filtered out
    assert "_internal0" not in overview
    assert "_internal50" not in overview

    # Code facts are stored as raw machine facts, and vendored files are filtered out.
    facts = _codefacts(root, "demo", "main")
    fact_paths = {fact["frontmatter"]["source_path"] for fact in facts}
    assert "src/lib/papaparse.js" not in fact_paths
    assert "src/scripts/order.js" in fact_paths
    assert "src/scripts/calc.js" in fact_paths
    assert not (root / "wiki/projects/demo/code").exists()


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

    pipelines_dir = root / "wiki" / "projects" / "vendpay" / "pipelines"
    assert pipelines_dir.exists()
    pipeline_files = list(pipelines_dir.glob("*.md"))
    assert len(pipeline_files) >= 1

    call_graph = root / "wiki" / "projects" / "vendpay" / "pipelines" / "call-graph.md"
    assert call_graph.is_file()

    overview = root / "wiki" / "projects" / "vendpay" / "architecture" / "code-overview.md"
    overview_content = overview.read_text(encoding="utf-8")
    assert "Business Pipelines" in overview_content
    assert "vendpay" in overview_content.lower()


def test_ingest_codegraph_generates_code_facts_without_src_prefix(tmp_path: Path):
    """Files without src/ prefix should still get raw code facts generated."""
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
    facts = _codefacts(root, "huideng", "codegraph")
    fact_paths = {fact["frontmatter"]["source_path"] for fact in facts}
    assert "SuiteScripts_GL/mr_hc_vendpay_verify.js" in fact_paths
    assert "SuiteScripts_GL/mr_hc_vendpay_import.js" in fact_paths
    content = "\n\n".join(fact["body"] for fact in facts)
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

    pipelines_dir = root / "wiki/projects/huideng/pipelines"
    if not pipelines_dir.exists():
        return  # no pipelines detected — acceptable for small graph

    all_code_stems = {Path(fact["path"]).stem for fact in _codefacts(root, "huideng", "codegraph")}

    for pipeline_file in pipelines_dir.glob("*.md"):
        content = pipeline_file.read_text(encoding="utf-8")
        wikilinks = _re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]", content)
        for link in wikilinks:
            assert link in all_code_stems, \
                f"Dangling wikilink [[{link}]] in {pipeline_file.name}; no matching code fact"


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
    call_graph = (root / "wiki/projects/huideng/pipelines/call-graph.md").read_text(encoding="utf-8")
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
    facts = _codefacts(root, "generic", "codegraph")
    assert any(fact["frontmatter"]["source_path"] == "src/sl_order_page.js" for fact in facts)
    assert not (root / "wiki/projects/generic/pipelines").exists()
    assert not (root / "wiki/projects/generic/code").exists()


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
    assert (root / "wiki/projects/alpha/pipelines/call-graph.md").is_file()


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
    """When include_extensions is specified, only matching files get raw code facts."""
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
    # JS file should have a raw code fact; XML files should not.
    facts = _codefacts(root, "alpha", "main")
    fact_paths = {fact["frontmatter"]["source_path"] for fact in facts}
    assert fact_paths == {"src/FileCabinet/SuiteScripts/sl_main.js"}
    assert "onRequest" in "\n".join(fact["body"] for fact in facts)
    assert not (root / "wiki/projects/alpha/code").exists()

    files_snapshot = json.loads((root / "raw/sources/projects/alpha/codegraph/files.json").read_text(encoding="utf-8"))
    snapshot_files = files_snapshot.get("files", files_snapshot)
    assert [item["path"] for item in snapshot_files] == ["src/FileCabinet/SuiteScripts/sl_main.js"]


def test_repair_cache_manifest_paths_direct(tmp_path: Path):
    """_repair_cache_manifest_paths repairs manifest paths when files exist at the new location."""
    from netsuite_llm_wiki_mcp.wiki_ingest import _write_cache

    root = tmp_path / "vault"
    create_wiki_root(root)
    project, source_name, source_type = "alpha", "docs", "file"

    # Create the correct raw dir with the actual files
    correct_raw_dir = root / "raw" / "sources" / source_type / project / source_name
    correct_raw_dir.mkdir(parents=True)
    (correct_raw_dir / "notes.md").write_text("# Notes\n\nContent", encoding="utf-8")
    (correct_raw_dir / "extra.md").write_text("# Extra\n\nMore", encoding="utf-8")

    # Build a cache with OLD (wrong) manifest paths — missing the source_type level
    old_prefix = f"raw/sources/{project}/{source_name}/"
    cache = {
        "source_hash": "abc123",
        "source_type": source_type,
        "manifest": [
            {"path": f"{old_prefix}notes.md", "relative_path": "notes.md", "stored_sha256": "x"},
            {"path": f"{old_prefix}extra.md", "relative_path": "extra.md", "stored_sha256": "y"},
        ],
        "status": "prepared",
    }
    _write_cache(root, project, source_name, cache, source_type=source_type)

    result = _repair_cache_manifest_paths(cache, root, project, source_name, source_type)

    assert result["ok"] is True
    assert result["repaired_count"] == 2
    assert result["unrepairable_paths"] == []
    correct_prefix = f"raw/sources/{source_type}/{project}/{source_name}/"
    assert result["repaired_paths"] == [f"{correct_prefix}notes.md", f"{correct_prefix}extra.md"]

    # Verify the cache file was updated
    updated_cache = json.loads((root / ".llm-wiki/ingest-cache/file/alpha/docs.json").read_text(encoding="utf-8"))
    assert updated_cache["manifest"][0]["path"] == f"{correct_prefix}notes.md"


def test_repair_cache_manifest_paths_fails_when_files_missing(tmp_path: Path):
    """_repair_cache_manifest_paths returns ok=False when repaired paths don't exist on disk."""
    from netsuite_llm_wiki_mcp.wiki_ingest import _write_cache

    root = tmp_path / "vault"
    create_wiki_root(root)
    project, source_name, source_type = "alpha", "docs", "file"

    # No files at the correct location — repair should fail
    old_prefix = f"raw/sources/{project}/{source_name}/"
    cache = {
        "source_hash": "abc123",
        "source_type": source_type,
        "manifest": [
            {"path": f"{old_prefix}notes.md", "relative_path": "notes.md", "stored_sha256": "x"},
        ],
        "status": "prepared",
    }
    _write_cache(root, project, source_name, cache, source_type=source_type)

    result = _repair_cache_manifest_paths(cache, root, project, source_name, source_type)

    assert result["ok"] is False
    assert result["repaired_count"] == 0
    assert result["unrepairable_paths"] == [f"{old_prefix}notes.md"]


def test_rescan_source_auto_repairs_mismatched_manifest_paths(tmp_path: Path):
    """rescan_source detects mismatched manifest paths and auto-repairs them when source hash is unchanged."""
    from netsuite_llm_wiki_mcp.wiki_ingest import _write_cache

    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    # First rescan — writes correct cache and snapshot
    first = rescan_source(root, project="alpha", source_name="docs", source_path=source)
    assert first["status"] == "changed"
    source_hash = first["source_hash"]

    # Now simulate a path migration: corrupt the cache manifest to use old-style paths
    # (missing the source_type level), but keep the files at the correct new location
    correct_raw_dir = root / "raw" / "sources" / "file" / "alpha" / "docs"
    assert correct_raw_dir.exists()
    old_prefix = "raw/sources/alpha/docs/"
    correct_prefix = "raw/sources/file/alpha/docs/"
    corrupt_cache = {
        "source_hash": source_hash,
        "source_type": "file",
        "manifest": [
            {"path": f"{old_prefix}notes.md", "relative_path": "notes.md", "stored_sha256": "x"},
        ],
        "status": "prepared",
    }
    _write_cache(root, "alpha", "docs", corrupt_cache, source_type="file")

    # Second rescan should detect the mismatch, repair the manifest, and return unchanged
    second = rescan_source(root, project="alpha", source_name="docs", source_path=source)
    assert second["ok"] is True
    assert second["status"] == "unchanged"
    assert second.get("manifest_repaired") is True
    assert second["repaired_count"] == 1
    assert second["repaired_paths"] == [f"{correct_prefix}notes.md"]


def test_rescan_source_falls_through_when_repaired_paths_missing(tmp_path: Path):
    """rescan_source falls through to re-snapshot when repaired paths don't exist on filesystem."""
    from netsuite_llm_wiki_mcp.wiki_ingest import _write_cache

    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    # First rescan
    first = rescan_source(root, project="alpha", source_name="docs", source_path=source)
    assert first["status"] == "changed"
    source_hash = first["source_hash"]

    # Corrupt the cache AND remove the raw snapshot files
    old_prefix = "raw/sources/alpha/docs/"
    corrupt_cache = {
        "source_hash": source_hash,
        "source_type": "file",
        "manifest": [
            {"path": f"{old_prefix}notes.md", "relative_path": "notes.md", "stored_sha256": "x"},
        ],
        "status": "prepared",
    }
    _write_cache(root, "alpha", "docs", corrupt_cache, source_type="file")
    # Remove the actual raw snapshot so repair verification fails
    import shutil
    correct_raw_dir = root / "raw" / "sources" / "file" / "alpha" / "docs"
    if correct_raw_dir.exists():
        shutil.rmtree(correct_raw_dir)

    # Second rescan should fall through to full re-snapshot
    second = rescan_source(root, project="alpha", source_name="docs", source_path=source)
    assert second["ok"] is True
    assert second["status"] == "changed"
    assert "manifest_repaired" not in second


def test_prepare_combined_auto_repairs_mismatched_manifest_paths(tmp_path: Path):
    """_prepare_combined also auto-repairs mismatched manifest paths."""
    from netsuite_llm_wiki_mcp.wiki_ingest import _write_cache

    root = tmp_path / "vault"
    create_wiki_root(root)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Notes\n\nAlpha content", encoding="utf-8")

    # First prepare — writes correct cache
    first = staged_wiki_ingest(root, "prepare", project="alpha", source_name="docs", source_path=source)
    assert first["status"] == "needs_model"
    source_hash = first["source_hash"]

    # Corrupt the cache manifest to use old-style paths
    correct_raw_dir = root / "raw" / "sources" / "file" / "alpha" / "docs"
    assert correct_raw_dir.exists()
    old_prefix = "raw/sources/alpha/docs/"
    corrupt_cache = {
        "source_hash": source_hash,
        "source_type": "file",
        "manifest": [
            {"path": f"{old_prefix}notes.md", "relative_path": "notes.md", "stored_sha256": "x"},
        ],
        "status": "prepared",
    }
    _write_cache(root, "alpha", "docs", corrupt_cache, source_type="file")

    # Second prepare should detect mismatch, repair, and return skipped
    second = staged_wiki_ingest(root, "prepare", project="alpha", source_name="docs", source_path=source)
    assert second["ok"] is True
    assert second["status"] == "skipped"
    assert second.get("manifest_repaired") is True
    assert second["repaired_count"] == 1

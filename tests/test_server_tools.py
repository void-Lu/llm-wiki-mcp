from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.server import (
    _build_filters,
    mcp,
    wiki_list_files_tool,
    wiki_read_file_tool,
    wiki_status_tool,
    wiki_init_tool,
    wiki_ingest_codegraph_tool,
    wiki_ingest_llm_tool,
    wiki_lint_tool,
    wiki_query_debug_tool,
    wiki_query_tool,
    wiki_rescan_tool,
    wiki_synthesis_tool,
    wiki_write_note,
    wiki_write_note_tool,
)


class TestLlmWikiServerTools:
    def test_registered_tools_exclude_deprecated_tools(self):
        deprecated = {
            "index_vault",
            "index_sources",
            "search_netsuite_knowledge",
            "ask_netsuite_rag",
            "get_index_status",
            "generate_suitecloud_wiki",
            "write_wiki_summaries",
            "save_obsidian_note",
        }

        registered = {tool.name for tool in mcp._tool_manager.list_tools()}

        assert registered.isdisjoint(deprecated)

    def test_registered_tools_include_wiki_write_note(self):
        registered = {tool.name for tool in mcp._tool_manager.list_tools()}

        assert "wiki_write_note" in registered
        assert "wiki_synthesis" in registered
        assert "wiki_status" in registered
        assert "wiki_list_files" in registered
        assert "wiki_read_file" in registered

    def test_wiki_write_note_tool_delegates_to_note_writer(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "path": "wiki/projects/alpha/specs/note.md"}
        calls: list[dict[str, object]] = []

        def fake_save_note(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_write_note", fake_save_note)

        result = wiki_write_note_tool(
            note_type="spec",
            title="Spec note",
            content="Body",
            project="alpha",
            domain="common-errors",
            related_script_types=["user-event"],
            script_type="restlet",
            object_type="salesorder",
            related_objects=["invoice"],
            related_scripts=["customscript_sync"],
            tags=["custom"],
            zentao_urls=["https://zentao.example/ticket/1"],
            decision_status="accepted",
            status="open",
            filename="note",
            overwrite=True,
            auto_index=False,
            vault_root=str(vault),
        )

        assert result == payload
        assert calls == [{
            "note_type": "spec",
            "title": "Spec note",
            "content": "Body",
            "project": "alpha",
            "domain": "common-errors",
            "related_script_types": ["user-event"],
            "script_type": "restlet",
            "object_type": "salesorder",
            "related_objects": ["invoice"],
            "related_scripts": ["customscript_sync"],
            "tags": ["custom"],
            "zentao_urls": ["https://zentao.example/ticket/1"],
            "decision_status": "accepted",
            "status": "open",
            "filename": "note",
            "overwrite": True,
            "auto_index": False,
            "vault_root": str(vault),
        }]

    def test_wiki_write_note_accepts_camel_case_params(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "path": "wiki/projects/alpha/specs/note.md"}
        calls: list[dict[str, object]] = []

        def fake_save_note(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_write_note", fake_save_note)

        result = wiki_write_note(
            noteType="spec",
            title="Spec note",
            content="Body",
            project="alpha",
            relatedScriptTypes=["user-event"],
            scriptType="restlet",
            objectType="salesorder",
            relatedObjects=["invoice"],
            relatedScripts=["customscript_sync"],
            zentaoUrls=["https://zentao.example/ticket/1"],
            decisionStatus="accepted",
            autoIndex=False,
            vaultRoot=str(vault),
        )

        assert result == payload
        assert calls[0]["note_type"] == "spec"
        assert calls[0]["related_script_types"] == ["user-event"]
        assert calls[0]["script_type"] == "restlet"
        assert calls[0]["object_type"] == "salesorder"
        assert calls[0]["related_objects"] == ["invoice"]
        assert calls[0]["related_scripts"] == ["customscript_sync"]
        assert calls[0]["zentao_urls"] == ["https://zentao.example/ticket/1"]
        assert calls[0]["decision_status"] == "accepted"
        assert calls[0]["auto_index"] is False
        assert calls[0]["vault_root"] == str(vault)

    def test_wiki_write_note_missing_note_type_returns_error(self):
        result = wiki_write_note(title="Test", content="Body")
        assert result["ok"] is False
        assert result["code"] == "missing_note_type"

    def test_wiki_init_tool_creates_confirmed_structure(self, tmp_path: Path):
        vault = tmp_path / "wiki-root"

        result = wiki_init_tool(str(vault))

        assert result["ok"] is True
        assert (vault / "purpose.md").is_file()
        assert (vault / "raw/sources").is_dir()
        assert (vault / "raw/sources/projects").is_dir()
        assert not (vault / "raw/projects").exists()
        assert (vault / "raw/assets").is_dir()
        assert (vault / "wiki/projects").is_dir()
        assert (vault / "wiki/concepts").is_dir()
        assert (vault / "wiki/entities").is_dir()
        assert (vault / "wiki/archives/log.md").is_file()
        assert (vault / ".llm-wiki").is_dir()

    def test_wiki_status_tool_delegates_status(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "initialized": True}
        calls: list[object] = []

        def fake_status(vault_root: object) -> dict[str, object]:
            calls.append(vault_root)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_status", fake_status)

        result = wiki_status_tool(str(vault))

        assert result == payload
        assert calls == [str(vault)]

    def test_wiki_list_files_tool_delegates_file_listing(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "files": []}
        calls: list[dict[str, object]] = []

        def fake_list_files(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_list_files", fake_list_files)

        result = wiki_list_files_tool(str(vault), root_name="all", recursive=False, max_files=10)

        assert result == payload
        assert calls == [{"vault_root": str(vault), "root_name": "all", "recursive": False, "max_files": 10}]

    def test_wiki_read_file_tool_delegates_file_read(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "content": "body"}
        calls: list[dict[str, object]] = []

        def fake_read_file(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_read_file", fake_read_file)

        result = wiki_read_file_tool(str(vault), path="wiki/index.md", max_bytes=42)

        assert result == payload
        assert calls == [{"vault_root": str(vault), "path": "wiki/index.md", "max_bytes": 42}]

    def test_wiki_query_tool_delegates_to_wiki_query(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "results": []}
        calls: list[dict[str, object]] = []

        def fake_query(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_query", fake_query)

        result = wiki_query_tool(str(vault), question="invoice", project="alpha", top_k=3, include_content=False, context_window_tokens=8000)

        assert result == payload
        assert calls == [{
            "vault_root": str(vault),
            "question": "invoice",
            "project": "alpha",
            "top_k": 3,
            "include_content": False,
            "context_window_tokens": 8000,
            "include_context_pack": True,
            "chat_history": None,
            "language": "zh-CN",
            "enable_vector": False,
            "vector_config": None,
            "max_graph_hops": 2,
            "include_raw_sources": False,
            "filter_type": None,
            "filter_tags": None,
        }]

    def test_wiki_ingest_llm_tool_delegates_staged_ingest(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "stage": "prepare_analysis"}
        calls: list[dict[str, object]] = []

        def fake_staged(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_staged_wiki_ingest", fake_staged)

        result = wiki_ingest_llm_tool(str(vault), stage="prepare_analysis", project="alpha", source_name="docs", source_path="src")

        assert result == payload
        assert calls == [{
            "vault_root": str(vault),
            "stage": "prepare_analysis",
            "project": "alpha",
            "source_name": "docs",
            "source_path": "src",
            "source_type": "file",
            "language": "zh-CN",
            "analysis": None,
            "generation": None,
        }]

    def test_wiki_rescan_tool_delegates_rescan(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "status": "changed"}
        calls: list[dict[str, object]] = []

        def fake_rescan(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_rescan_source", fake_rescan)

        result = wiki_rescan_tool(str(vault), project="alpha", source_name="docs", source_path="src", source_type="file", language="zh-CN")

        assert result == payload
        assert calls == [{
            "vault_root": str(vault),
            "project": "alpha",
            "source_name": "docs",
            "source_path": "src",
            "source_type": "file",
            "language": "zh-CN",
        }]

    def test_wiki_query_debug_tool_delegates_debug_query(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "graph_reasons": {}}
        calls: list[dict[str, object]] = []

        def fake_debug(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_query_debug", fake_debug)

        result = wiki_query_debug_tool(str(vault), question="invoice", project="alpha", top_k=3, max_graph_hops=1, include_raw_sources=True)

        assert result == payload
        assert calls == [{
            "vault_root": str(vault),
            "question": "invoice",
            "project": "alpha",
            "top_k": 3,
            "max_graph_hops": 1,
            "include_raw_sources": True,
        }]

    def test_wiki_ingest_codegraph_tool_delegates_codegraph_ingest(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "written": 2}
        calls: list[dict[str, object]] = []

        def fake_ingest(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_ingest_codegraph", fake_ingest)

        result = wiki_ingest_codegraph_tool(
            str(vault),
            project="alpha",
            source_name="main",
            query="entry",
            codegraph_project_path="repo",
            include_extensions=[".js"],
            profile="suitescript",
        )

        assert result == payload
        assert calls == [{
            "vault_root": str(vault),
            "project": "alpha",
            "source_name": "main",
            "query": "entry",
            "codegraph_project_path": "repo",
            "include_extensions": [".js"],
            "profile": "suitescript",
        }]

    def test_wiki_lint_tool_delegates_semantic_stage(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "stage": "prepare_semantic_review"}
        calls: list[dict[str, object]] = []

        def fake_lint(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_lint", fake_lint)

        result = wiki_lint_tool(str(vault), stage="prepare_semantic_review", project="alpha", semantic_review="review", language="zh-CN")

        assert result == payload
        assert calls == [{"vault_root": str(vault), "stage": "prepare_semantic_review", "project": "alpha", "semantic_review": "review", "language": "zh-CN"}]

    def test_wiki_synthesis_tool_delegates_to_synthesis_module(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "stage": "prepare"}
        calls: list[dict[str, object]] = []

        def fake_synthesis(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_wiki_synthesis", fake_synthesis)

        result = wiki_synthesis_tool(
            str(vault),
            question="invoice sync",
            stage="prepare",
            context_pages=[{"path": "wiki/a.md"}],
            synthesis="body",
            title="Invoice Sync",
            project="alpha",
            language="zh-CN",
        )

        assert result == payload
        assert calls == [{
            "vault_root": str(vault),
            "question": "invoice sync",
            "stage": "prepare",
            "context_pages": [{"path": "wiki/a.md"}],
            "synthesis": "body",
            "title": "Invoice Sync",
            "project": "alpha",
            "language": "zh-CN",
        }]


class TestBuildFilters:
    def test_source_kind_included(self):
        filters = _build_filters(None, None, None, None, None, None, source_kind="note")
        assert filters["source_kind"] == "note"

    def test_source_name_included(self):
        filters = _build_filters(None, None, None, None, None, None, source_name="obsidian")
        assert filters["source_name"] == "obsidian"

    def test_content_type_filter_is_mapped_to_type(self):
        filters = _build_filters(None, None, None, None, None, None, content_type="generated_wiki")
        assert filters["type"] == "generated_wiki"

    def test_source_params_omitted_when_none(self):
        filters = _build_filters("project-a", None, None, None, None, None)
        assert filters["project"] == "project-a"
        assert "source_kind" not in filters
        assert "source_name" not in filters

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.server import (
    _build_filters,
    generate_suitecloud_wiki_tool,
    get_index_status_tool,
    index_sources_tool,
    index_vault_tool,
    wiki_init_tool,
    wiki_ingest_tool,
    wiki_ingest_llm_tool,
    wiki_lint_tool,
    wiki_query_debug_tool,
    wiki_query_tool,
    wiki_rescan_tool,
)


class TestDeprecatedRagTools:
    def test_index_vault_is_deprecated(self):
        result = index_vault_tool()
        assert result["ok"] is False
        assert result["code"] == "deprecated_rag_tool"
        assert "wiki_ingest" in result["replacement"]

    def test_index_sources_is_deprecated(self):
        result = index_sources_tool()
        assert result["ok"] is False
        assert result["code"] == "deprecated_rag_tool"
        assert "wiki_ingest" in result["replacement"]

    def test_get_index_status_is_deprecated(self):
        result = get_index_status_tool()
        assert result["ok"] is False
        assert result["code"] == "deprecated_rag_tool"
        assert "wiki_lint" in result["replacement"]

    def test_search_netsuite_knowledge_is_deprecated(self):
        from netsuite_llm_wiki_mcp.server import search_netsuite_knowledge_tool
        result = search_netsuite_knowledge_tool(question="test")
        assert result["ok"] is False
        assert result["code"] == "deprecated_rag_tool"
        assert "wiki_query" in result["replacement"]

    def test_ask_netsuite_rag_is_deprecated(self):
        from netsuite_llm_wiki_mcp.server import ask_netsuite_rag_tool
        result = ask_netsuite_rag_tool(question="test")
        assert result["ok"] is False
        assert result["code"] == "deprecated_rag_tool"
        assert "wiki_query" in result["replacement"]


class TestLlmWikiServerTools:
    def test_wiki_init_tool_creates_confirmed_structure(self, tmp_path: Path):
        vault = tmp_path / "wiki-root"

        result = wiki_init_tool(str(vault))

        assert result["ok"] is True
        assert (vault / "purpose.md").is_file()
        assert (vault / "raw/sources").is_dir()
        assert (vault / "raw/assets").is_dir()
        assert (vault / "wiki/projects").is_dir()
        assert (vault / "wiki/concepts").is_dir()
        assert (vault / ".llm-wiki").is_dir()

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

    def test_wiki_ingest_rejects_unsupported_source_type(self, tmp_path: Path):
        vault = tmp_path / "wiki-root"

        result = wiki_ingest_tool(str(vault), source_type="pdf", project="alpha", source_name="main")

        assert result["ok"] is False
        assert result["code"] == "unsupported_source_type"

    def test_wiki_ingest_requires_project(self, tmp_path: Path):
        vault = tmp_path / "wiki-root"

        result = wiki_ingest_tool(str(vault), source_type="codegraph")

        assert result["ok"] is False
        assert result["code"] == "missing_project"

    def test_wiki_ingest_requires_source_name(self, tmp_path: Path):
        vault = tmp_path / "wiki-root"

        result = wiki_ingest_tool(str(vault), source_type="codegraph", project="alpha")

        assert result["ok"] is False
        assert result["code"] == "missing_source_name"

    def test_wiki_ingest_tool_delegates_codegraph_ingest(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "written": 2}
        calls: list[dict[str, object]] = []

        def fake_ingest(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_ingest_codegraph", fake_ingest)

        result = wiki_ingest_tool(str(vault), source_type="codegraph", project="alpha", source_name="main", query="entry", codegraph_project_path="repo")

        assert result == payload
        assert calls == [{"vault_root": str(vault), "project": "alpha", "source_name": "main", "query": "entry", "codegraph_project_path": "repo"}]

    def test_generate_suitecloud_wiki_requires_vault_root(self):
        result = generate_suitecloud_wiki_tool(project="alpha", source_name="main")

        assert result["ok"] is False
        assert result["code"] == "missing_vault_root"

    def test_generate_suitecloud_wiki_delegates_to_ingest(self, monkeypatch, tmp_path: Path):
        vault = tmp_path / "wiki-root"
        payload = {"ok": True, "written": 2}
        calls: list[dict[str, object]] = []

        def fake_ingest(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return payload

        monkeypatch.setattr("netsuite_llm_wiki_mcp.server.run_ingest_codegraph", fake_ingest)

        result = generate_suitecloud_wiki_tool(project="alpha", source_name="main", vault_root=str(vault))

        assert result == payload
        assert calls == [{"vault_root": str(vault), "project": "alpha", "source_name": "main", "query": "project code overview", "codegraph_project_path": None}]


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

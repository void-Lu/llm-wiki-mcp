from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from netsuite_rag_mcp.wiki_ingest import ingest_codegraph, staged_wiki_ingest
from netsuite_rag_mcp.wiki_paths import create_wiki_root


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


def test_ingest_codegraph_writes_snapshot_source_page_code_page_and_indexes(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient()

    result = ingest_codegraph(root, project="alpha", source_name="main", query="Suitelet entry", client=client)

    assert result["ok"] is True
    snapshot = root / "raw/sources/codegraph/alpha/main/context.json"
    assert snapshot.is_file()
    source_page = root / "wiki/sources/codegraph-alpha-main.md"
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
    assert frontmatter["sources"] == ["raw/sources/codegraph/alpha/main/context.json"]
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
    assert "prompt" in result
    assert (root / "raw/sources/file/alpha/docs/notes.md").is_file()

    second = staged_wiki_ingest(root, "prepare_analysis", project="alpha", source_name="docs", source_path=source)

    assert second["status"] == "skipped"
    assert second["code"] == "source_unchanged"


def test_staged_wiki_ingest_requires_analysis_and_generation(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    missing_analysis = staged_wiki_ingest(root, "prepare_generation", project="alpha", source_name="docs")
    missing_generation = staged_wiki_ingest(root, "apply_generation", project="alpha", source_name="docs")

    assert missing_analysis["code"] == "missing_analysis"
    assert missing_generation["code"] == "missing_generation"




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
    assert "wiki/sources/alpha-docs.md" in result["paths"]
    assert "wiki/concepts/alpha/generated.md" in result["paths"]
    generated = root / "wiki/concepts/alpha/generated.md"
    frontmatter = yaml.safe_load(generated.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["sources"] == ["raw/sources/file/alpha/docs/source.md"]
    assert (root / "wiki/overview.md").is_file()
    assert "llm_ingest" in (root / "wiki/log.md").read_text(encoding="utf-8")

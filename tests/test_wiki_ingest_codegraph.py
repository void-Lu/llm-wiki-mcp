from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from netsuite_rag_mcp.wiki_ingest import ingest_codegraph
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


def test_ingest_codegraph_returns_not_initialized_error(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    client = FakeCodeGraphClient({"status": {"ok": False, "code": "codegraph_not_initialized", "error": "not initialized"}})

    result = ingest_codegraph(root, project="alpha", source_name="main", client=client)

    assert result["ok"] is False
    assert result["code"] == "codegraph_not_initialized"

"""Tests for wiki_verify module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page, WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_verify import wiki_verify


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    create_wiki_root(root)
    return root


def _write_index_page(root: Path, rel_path: str, sources: list[str], wikilinks: list[str]):
    body = "Source index\n\n## Generated pages\n\n" + "\n".join(f"- [[{name}]]" for name in wikilinks)
    write_wiki_page(root, WikiPage(
        relative_path=Path(rel_path),
        frontmatter={"type": "source_index", "generated": True, "project": "alpha", "sources": sources, "summary": "index"},
        title="Index",
        body=body,
    ))


def _write_generated_page(root: Path, rel_path: str, title: str, body: str):
    write_wiki_page(root, WikiPage(
        relative_path=Path(rel_path),
        frontmatter={"type": "concept", "generated": True, "project": "alpha", "summary": "test"},
        title=title,
        body=body,
    ))


def _write_raw_source(root: Path, rel_path: str, content: str):
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_prepare_from_index_page(vault: Path):
    _write_raw_source(vault, "raw/sources/file/alpha/docs/notes.md", "# API\n\nThe API uses REST.")
    _write_generated_page(vault, "wiki/concepts/alpha/api-overview.md", "API Overview", "The API uses REST.")
    _write_index_page(vault, "wiki/sources/concepts/alpha/docs.md", ["raw/sources/file/alpha/docs/notes.md"], ["api-overview"])

    result = wiki_verify(vault, "prepare", project="alpha")

    assert result["ok"] is True
    assert result["stage"] == "prepare"
    assert result["status"] == "needs_model"
    assert result["pages_to_verify"] == 1
    assert "REST" in result["prompt"]


def test_prepare_no_index_pages_returns_error(vault: Path):
    result = wiki_verify(vault, "prepare", project="nonexistent")
    assert result["ok"] is False
    assert result["code"] == "no_pages"


def test_prepare_single_index_page_path(vault: Path):
    _write_raw_source(vault, "raw/sources/file/alpha/docs/notes.md", "Source content here")
    _write_generated_page(vault, "wiki/concepts/alpha/my-concept.md", "My Concept", "Body text from source")
    _write_index_page(vault, "wiki/sources/concepts/alpha/docs.md", ["raw/sources/file/alpha/docs/notes.md"], ["my-concept"])

    result = wiki_verify(vault, "prepare", page_path="wiki/sources/concepts/alpha/docs.md")

    assert result["ok"] is True
    assert result["pages_to_verify"] == 1


def test_prepare_project_includes_date_grouped_chatlog_source_indexes(vault: Path):
    _write_raw_source(vault, "raw/sources/chat/2026/06/16/session-2026-06-16/session.md", "Chat source content here")
    _write_generated_page(vault, "wiki/chatlog/2026/06/16/session.md", "Session", "Body text from chat source")
    _write_index_page(
        vault,
        "wiki/sources/chatlog/2026/06/16/session-2026-06-16.md",
        ["raw/sources/chat/2026/06/16/session-2026-06-16/session.md"],
        ["session"],
    )

    result = wiki_verify(vault, "prepare", project="alpha")

    assert result["ok"] is True
    assert result["pages_to_verify"] == 1


def test_apply_records_faithful_results(vault: Path):
    verification_result = {
        "results": [
            {"page_path": "wiki/concepts/alpha/api-overview.md", "faithful": True, "score": 0.95, "issues": []},
        ]
    }
    result = wiki_verify(vault, "apply", verification_result=verification_result)

    assert result["ok"] is True
    assert result["stage"] == "apply"
    assert result["verified"] == 1
    assert result["faithful"] == 1
    assert result["unfaithful"] == 0


def test_apply_records_unfaithful_results(vault: Path):
    verification_result = {
        "results": [
            {
                "page_path": "wiki/concepts/alpha/api-overview.md",
                "faithful": False,
                "score": 0.4,
                "issues": [{"claim": "API uses GraphQL", "source_evidence": "API uses REST", "severity": "hallucination"}],
            },
        ]
    }
    result = wiki_verify(vault, "apply", verification_result=verification_result)

    assert result["ok"] is True
    assert result["unfaithful"] == 1
    assert result["results"][0]["issue_count"] == 1


def test_apply_rejects_missing_result(vault: Path):
    result = wiki_verify(vault, "apply")
    assert result["ok"] is False
    assert result["code"] == "missing_verification_result"


def test_apply_accepts_json_string(vault: Path):
    verification_result = json.dumps({"results": [{"page_path": "x.md", "faithful": True, "score": 1.0, "issues": []}]})
    result = wiki_verify(vault, "apply", verification_result=verification_result)
    assert result["ok"] is True
    assert result["verified"] == 1

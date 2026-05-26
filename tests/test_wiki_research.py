from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_research import wiki_research


@pytest.fixture
def research_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "index.md").write_text(
        "---\ntype: index\ngenerated: true\n---\n\n# Index\n\n- [[concepts/suiteql|SuiteQL]]\n",
        encoding="utf-8",
    )
    (wiki / "log.md").write_text("", encoding="utf-8")
    return root


def test_prepare_returns_prompt(research_root: Path):
    results = [
        {"title": "SuiteQL Guide", "url": "https://example.com/1", "snippet": "SuiteQL is a query language."},
        {"title": "NetSuite API", "url": "https://example.com/2", "snippet": "REST API docs."},
    ]
    result = wiki_research(str(research_root), "SuiteQL best practices", stage="prepare", search_results=results)
    assert result["ok"] is True
    assert result["stage"] == "prepare"
    assert "SuiteQL" in result["prompt"]
    assert "[1]" in result["prompt"]
    assert "[2]" in result["prompt"]


def test_prepare_empty_results(research_root: Path):
    result = wiki_research(str(research_root), "topic", stage="prepare", search_results=[])
    assert result["ok"] is False
    assert result["code"] == "no_results"


def test_apply_writes_page(research_root: Path):
    synthesis = "## Overview\n\nSuiteQL is powerful. See [[concepts/suiteql|SuiteQL]].\n\n## References\n\n1. [Guide](https://example.com)"
    result = wiki_research(str(research_root), "SuiteQL best practices", stage="apply", synthesis=synthesis)
    assert result["ok"] is True
    assert result["stage"] == "apply"
    assert result["path"].startswith("wiki/queries/")

    page_path = research_root / result["path"]
    assert page_path.exists()
    content = page_path.read_text(encoding="utf-8")
    assert "SuiteQL" in content
    assert "generated: true" in content
    assert "deep-research" in content


def test_apply_strips_thinking(research_root: Path):
    synthesis = "<thinking>Let me think...</thinking>\n\nActual content here."
    result = wiki_research(str(research_root), "test topic", stage="apply", synthesis=synthesis)
    assert result["ok"] is True
    page_path = research_root / result["path"]
    content = page_path.read_text(encoding="utf-8")
    assert "<thinking>" not in content
    assert "Actual content here" in content


def test_apply_empty_synthesis(research_root: Path):
    result = wiki_research(str(research_root), "topic", stage="apply", synthesis="")
    assert result["ok"] is False
    assert result["code"] == "empty_synthesis"

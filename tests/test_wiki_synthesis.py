from __future__ import annotations

from pathlib import Path

import yaml

from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_synthesis import wiki_synthesis


def test_wiki_synthesis_prepare_builds_prompt_from_query_context(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "purpose.md").write_text("# Purpose\n\nPurpose marker for synthesis.", encoding="utf-8")
    (root / "wiki/overview.md").write_text("---\ntype: overview\ngenerated: true\n---\n\n# Overview\n\nOverview marker for synthesis.", encoding="utf-8")

    result = wiki_synthesis(
        root,
        question="How should we handle invoice sync?",
        stage="prepare",
        context_pages=[{"path": "wiki/projects/alpha/code/sync.md", "title": "Sync", "content": "Invoice sync context."}],
        project="alpha",
    )

    assert result["ok"] is True
    assert result["stage"] == "prepare"
    assert "Purpose marker for synthesis" in result["prompt"]
    assert "Overview marker for synthesis" in result["prompt"]
    assert "Invoice sync context" in result["prompt"]
    assert result["next_call"]["tool"] == "wiki_synthesis"


def test_wiki_synthesis_apply_writes_synthesis_page_and_log(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = wiki_synthesis(
        root,
        question="How should we handle invoice sync?",
        stage="apply",
        synthesis="<thinking>draft</thinking>\n\n## Recommendation\n\nUse staged retries.",
        context_pages=[{"path": "wiki/projects/alpha/code/sync.md", "title": "Sync"}],
        project="alpha",
        title="Invoice Sync Synthesis",
    )

    assert result["ok"] is True
    assert result["stage"] == "apply"
    assert result["path"].startswith("wiki/synthesis/")
    page = root / result["path"]
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["type"] == "synthesis"
    assert frontmatter["origin"] == "query-synthesis"
    assert frontmatter["project"] == "alpha"
    assert frontmatter["sources"] == ["wiki/projects/alpha/code/sync.md"]
    assert "<thinking>" not in text
    assert "Use staged retries" in text
    assert "synthesis" in (root / "wiki/log.md").read_text(encoding="utf-8")
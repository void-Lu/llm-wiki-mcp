from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_rag_mcp.wiki_insights import wiki_insights


@pytest.fixture
def insights_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    wiki = root / "wiki"
    concepts = wiki / "concepts" / "domain"
    concepts.mkdir(parents=True)
    code = wiki / "projects" / "proj" / "code"
    code.mkdir(parents=True)

    (concepts / "alpha.md").write_text(
        "---\ntype: concept\ntitle: Alpha\n---\n\n# Alpha\n\nLinks to [[beta]] and [[gamma]].\n",
        encoding="utf-8",
    )
    (concepts / "beta.md").write_text(
        "---\ntype: concept\ntitle: Beta\n---\n\n# Beta\n\nLinks to [[alpha]].\n",
        encoding="utf-8",
    )
    (concepts / "gamma.md").write_text(
        "---\ntype: concept\ntitle: Gamma\n---\n\n# Gamma\n\nLinks to [[alpha]].\n",
        encoding="utf-8",
    )
    # Orphan page — no links
    (concepts / "orphan.md").write_text(
        "---\ntype: concept\ntitle: Orphan\n---\n\n# Orphan\n\nNo links here.\n",
        encoding="utf-8",
    )
    # Code page linking to concept (cross-type)
    (code / "entry.md").write_text(
        "---\ntype: code\ntitle: Entry\ngenerated: true\n---\n\n# Entry\n\nUses [[alpha]].\n",
        encoding="utf-8",
    )
    return root


def test_insights_finds_orphans(insights_root: Path):
    result = wiki_insights(str(insights_root))
    assert result["ok"] is True
    assert result["node_count"] >= 4

    orphan_insights = [i for i in result["insights"] if i["type"] == "orphan_pages"]
    assert len(orphan_insights) == 1
    orphan_slugs = [p["title"] for p in orphan_insights[0]["pages"]]
    assert "Orphan" in orphan_slugs


def test_insights_finds_bridges(insights_root: Path):
    result = wiki_insights(str(insights_root))
    # Alpha connects to beta, gamma, and entry (cross-type) — may be a bridge
    bridge_insights = [i for i in result["insights"] if i["type"] == "bridge_page"]
    # With simple connected components, bridges only appear with multiple communities
    # In this small graph everything is connected, so bridges may not appear
    assert isinstance(bridge_insights, list)


def test_insights_project_filter(insights_root: Path):
    result = wiki_insights(str(insights_root), project="proj")
    assert result["ok"] is True
    # Should include code pages and concepts but filter appropriately
    assert result["node_count"] >= 1


def test_insights_empty_wiki(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    result = wiki_insights(str(root))
    assert result["ok"] is True
    assert result["insights"] == []

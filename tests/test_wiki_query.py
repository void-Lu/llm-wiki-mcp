from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.wiki_index import refresh_indexes
from netsuite_rag_mcp.wiki_io import write_wiki_page
from netsuite_rag_mcp.wiki_models import WikiPage
from netsuite_rag_mcp.wiki_paths import create_wiki_root
from netsuite_rag_mcp.wiki_query import wiki_query


def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    data = {"title": title, "generated": bool(frontmatter.pop("generated", True)), **frontmatter}
    write_wiki_page(root, WikiPage(Path(path), data, title, body), overwrite_generated_only=False)


def test_wiki_query_finds_keyword_matches_and_returns_citations(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/projects/alpha/code/suitelet.md",
        "Suitelet Entry",
        "This Suitelet handles invoice approval and links to [[decisions/invoice-approval.md|decision]].",
        type="code_fact",
        tags=["suitelet", "invoice"],
        summary="invoice suitelet",
    )
    _write(
        root,
        "wiki/projects/alpha/decisions/invoice-approval.md",
        "Invoice Approval Decision",
        "We chose synchronous invoice approval because finance needs immediate feedback.",
        type="decision",
        generated=False,
        summary="finance decision",
    )
    refresh_indexes(root)

    result = wiki_query(root, "invoice suitelet", project="alpha", top_k=3)

    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    assert paths[0] == "wiki/projects/alpha/code/suitelet.md"
    assert "wiki/projects/alpha/decisions/invoice-approval.md" in paths
    assert result["context"][0]["citation"] == "[1] wiki/projects/alpha/code/suitelet.md"


def test_wiki_query_project_scope_prioritizes_project_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/code/script.md", "Alpha Script", "shared keyword alpha behavior", type="code_fact")
    _write(root, "wiki/projects/beta/code/script.md", "Beta Script", "shared keyword beta behavior", type="code_fact")
    refresh_indexes(root)

    result = wiki_query(root, "shared keyword", project="beta", top_k=2)

    assert result["results"][0]["path"] == "wiki/projects/beta/code/script.md"


def test_wiki_query_uses_frontmatter_tags_and_index(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/suitescript-governance.md",
        "SuiteScript Governance",
        "Usage units matter.",
        type="concept",
        tags=["governance", "netsuite"],
        summary="governance limits",
    )
    refresh_indexes(root)

    result = wiki_query(root, "governance", top_k=1)

    assert result["ok"] is True
    assert result["results"][0]["path"] == "wiki/concepts/suitescript-governance.md"
    assert "Usage units matter" in result["context"][0]["content"]

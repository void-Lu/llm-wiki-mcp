from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.wiki_index import refresh_indexes
from netsuite_rag_mcp.wiki_io import write_wiki_page
from netsuite_rag_mcp.wiki_models import WikiPage
from netsuite_rag_mcp.wiki_paths import create_wiki_root
from netsuite_rag_mcp.wiki_query import wiki_query, wiki_query_debug


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


def test_wiki_query_project_scope_filters_other_projects(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/code/script.md", "Alpha Script", "shared keyword alpha behavior extra extra", type="code_fact")
    _write(root, "wiki/projects/beta/code/script.md", "Beta Script", "shared keyword beta behavior", type="code_fact")
    refresh_indexes(root)

    result = wiki_query(root, "shared keyword alpha", project="beta", top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/projects/beta/code/script.md" in paths
    assert "wiki/projects/alpha/code/script.md" not in paths


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


def test_wiki_query_includes_project_scoped_raw_sources_when_enabled(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    alpha = root / "raw/sources/file/alpha/docs"
    beta = root / "raw/sources/file/beta/docs"
    alpha.mkdir(parents=True, exist_ok=True)
    beta.mkdir(parents=True, exist_ok=True)
    (alpha / "alpha.md").write_text("alpha raw invoice", encoding="utf-8")
    (beta / "beta.md").write_text("beta raw invoice", encoding="utf-8")

    result = wiki_query(root, "invoice", project="alpha", include_raw_sources=True, top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert any(path.startswith("raw/sources/file/alpha/") for path in paths)
    assert all(not path.startswith("raw/sources/file/beta/") for path in paths)

    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice Approval", "finance workflow", type="concept")
    raw = root / "raw/sources/manual.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("发票审批 每个 业务 流程", encoding="utf-8")

    result = wiki_query(root, "invoice 发票审批", top_k=2, include_raw_sources=True)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/invoice.md" in paths
    assert "raw/sources/manual.txt" in paths
    assert result["results"][paths.index("wiki/concepts/invoice.md")]["scores"]["keyword"] >= 10

    default_result = wiki_query(root, "发票审批", top_k=2)
    assert "raw/sources/manual.txt" not in [item["path"] for item in default_result["results"]]


def test_wiki_query_graph_expands_by_sources_and_wikilinks(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle links to [[neighbor.md]].",
        type="concept",
        sources=["raw/sources/a.md"],
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept", sources=["raw/sources/a.md"])
    _write(root, "wiki/concepts/second-hop.md", "Second Hop", "distant content [[neighbor.md]]", type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "needle", top_k=3)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert "wiki/concepts/second-hop.md" in paths
    neighbor = result["results"][paths.index("wiki/concepts/neighbor.md")]
    assert neighbor["scores"]["graph"] > 0


def test_wiki_query_debug_explains_graph_reasons(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle links to [[neighbor.md]].",
        type="concept",
        sources=["raw/sources/a.md"],
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept", sources=["raw/sources/a.md"])
    refresh_indexes(root)

    result = wiki_query_debug(root, "needle", top_k=3)

    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    reasons = result["graph_reasons"]["wiki/concepts/neighbor.md"]
    assert {reason["kind"] for reason in reasons} >= {"direct_wikilink", "shared_source", "same_type"}
    assert all("score" in reason for reason in reasons)


def test_wiki_query_returns_budgeted_context_pack(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/long.md", "Long Page", "budget " * 2000, type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "budget", top_k=1, context_window_tokens=4000, chat_history=[{"role": "user", "content": "prior question"}])

    pack = result["context_pack"]
    assert pack["budget"]["allocated"]["wiki_pages"] == 2400
    assert pack["budget"]["allocated"]["chat_history"] == 800
    assert pack["pages"][0]["citation"] == "[1]"
    assert "prior question" in pack["chat_history"]


def test_wiki_query_vector_stage_is_optional_warning(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/a.md", "Alpha", "vector keyword", type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "vector", enable_vector=True, top_k=1)

    assert result["pipeline"]["stage_1_5_vector_enabled"] is True
    assert result["pipeline"]["stage_1_5_vector_warnings"][0]["code"] == "vector_config_missing"
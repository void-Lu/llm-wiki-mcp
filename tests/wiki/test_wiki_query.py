from __future__ import annotations

from pathlib import Path

from wiki.wiki_index import refresh_indexes
from wiki.wiki_io import write_wiki_page
from wiki.wiki_models import WikiPage
from wiki.wiki_paths import create_wiki_root
from retrieval.vector_index import VectorIndexStore
from retrieval.vector_provider import DeterministicFakeProvider
from wiki.wiki_query import wiki_query
import wiki.wiki_query as wiki_query_module


def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    data = {"title": title, "generated": bool(frontmatter.pop("generated", True)), **frontmatter}
    write_wiki_page(root, WikiPage(Path(path), data, title, body), overwrite_generated_only=False)


def test_wiki_query_finds_keyword_matches_and_returns_citations(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/projects/alpha/architecture/suitelet.md",
        "Suitelet Entry",
        "This Suitelet handles invoice approval and links to [[specs/invoice-approval.md|spec]].",
        type="architecture",
        tags=["suitelet", "invoice"],
        summary="invoice suitelet",
    )
    _write(
        root,
        "wiki/projects/alpha/specs/invoice-approval.md",
        "Invoice Approval Decision",
        "We chose synchronous invoice approval because finance needs immediate feedback.",
        type="spec",
        generated=False,
        summary="finance decision",
    )
    refresh_indexes(root)

    result = wiki_query(root, "invoice suitelet", project="alpha", top_k=3)

    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    assert paths[0] == "wiki/projects/alpha/architecture/suitelet.md"
    assert "wiki/projects/alpha/specs/invoice-approval.md" in paths
    assert result["context"][0]["citation"] == "[1] wiki/projects/alpha/architecture/suitelet.md"


def test_wiki_query_uses_existing_retrieval_store_without_reading_corpus(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "invoice approval workflow", type="concept")
    refresh_indexes(root)

    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("corpus read")))
    result = wiki_query(root, "invoice", include_content=False, include_context_pack=False)

    assert result["results"][0]["path"] == "wiki/concepts/invoice.md"


def test_wiki_query_project_scope_prioritizes_project_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/architecture/script.md", "Alpha Script", "shared keyword alpha behavior", type="architecture")
    _write(root, "wiki/projects/beta/architecture/script.md", "Beta Script", "shared keyword beta behavior", type="architecture")
    refresh_indexes(root)

    result = wiki_query(root, "shared keyword", project="beta", top_k=2)

    assert result["results"][0]["path"] == "wiki/projects/beta/architecture/script.md"


def test_wiki_query_project_scope_filters_other_projects(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/architecture/script.md", "Alpha Script", "shared keyword alpha behavior extra extra", type="architecture")
    _write(root, "wiki/projects/beta/architecture/script.md", "Beta Script", "shared keyword beta behavior", type="architecture")
    refresh_indexes(root)

    result = wiki_query(root, "shared keyword alpha", project="beta", top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/projects/beta/architecture/script.md" in paths
    assert "wiki/projects/alpha/architecture/script.md" not in paths


def test_wiki_query_excludes_archives_by_default(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/current/invoice.md", "Current Invoice", "current invoice workflow", type="concept")
    archived = root / "wiki/archives/2026/06/16/concepts/old/invoice.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\ntitle: Old Invoice\ngenerated: true\narchived: true\n---\n\n# Old Invoice\n\narchived invoice workflow",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = wiki_query(root, "invoice workflow", top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/current/invoice.md" in paths
    assert all(not path.startswith("wiki/archives/") for path in paths)


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


def test_wiki_query_reads_raw_projection_for_explicit_inclusion_and_scope(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/wiki-only.md", "Wiki Only", "curated material", type="concept")
    raw = root / "raw/sources/file/default/projection.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw projection sentinel", encoding="utf-8")
    refresh_indexes(root)

    default_result = wiki_query(root, "raw projection sentinel", top_k=5)
    included_result = wiki_query(root, "raw projection sentinel", include_raw_sources=True, top_k=5)
    raw_result = wiki_query(root, "raw projection sentinel", scope="raw", top_k=5)

    assert default_result["results"] == []
    assert [item["path"] for item in included_result["results"]] == ["raw/sources/file/default/projection.txt"]
    assert [item["path"] for item in raw_result["results"]] == ["raw/sources/file/default/projection.txt"]
    assert raw_result["results"][0]["source_kind"] == "raw"


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


def test_wiki_query_graph_expands_by_full_wiki_reference_target(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/seed.md", "Seed Page", "unique full target needle", type="concept")
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept")
    seed = root / "wiki/concepts/seed.md"
    seed.write_text(seed.read_text(encoding="utf-8").replace("unique full target needle", "unique full target needle [[wiki/concepts/neighbor]]"), encoding="utf-8")
    refresh_indexes(root)

    result = wiki_query(root, "unique full target needle", top_k=3)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert result["results"][paths.index("wiki/concepts/neighbor.md")]["scores"]["graph"] > 0


def test_wiki_query_graph_uses_markdown_code_context_semantics(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        (
            "unique needle links to [[neighbor.md]].\n\n"
            "`[[inline-example.md]]`\n\n"
            "~~~markdown\n[[fenced-example.md]]\n~~~"
        ),
        type="concept",
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor", "ordinary neighbor", type="concept")
    _write(root, "wiki/concepts/inline-example.md", "Inline Example", "not related", type="concept")
    _write(root, "wiki/concepts/fenced-example.md", "Fenced Example", "not related", type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "unique needle", top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert "wiki/concepts/inline-example.md" not in paths
    assert "wiki/concepts/fenced-example.md" not in paths


def test_wiki_query_archived_page_is_not_a_graph_bridge(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle links to [[bridge]].",
        type="concept",
    )
    _write(root, "wiki/concepts/distant.md", "Distant", "unrelated content", type="concept")
    archived = root / "wiki/archives/stale/2026/07/27/wiki/concepts/bridge.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\n"
        "title: Archived Bridge\n"
        "type: concept\n"
        "generated: true\n"
        "archived: true\n"
        "---\n\n"
        "# Archived Bridge\n\n"
        "[[distant]]\n",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = wiki_query(root, "unique needle", top_k=5, max_graph_hops=2)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/distant.md" not in paths
    assert all(not path.startswith("wiki/archives/") for path in paths)


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


def test_default_top_k_is_ten_and_explicit_eight_preserves_prefix_scores(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(12):
        _write(
            root,
            f"wiki/concepts/result-{index}.md",
            f"Result {index}",
            f"shared retrieval term {index}",
            type="concept",
        )
    refresh_indexes(root)

    default_result = wiki_query(root, "shared retrieval term", include_content=False, context_window_tokens=4000)
    explicit_eight = wiki_query(root, "shared retrieval term", top_k=8, include_content=False, context_window_tokens=4000)

    assert len(default_result["results"]) == 10
    assert [(item["path"], item["score"], item["scores"]) for item in explicit_eight["results"]] == [
        (item["path"], item["score"], item["scores"]) for item in default_result["results"][:8]
    ]
    budget = default_result["budget"]
    assert sum(budget["used"].values()) <= budget["total"]


def test_wiki_query_vector_stage_is_optional_warning(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/a.md", "Alpha", "vector keyword", type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "vector", enable_vector=True, top_k=1)

    assert result["pipeline"]["stage_1_5_vector_enabled"] is True
    assert result["pipeline"]["stage_1_5_vector_warnings"][0]["code"] == "vector_config_missing"


def test_wiki_query_vector_recall_is_independent(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/lexical.md", "Lexical Match", "expense report keyword", type="concept")
    _write(root, "wiki/concepts/semantic.md", "Semantic Match", "accounts payable operations", type="concept")
    refresh_indexes(root)
    model = tmp_path / "local-bge-m3"
    model.mkdir()
    provider = DeterministicFakeProvider(
        {
            "expense automation": [1, 0, 0, 0],
            "accounts payable operations": [1, 0, 0, 0],
        }
    )
    store = VectorIndexStore(root)
    records = wiki_query_module.vector_index_records(root)
    store.build(records, provider, include_raw_sources=False)
    monkeypatch.setattr(wiki_query_module, "LocalBgeM3Provider", lambda *args, **kwargs: provider)
    config = {"provider": "local_bge_m3", "model_path": str(model), "rrf_k": 60}

    result = wiki_query(root, "expense automation", top_k=3, include_content=False, include_context_pack=False, enable_vector=True, vector_config=config)

    semantic = next(item for item in result["results"] if item["path"] == "wiki/concepts/semantic.md")
    assert semantic["scores"]["keyword"] == 0
    assert semantic["scores"]["vector"] > 0
    assert result["pipeline"]["stage_1_5_vector_status"]["state"] == "ready"


def test_wiki_query_min_vector_score_filters_weak_matches(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/strong.md", "Strong Match", "strong semantic match content", type="concept")
    _write(root, "wiki/concepts/weak.md", "Weak Match", "weak semantic match content", type="concept")
    refresh_indexes(root)
    model = tmp_path / "local-bge-m3"
    model.mkdir()
    provider = DeterministicFakeProvider(
        {
            "test query": [1, 0, 0, 0],
            "strong semantic match": [1, 0, 0, 0],
            "weak semantic match": [3, 95, 0, 0],
        }
    )
    store = VectorIndexStore(root)
    records = wiki_query_module.vector_index_records(root)
    store.build(records, provider, include_raw_sources=False)
    monkeypatch.setattr(wiki_query_module, "LocalBgeM3Provider", lambda *args, **kwargs: provider)
    base_config = {"provider": "local_bge_m3", "model_path": str(model)}

    # Default min_vector_score=0.5 filters out the weak match (cosine ≈ 0.03).
    result_default = wiki_query(
        root, "test query", top_k=10, include_content=False, include_context_pack=False,
        enable_vector=True, vector_config=dict(base_config),
    )
    paths_default = {item["path"] for item in result_default["results"]}
    assert "wiki/concepts/strong.md" in paths_default
    assert "wiki/concepts/weak.md" not in paths_default

    # min_vector_score=0.0 lets the weak match through.
    result_open = wiki_query(
        root, "test query", top_k=10, include_content=False, include_context_pack=False,
        enable_vector=True, vector_config={**base_config, "min_vector_score": 0.0},
    )
    paths_open = {item["path"] for item in result_open["results"]}
    assert "wiki/concepts/strong.md" in paths_open
    assert "wiki/concepts/weak.md" in paths_open


def test_wiki_query_never_builds_missing_vector_index_or_exposes_raw_when_disabled(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/visible.md", "Visible", "ordinary content", type="concept")
    raw = root / "raw/sources/private.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw semantic content", encoding="utf-8")
    model = tmp_path / "local-bge-m3"
    model.mkdir()

    result = wiki_query(
        root,
        "raw semantic",
        include_content=False,
        include_context_pack=False,
        enable_vector=True,
        vector_config={"provider": "local_bge_m3", "model_path": str(model)},
    )

    assert not (root / ".llm-wiki/vector-index").exists()
    assert result["pipeline"]["stage_1_5_vector_warnings"][0]["code"] == "index_missing"
    assert all(not item["path"].startswith("raw/") for item in result["results"])


def test_wiki_query_idf_weights_rare_terms_higher(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/common1.md", "Common One", "common word appears here", type="concept")
    _write(root, "wiki/concepts/common2.md", "Common Two", "common word appears here too", type="concept")
    _write(root, "wiki/concepts/common3.md", "Common Three", "common word appears here also", type="concept")
    _write(root, "wiki/concepts/rare.md", "Rare Page", "common word and unique_rare_term here", type="concept")
    refresh_indexes(root)

    common_result = wiki_query(root, "common", top_k=4)
    rare_result = wiki_query(root, "unique_rare_term", top_k=4)

    rare_scores = {item["path"]: item["scores"]["keyword"] for item in rare_result["results"]}
    assert rare_scores.get("wiki/concepts/rare.md", 0) > 0
    common_scores = {item["path"]: item["scores"]["keyword"] for item in common_result["results"]}
    rare_keyword = rare_scores.get("wiki/concepts/rare.md", 0)
    common_keyword = common_scores.get("wiki/concepts/rare.md", 0)
    assert rare_keyword > common_keyword


def test_wiki_query_frontmatter_filter_by_type(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/projects/alpha/architecture/script.md", "Script", "shared keyword alpha", type="architecture")
    _write(root, "wiki/projects/alpha/specs/choice.md", "Choice", "shared keyword alpha", type="spec", generated=False)
    refresh_indexes(root)

    result = wiki_query(root, "shared keyword", project="alpha", top_k=5, filter_type="spec")

    paths = [item["path"] for item in result["results"]]
    assert "wiki/projects/alpha/specs/choice.md" in paths
    assert "wiki/projects/alpha/architecture/script.md" not in paths


def test_wiki_query_returns_title_match_and_images(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/diagram.md",
        "Architecture Diagram",
        "The system includes this diagram: ![Suitelet flow](../media/suitelet-flow.png) and another ![Suitelet flow](../media/suitelet-flow.png).",
        type="concept",
    )
    refresh_indexes(root)

    result = wiki_query(root, "architecture", top_k=1)

    item = result["results"][0]
    assert item["path"] == "wiki/concepts/diagram.md"
    assert item["title_match"] is True
    assert item["images"] == [{"url": "../media/suitelet-flow.png", "alt": "Suitelet flow"}]


def test_wiki_query_prioritizes_exact_title_phrase_over_body_repetition(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice-approval.md", "Invoice Approval", "short body", type="concept")
    _write(root, "wiki/concepts/noisy.md", "Noisy Page", "invoice approval " * 20, type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "invoice approval", top_k=2)

    assert result["results"][0]["path"] == "wiki/concepts/invoice-approval.md"
    assert result["results"][0]["scores"]["keyword"] > result["results"][1]["scores"]["keyword"]


def test_wiki_query_length_normalization_keeps_exact_title_above_generated_body(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/3d-secure.md", "3D Secure Payment Authentication", "short reference", type="concept")
    _write(
        root,
        "wiki/concepts/help-noise.md",
        "Commerce Reference",
        "3d secure payment authentication commerce web stores " * 4_000,
        type="concept",
    )
    refresh_indexes(root)

    result = wiki_query(root, "3D Secure Payment Authentication", top_k=2, include_content=False)

    assert [item["path"] for item in result["results"]] == [
        "wiki/concepts/3d-secure.md",
        "wiki/concepts/help-noise.md",
    ]
    assert result["results"][0]["scores"]["keyword"] > result["results"][1]["scores"]["keyword"]


def test_wiki_query_uses_path_tie_break_and_keeps_graph_inside_filters(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/zeta.md", "Zeta", "stable tie needle", type="concept")
    _write(root, "wiki/concepts/alpha.md", "Alpha", "stable tie needle", type="concept")
    _write(root, "wiki/concepts/seed.md", "Seed", "unique bridge needle [[projects/alpha/specs/bridge.md]]", type="concept")
    _write(root, "wiki/projects/alpha/specs/bridge.md", "Bridge", "[[reachable.md]]", type="spec")
    _write(root, "wiki/concepts/reachable.md", "Reachable", "no lexical evidence", type="concept")
    refresh_indexes(root)

    first = wiki_query(root, "stable tie needle", top_k=2, include_content=False)
    second = wiki_query(root, "stable tie needle", top_k=2, include_content=False)
    filtered = wiki_query(root, "unique bridge needle", top_k=5, filter_type="concept", include_content=False)

    assert [item["path"] for item in first["results"]] == [
        "wiki/concepts/alpha.md",
        "wiki/concepts/zeta.md",
    ]
    assert [item["path"] for item in second["results"]] == [item["path"] for item in first["results"]]
    assert "wiki/projects/alpha/specs/bridge.md" not in [item["path"] for item in filtered["results"]]
    assert "wiki/concepts/reachable.md" not in [item["path"] for item in filtered["results"]]


def test_wiki_query_excludes_structural_pages_from_results(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/needle.md", "Needle", "needle content", type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "needle", top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/needle.md" in paths
    assert "wiki/index.md" not in paths
    assert "wiki/overview.md" not in paths


def test_wiki_query_excludes_retired_source_namespace(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = root / "wiki/sources/provider/_entries.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source_index\ngenerated: true\n---\n\n# Source Leaf\n\nsource index needle", encoding="utf-8")

    result = wiki_query(root, "needle", top_k=5)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/sources/provider/_entries.md" not in paths

def test_wiki_query_graph_expands_escaped_table_wikilinks(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/seed.md",
        "Seed Page",
        "unique needle table link\n\n| Example | Description |\n|---|---|\n| [[neighbor.md\\|Neighbor Page]] | related |",
        type="concept",
    )
    _write(root, "wiki/concepts/neighbor.md", "Neighbor Page", "related content", type="concept")
    refresh_indexes(root)

    result = wiki_query(root, "needle", top_k=2)

    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    neighbor = result["results"][paths.index("wiki/concepts/neighbor.md")]
    assert neighbor["scores"]["graph"] > 0

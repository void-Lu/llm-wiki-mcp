from pathlib import Path

from netsuite_llm_wiki_mcp.query_pipeline import QueryFilters, run_query_v2
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore
from netsuite_llm_wiki_mcp.runtime_config import decode_global_config
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes


def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    write_wiki_page(root, WikiPage(Path(path), {"title": title, "generated": True, **frontmatter}, title, body), overwrite_generated_only=False)


def test_v2_returns_compact_passages_without_result_body(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "Invoice approval requires a role.", type="concept")
    refresh_indexes(root)
    result = run_query_v2(root, "invoice approval")
    assert result["ok"] is True
    assert "content" not in result["results"][0]
    assert result["context_pack"]["passages"][0]["content"]
    assert result["pipeline"]["corpus"] == "active"


def test_v2_archive_scope_is_physically_isolated(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/current.md", "Current", "current retention policy", type="concept")
    archive = root / "archives" / "bundles" / "2026" / "07" / "x" / "wiki" / "concepts"
    archive.mkdir(parents=True)
    (archive / "old.md").write_text("---\ntitle: Old\ntype: concept\nlifecycle: archived\n---\n\n# Old\n\nlegacy retention policy", encoding="utf-8")
    RetrievalIndexStore(root, scope="archive").build(RetrievalIndexStore(root, scope="archive").iter_vault_pages())
    result = run_query_v2(root, "legacy retention", scope="archive", filters=QueryFilters())
    assert result["pipeline"]["corpus"] == "archive"
    assert all("current.md" not in item["path"] for item in result["results"])


def test_v2_reuses_bounded_graph_expansion_without_filter_escape(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/seed.md", "Seed", "unique needle [[neighbor.md]]", type="concept")
    _write(root, "wiki/concepts/neighbor.md", "Neighbor", "related material", type="concept")
    _write(root, "wiki/entities/other.md", "Other", "related material", type="entity")
    refresh_indexes(root)
    result = run_query_v2(root, "unique needle", filters=QueryFilters(type="concept"), debug=True)
    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/neighbor.md" in paths
    assert "wiki/entities/other.md" not in paths
    neighbor = next(item for item in result["results"] if item["path"] == "wiki/concepts/neighbor.md")
    assert neighbor["scores"]["graph"] > 0


def test_v2_raw_fallback_reads_only_selected_source_chunks(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw" / "sources" / "manual.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# API evidence\n\nfield_id is custbody_approval_state token=super-secret\n\n# Unrelated\n\n" + "noise " * 500, encoding="utf-8")
    _write(root, "wiki/entities/approval.md", "Approval", "Approval field id reference", type="entity", sources=["raw/sources/manual.md"])
    refresh_indexes(root)
    result = run_query_v2(root, "field id reference")
    raw_passages = [item for item in result["context_pack"]["passages"] if item["evidence_kind"] == "raw_evidence"]
    assert raw_passages and "custbody_approval_state" in raw_passages[0]["content"]
    assert "super-secret" not in raw_passages[0]["content"]
    assert result["pipeline"]["fallback"]["level"] == "raw"


def test_query_version_flag_keeps_a_legacy_rollback_path(tmp_path: Path) -> None:
    config = decode_global_config({"vaults": {"local": {"root": str(tmp_path), "retrieval": {"query_version": "v1"}}}}, tmp_path / "config.yaml")
    assert config.vaults["local"].retrieval.query_version == "v1"


def test_v2_adds_vector_only_passages_without_scanning_markdown(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/semantic.md", "Semantic", "no lexical overlap", type="concept")
    refresh_indexes(root)

    monkeypatch.setattr(
        "netsuite_llm_wiki_mcp.query_pipeline._vector_hits",
        lambda *_args, **_kwargs: ({next(iter(RetrievalIndexStore(root).vector_records()))["passage_id"]: (1, 0.9)}, []),
    )
    result = run_query_v2(root, "unrelated query")
    assert [item["path"] for item in result["results"]] == ["wiki/concepts/semantic.md"]


def test_v2_vector_only_recall_does_not_bypass_filters(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/alpha.md", "Alpha", "no lexical overlap", type="concept", project="alpha", tags=["finance"])
    refresh_indexes(root)
    passage_id = str(next(iter(RetrievalIndexStore(root).vector_records()))["passage_id"])
    monkeypatch.setattr("netsuite_llm_wiki_mcp.query_pipeline._vector_hits", lambda *_args, **_kwargs: ({passage_id: (1, 0.9)}, []))

    result = run_query_v2(root, "unrelated query", project="beta", filters=QueryFilters(type="concept", tags=("finance",)))
    assert result["results"] == []

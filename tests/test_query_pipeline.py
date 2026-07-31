from pathlib import Path

from netsuite_llm_wiki_mcp.query_pipeline import QueryFilters, legacy_response_from_v2, run_query_v2
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


def test_legacy_adapter_reuses_v2_selected_context_passages(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "Invoice approval requires a role.", type="concept")
    refresh_indexes(root)

    compact = run_query_v2(root, "invoice approval")
    legacy = legacy_response_from_v2(compact)

    assert [item["path"] for item in legacy["context"]] == [item["path"] for item in compact["context_pack"]["passages"]]
    assert legacy["warnings"] == ["legacy_response_adapter_v2_selected_results"]


def test_v2_returns_at_most_one_best_passage_per_page(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "invoice approval " * 1_000, type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "invoice approval", top_k=5)

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/invoice.md"]
    assert len(result["context_pack"]["passages"]) == 1


def test_v2_uses_a_stable_path_tie_break_for_equal_scores(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/a-first.md", "First", "shared ranking token", type="concept")
    _write(root, "wiki/concepts/b-second.md", "Second", "shared ranking token", type="concept")
    refresh_indexes(root)

    first = run_query_v2(root, "shared ranking token", top_k=2)
    second = run_query_v2(root, "shared ranking token", top_k=2)

    assert [item["path"] for item in first["results"]] == ["wiki/concepts/a-first.md", "wiki/concepts/b-second.md"]
    assert [item["path"] for item in second["results"]] == [item["path"] for item in first["results"]]


def test_v2_history_scope_is_traceable_and_cannot_outrank_formal_knowledge(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/approval.md", "Approval policy", "approval decision is approved", type="concept")
    chat = root / "raw" / "sources" / "chat" / "2026" / "07" / "31" / "review-123" / "transcript.md"
    chat.parent.mkdir(parents=True, exist_ok=True)
    chat.write_text(
        "---\ntitle: Review transcript\nproject: billing\noccurred_at: 2026-07-31T08:30:00+00:00\n---\n\n# Review\n\napproval decision is rejected in this provisional chat note",
        encoding="utf-8",
    )
    refresh_indexes(root)

    all_scope = run_query_v2(root, "approval decision", scope="all", top_k=2)
    history_scope = run_query_v2(root, "approval decision", scope="history", top_k=2)

    assert all_scope["results"][0]["path"] == "wiki/concepts/approval.md"
    assert [item["path"] for item in history_scope["results"]] == ["raw/sources/chat/2026/07/31/review-123/transcript.md"]
    citation = history_scope["context_pack"]["citations"][0]
    assert citation["metadata"] == {
        "session_id": "2026/07/31/review-123",
        "occurred_at": "2026-07-31 08:30:00+00:00",
        "project": "billing",
        "content_hash": citation["metadata"]["content_hash"],
    }
    assert len(citation["metadata"]["content_hash"]) == 64


def test_v2_title_signal_recalls_a_terse_entity_query_without_full_body_match(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/sources/capsules/action-examples.md", "Action Examples", "workflow transition reference", type="source_capsule")
    refresh_indexes(root)

    result = run_query_v2(root, "NetSuite action examples")

    assert [item["path"] for item in result["results"]] == ["wiki/sources/capsules/action-examples.md"]


def test_v2_provenance_signal_recalls_source_named_entity_without_raw_read(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/sources/capsules/reports.md", "Reports Menu Links", "navigation reference", type="source_capsule", sources=["raw/sources/help/Access to Reports.md"])
    refresh_indexes(root)

    result = run_query_v2(root, "access reports")

    assert [item["path"] for item in result["results"]] == ["wiki/sources/capsules/reports.md"]
    assert all("raw_evidence" != item["evidence_kind"] for item in result["context_pack"]["passages"])


def test_v2_never_returns_source_index_pages_regardless_of_filename(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/sources/catalog/index-04.md", "Catalog index", "shared source retrieval term", type="index")
    _write(root, "wiki/sources/capsules/catalog.md", "Catalog capsule", "shared source retrieval term", type="source_capsule")
    refresh_indexes(root)

    result = run_query_v2(root, "shared source retrieval term")

    assert [item["path"] for item in result["results"]] == ["wiki/sources/capsules/catalog.md"]


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


def test_v2_graph_expansion_requires_every_requested_tag(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/seed.md", "Seed", "unique needle [[partial.md]]", type="concept", tags=["finance", "approved"])
    _write(root, "wiki/concepts/partial.md", "Partial", "related material", type="concept", tags=["finance"])
    refresh_indexes(root)

    result = run_query_v2(root, "unique needle", filters=QueryFilters(tags=("finance", "approved")), debug=True)

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/seed.md"]


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


def test_v2_vector_mode_excludes_fts_recall(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/fts.md", "FTS", "unique lexical term", type="concept")
    _write(root, "wiki/concepts/vector.md", "Vector", "semantic meaning", type="concept")
    refresh_indexes(root)
    vector_passage_id = next(
        str(record["passage_id"])
        for record in RetrievalIndexStore(root).vector_records()
        if record["page_path"] == "wiki/concepts/vector.md"
    )
    monkeypatch.setattr(
        "netsuite_llm_wiki_mcp.query_pipeline._vector_hits",
        lambda *_args, **_kwargs: ({vector_passage_id: (1, 0.9)}, []),
    )

    result = run_query_v2(root, "unique lexical term", retrieval_mode="vector")

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/vector.md"]
    assert result["pipeline"]["retrieval_mode"] == "vector"
    assert result["pipeline"]["counters"]["fts_hits"] == 0


def test_v2_vector_only_recall_does_not_bypass_filters(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/alpha.md", "Alpha", "no lexical overlap", type="concept", project="alpha", tags=["finance"])
    refresh_indexes(root)
    passage_id = str(next(iter(RetrievalIndexStore(root).vector_records()))["passage_id"])
    monkeypatch.setattr("netsuite_llm_wiki_mcp.query_pipeline._vector_hits", lambda *_args, **_kwargs: ({passage_id: (1, 0.9)}, []))

    result = run_query_v2(root, "unrelated query", project="beta", filters=QueryFilters(type="concept", tags=("finance",)))
    assert result["results"] == []

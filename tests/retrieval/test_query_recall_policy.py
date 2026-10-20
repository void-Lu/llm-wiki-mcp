from pathlib import Path

from retrieval.candidate_items import candidate_item
from retrieval.query_pipeline import run_query_v2
from retrieval.query_recall_policy import (
    MAX_ADAPTIVE_SCORE_RATIO,
    PASSAGE_PROBE_LIMIT,
    PASSAGE_SCAN_LIMIT,
    adaptive_expand,
    classify_intent,
    fts_ranked_evidence,
    merge_coverage_items,
    merge_weak_primary_raw,
    page_coverage_terms,
    primary_fill_is_strong,
    relaxed_fill_is_strong,
    uncovered_latin_terms,
    _title_token_overlap_is_strong,
)
from retrieval.query_shared import QueryFilters
from retrieval.retrieval_index import PassageHit, RetrievalIndexStore
from runtime.runtime_config import EmbeddingSettings
from tests.helpers import write_test_page
from wiki.wiki_paths import create_wiki_root
from wiki.wiki_index import refresh_indexes


def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    write_test_page(
        root, path, {"title": title, "generated": True, **frontmatter}, body
    )


def test_passage_scan_limits_are_separate_from_body_budget_policy() -> None:
    assert PASSAGE_SCAN_LIMIT == 500
    assert PASSAGE_PROBE_LIMIT == 20


def test_classify_intent_treats_multiword_howto_questions_as_concepts() -> None:
    assert (
        classify_intent(
            "配置netsuite系统内ai connector的mcp工具的完整步骤是什么，需要详细介绍需要安装和勾选的配置内容"
        )
        == "concept"
    )
    assert (
        classify_intent("subsidiary在自定义list类型字段上的内部id是什么")
        == "exact_evidence"
    )
    assert classify_intent("N/record模块有哪些方法？") == "exact_evidence"
    assert classify_intent("invoice approval") == "exact_entity"


def test_v2_reports_unresolved_fuzzy_terms_when_primary_recall_is_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/chatbot-guide.md",
        "NetSuite ChatBot Guide",
        "The ChatBot page script uses N/llm and Suitelet to render the page.",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "我想写一个llm chatbox的sl页面脚本", top_k=5)

    assert result["ok"] is True
    # chatbox is bridged by edit distance to the title word ChatBot, so it is
    # not suggested; llm and sl have no corpus variant and are the fuzzy hints
    # the caller's model should resolve.
    assert result["expansion_suggestions"] == ["llm", "sl"]
    assert result["results"][0]["path"] == "wiki/concepts/chatbot-guide.md"


def test_v2_agent_supplied_expansion_terms_reach_documents_with_abbreviated_query(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/suitelet-guide.md",
        "Suitelet Guide",
        "A Suitelet script renders a server-side page in the NetSuite UI.",
        type="concept",
    )
    refresh_indexes(root)

    first = run_query_v2(root, "sl 页面脚本", top_k=5)
    assert first["ok"] is True
    assert first["results"] == []
    assert first["expansion_suggestions"] == ["sl"]

    # The agent resolves sl -> suitelet with its own model and retries.
    retried = run_query_v2(
        root, "sl 页面脚本", top_k=5, expansion_terms={"sl": ["suitelet"]}
    )
    assert retried["results"][0]["path"] == "wiki/concepts/suitelet-guide.md"
    assert retried["expansion_suggestions"] == []


def test_v2_expansion_suggestions_stay_empty_when_primary_recall_exists(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/invoice.md",
        "Invoice",
        "Invoice approval requires a role with the approval permission.",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "invoice approval", top_k=5)

    assert result["ok"] is True
    assert result["results"]
    assert result["expansion_suggestions"] == []


def test_uncovered_latin_terms_only_reports_missing_selected_passage_tokens() -> None:
    selected = [
        {
            "hit": PassageHit(
                "p1",
                "wiki/concepts/invoice.md",
                "Invoice",
                (),
                "invoice approval",
                1.0,
                "knowledge",
                "high",
                "concept",
            )
        }
    ]

    assert uncovered_latin_terms("invoice RAG 和 LLM", selected) == ["rag", "llm"]
    assert uncovered_latin_terms("知识检索", selected) == []


def test_v2_coverage_recovery_merges_raw_evidence_when_primary_misses_latin_term(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/invoice.md",
        "Invoice",
        "invoice approval is documented here",
        type="concept",
    )
    raw = root / "raw/sources/references/rag.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: RAG Reference\n---\n\nRAG evidence complements invoice retrieval.",
        encoding="utf-8",
    )
    refresh_indexes(root)
    active_hit = RetrievalIndexStore(root).search_fts("invoice")[0]

    monkeypatch.setattr(
        "retrieval.query_pipeline._vector_hits",
        lambda *_args, **_kwargs: ({active_hit.passage_id: (1, 0.99)}, []),
    )
    result = run_query_v2(
        root,
        "invoice RAG",
        top_k=2,
        embedding=EmbeddingSettings(enabled=True),
        retrieval_mode="hybrid",
    )

    assert {item["path"] for item in result["results"]} == {
        "wiki/concepts/invoice.md",
        "raw/sources/references/rag.md",
    }
    assert result["pipeline"]["fallback"] == {
        "level": "raw",
        "reasons": ["wiki_primary_missing_latin_coverage"],
        "allowed_source_paths": ["raw/sources/references/rag.md"],
    }
    assert result["pipeline"]["coverage"]["uncovered_latin_terms"] == ["rag"]


def test_v2_coverage_keeps_primary_results_when_raw_does_not_cover_gap(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/invoice.md",
        "Invoice",
        "invoice approval is documented here",
        type="concept",
    )
    raw = root / "raw/sources/references/unrelated.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Unrelated\n---\n\nOnly unrelated evidence.", encoding="utf-8"
    )
    refresh_indexes(root)
    active_hit = RetrievalIndexStore(root).search_fts("invoice")[0]
    monkeypatch.setattr(
        "retrieval.query_pipeline._vector_hits",
        lambda *_args, **_kwargs: ({active_hit.passage_id: (1, 0.99)}, []),
    )

    result = run_query_v2(
        root,
        "invoice RAG",
        embedding=EmbeddingSettings(enabled=True),
        retrieval_mode="hybrid",
    )

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/invoice.md"]
    assert result["pipeline"]["fallback"]["level"] == "none"
    assert result["pipeline"]["coverage"]["triggered"] is False


def test_v2_all_scope_coverage_extends_wiki_relaxed_results(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/rag.md",
        "RAG Notes",
        "RAG is the curated primary answer.",
        type="concept",
    )
    raw = root / "raw/sources/references/llm.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: LLM Reference\n---\n\nLLM evidence fills the missing term.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "RAG LLM", scope="all", retrieval_mode="lexical", top_k=2
    )

    assert {item["path"] for item in result["results"]} == {
        "wiki/concepts/rag.md",
        "raw/sources/references/llm.md",
    }
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] > 0
    assert result["pipeline"]["coverage"] == {
        "uncovered_latin_terms": ["llm"],
        "triggered": True,
    }
    assert result["pipeline"]["fallback"] == {
        "level": "raw",
        "reasons": ["wiki_primary_missing_latin_coverage"],
        "allowed_source_paths": ["raw/sources/references/llm.md"],
    }


def test_v2_coverage_does_not_open_raw_store_when_primary_covers_terms(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/invoice.md",
        "Invoice",
        "invoice approval is documented here",
        type="concept",
    )
    refresh_indexes(root)

    original_status = RetrievalIndexStore.status

    def reject_raw_status(self: RetrievalIndexStore) -> dict[str, object]:
        if self.scope == "raw":
            raise AssertionError("covered primary terms must not open the raw store")
        return original_status(self)

    monkeypatch.setattr(RetrievalIndexStore, "status", reject_raw_status)
    result = run_query_v2(root, "invoice approval", retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/invoice.md"]
    assert result["pipeline"]["coverage"] == {
        "uncovered_latin_terms": [],
        "triggered": False,
    }


def test_v2_cjk_only_query_does_not_open_raw_store(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/knowledge.md",
        "知识检索",
        "知识检索结果来自已索引的知识库。",
        type="concept",
    )
    refresh_indexes(root)

    original_status = RetrievalIndexStore.status

    def reject_raw_status(self: RetrievalIndexStore) -> dict[str, object]:
        if self.scope == "raw":
            raise AssertionError("CJK-only queries must not open the raw store")
        return original_status(self)

    monkeypatch.setattr(RetrievalIndexStore, "status", reject_raw_status)
    result = run_query_v2(root, "知识检索", retrieval_mode="lexical")

    assert result["pipeline"]["coverage"] == {
        "uncovered_latin_terms": [],
        "triggered": False,
    }


def test_coverage_fusion_uses_raw_source_rank_not_bm25_absolute_value() -> None:
    active = {
        "hit": PassageHit(
            "a",
            "wiki/concepts/active.md",
            "Active",
            (),
            "active evidence",
            1.0,
            "knowledge",
            "high",
            "concept",
        ),
        "score": 4.0,
    }
    raw_first = {
        "hit": PassageHit(
            "r1",
            "raw/sources/references/first.md",
            "RAG first",
            (),
            "rag evidence",
            100.0,
            "raw",
            "low",
            "raw",
        ),
        "score": 100.0,
    }
    raw_second = {
        "hit": PassageHit(
            "r2",
            "raw/sources/references/second.md",
            "RAG second",
            (),
            "rag evidence",
            1.0,
            "raw",
            "low",
            "raw",
        ),
        "score": 1.0,
    }
    baseline = merge_coverage_items([active], [raw_first, raw_second], ["rag"])

    raw_first["score"] = 10_000.0
    raw_second["score"] = 2.0
    changed_magnitude = merge_coverage_items([active], [raw_first, raw_second], ["rag"])

    assert [item["hit"].page_path for item in changed_magnitude] == [
        item["hit"].page_path for item in baseline
    ]


def test_coverage_fusion_uses_effective_rrf_k() -> None:
    active = {
        "hit": PassageHit(
            "a",
            "wiki/concepts/active.md",
            "Active",
            (),
            "active rag",
            1.0,
            "knowledge",
            "high",
            "concept",
        ),
        "score": 4.0,
    }
    raw = [
        {
            "hit": PassageHit(
                "r",
                "raw/sources/references/rag.md",
                "RAG",
                (),
                "rag evidence",
                1.0,
                "raw",
                "low",
                "raw",
            ),
            "score": 1.0,
        },
        {
            "hit": PassageHit(
                "r2",
                "raw/sources/references/rag-2.md",
                "RAG 2",
                (),
                "rag evidence",
                0.5,
                "raw",
                "low",
                "raw",
            ),
            "score": 0.5,
        },
    ]

    small = merge_coverage_items([active], raw, ["rag"], rrf_k=10)
    large = merge_coverage_items([active], raw, ["rag"], rrf_k=120)

    small_second = next(
        item for item in small if item["hit"].page_path.endswith("rag-2.md")
    )
    large_second = next(
        item for item in large if item["hit"].page_path.endswith("rag-2.md")
    )
    assert small_second["source_local_rrf"] != large_second["source_local_rrf"]
    assert small_second["score"] != large_second["score"]


def test_merge_coverage_items_prefers_high_coverage_raw_over_padding_wiki() -> None:
    wiki = candidate_item(
        PassageHit(
            passage_id="w1",
            page_path="wiki/concepts/n-search.md",
            title="N/search",
            heading_path=(),
            text="unauthenticated client module search APIs only",  # 2/3 uncovered; tips old formula to wiki
            score=80.0,
            corpus="active",
            authority="formal",
            source_kind="wiki",
            ordinal=0,
        ),
        score=80.0,
    )
    raw = candidate_item(
        PassageHit(
            passage_id="r1",
            page_path="raw/sources/file/Help/Nsearch Module.md",
            title="N/search Module",
            heading_path=(),
            text="unauthenticated client-side contexts are not supported",
            score=5.0,
            corpus="raw",
            authority="",
            source_kind="raw",
            ordinal=0,
        ),
        score=5.0,
    )
    merged = merge_coverage_items(
        [wiki],
        [raw],
        uncovered_terms=["unauthenticated", "client", "contexts"],
        rrf_k=10,
    )
    assert merged, "expected fused rows"
    assert merged[0]["hit"].source_kind == "raw"
    assert merged[0]["coverage_ratio"] == 1.0


def _raw_hit(
    path: str,
    *,
    passage_id: str,
    title: str,
    text: str,
    ordinal: int,
    score: float,
) -> dict:
    return candidate_item(
        PassageHit(
            passage_id=passage_id,
            page_path=path,
            title=title,
            heading_path=(),
            text=text,
            score=score,
            corpus="raw",
            authority="",
            source_kind="raw",
            ordinal=ordinal,
        ),
        score=score,
        fts_rank=ordinal + 1,
    )


PAGE_NSEARCH = "raw/sources/file/NetSuite Help Docs/41_Nsearch Module/Nsearch Module.md"


def test_page_coverage_terms_unions_sibling_passages() -> None:
    best_junk = _raw_hit(
        PAGE_NSEARCH,
        passage_id="p-junk",
        title="N/search Module",
        text="/ docs . oracle . com / en / cloud / table junk",
        ordinal=0,
        score=50.0,
    )
    sibling = _raw_hit(
        PAGE_NSEARCH,
        passage_id="p-limit",
        title="N/search Module",
        text="unsupported in unauthenticated client-side contexts",
        ordinal=3,
        score=5.0,
    )
    uncovered = ["unauthenticated", "side", "contexts"]
    assert page_coverage_terms([best_junk], uncovered) == set()
    assert page_coverage_terms([best_junk, sibling], uncovered) == {
        "unauthenticated",
        "side",
        "contexts",
    }


def test_merge_coverage_admits_raw_when_sibling_passage_covers_uncovered() -> None:
    """Best ordinal misses uncovered; sibling on same page hits -> page admitted."""
    best_junk = _raw_hit(
        PAGE_NSEARCH,
        passage_id="p-junk",
        title="N/search Module",
        text="/ docs . oracle . com / en / cloud / table junk",
        ordinal=0,
        score=50.0,
    )
    sibling = _raw_hit(
        PAGE_NSEARCH,
        passage_id="p-limit",
        title="N/search Module",
        text="unsupported in unauthenticated client-side contexts",
        ordinal=3,
        score=5.0,
    )
    uncovered = ["unauthenticated", "side", "contexts"]
    merged = merge_coverage_items([], [best_junk, sibling], uncovered, rrf_k=10)
    assert len(merged) == 1
    assert merged[0]["hit"].page_path == PAGE_NSEARCH
    # Display carrier remains page-best (higher score), not the sibling
    assert merged[0]["hit"].passage_id == "p-junk"
    assert merged[0]["coverage_ratio"] == 1.0
    assert set(merged[0]["coverage_terms"]) == set(uncovered)


def test_merge_coverage_still_filters_raw_when_no_passage_covers() -> None:
    only_junk = _raw_hit(
        PAGE_NSEARCH,
        passage_id="p-junk",
        title="N/search Module",
        text="/ docs . oracle . com / en / cloud / table junk",
        ordinal=0,
        score=50.0,
    )
    merged = merge_coverage_items(
        [], [only_junk], ["unauthenticated", "contexts"], rrf_k=10
    )
    assert merged == []


def test_v2_coverage_merge_surfaces_raw_help_doc(tmp_path: Path) -> None:
    """0015-shaped: strong-ish wiki covers search/module; raw Help fills uncovered Latin."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/n-search.md",
        "N/search",
        "module search APIs only",
        type="concept",
    )
    raw = (
        root
        / "raw/sources/file/NetSuite Help Docs"
        / "Nsearch Module.md"
    )
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# N/search Module\n\n"
        "unauthenticated client-side contexts are not supported for the N/search module.\n",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "unauthenticated client-side contexts N/search module",
        scope="knowledge",
        retrieval_mode="lexical",
        top_k=10,
    )
    paths = [item["path"] for item in result["results"]]
    assert any(path.startswith("raw/") for path in paths), paths
    assert result["pipeline"]["coverage"]["triggered"] is True


def test_v2_raw_fallback_uses_raw_documents_after_formal_miss(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "access reports", retrieval_mode="lexical")

    assert result["results"]
    assert result["results"][0]["path"] == "raw/sources/file/default/help.md"
    assert result["results"][0]["source_kind"] == "raw"


def test_v2_raw_fallback_uses_bounded_prefix_recovery(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/ingestion.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# Ingestion\n\nThe ingestion pipeline is the raw answer.", encoding="utf-8"
    )
    refresh_indexes(root)

    result = run_query_v2(root, "ingest", retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == [
        "raw/sources/file/default/ingestion.md"
    ]
    assert result["pipeline"]["lexical"]["mode"] == "raw_prefix"
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 1


def test_v2_raw_fallback_uses_the_dedicated_fts_store_without_scanning_sources(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw" / "sources" / "manual.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# API evidence\n\nfield_id is custbody_approval_state token=super-secret",
        encoding="utf-8",
    )
    refresh_indexes(root)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw source scan")
        ),
    )
    result = run_query_v2(root, "custbody_approval_state", retrieval_mode="lexical")

    raw_passages = [item for item in result["results"] if item["source_kind"] == "raw"]
    assert raw_passages and "custbody_approval_state" in raw_passages[0]["content"]
    assert "super-secret" not in raw_passages[0]["content"]
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert result["pipeline"]["fallback"]["reasons"] == ["wiki_zero_results"]
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 1


def test_v2_raw_fallback_reports_missing_corrupt_and_stale_raw_indexes(
    tmp_path: Path,
) -> None:
    for state in ("missing", "corrupt", "stale"):
        root = tmp_path / state
        create_wiki_root(root)
        raw = root / "raw/sources/file/default/marker.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("raw lifecycle marker", encoding="utf-8")
        refresh_indexes(root)
        raw_store = RetrievalIndexStore(root, scope="raw")
        if state == "missing":
            raw_store.path.unlink()
        elif state == "corrupt":
            raw_store.path.write_bytes(b"not a sqlite database")
        else:
            raw_store.mark_stale()

        result = run_query_v2(root, "raw lifecycle marker", retrieval_mode="lexical")

        assert result["ok"] is True
        assert result["results"] == []
        assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
        assert f"raw_index_{state}" in result["pipeline"]["warnings"]


def test_v2_raw_fallback_reranks_page_titles_and_deduplicates_pages(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    titled = root / "raw/sources/file/default/target.md"
    other = root / "raw/sources/file/default/other.md"
    titled.parent.mkdir(parents=True, exist_ok=True)
    titled.write_text("# Target Feature\n\nshared raw evidence", encoding="utf-8")
    other.write_text(
        "# Other Feature\n\nTarget appears in the body with shared raw evidence",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "target", retrieval_mode="lexical", top_k=2)

    assert [item["path"] for item in result["results"]] == [
        "raw/sources/file/default/target.md",
        "raw/sources/file/default/other.md",
    ]
    assert len({item["path"] for item in result["results"]}) == 2


def test_v2_raw_fallback_recovers_qualified_module_names_from_chinese_questions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for module, expected_path in (
        ("record", "raw/sources/references/n-record.md"),
        ("search", "raw/sources/references/n-search.md"),
    ):
        source = root / expected_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            f"# N/{module} Module\n\nN/{module} module methods and API reference.",
            encoding="utf-8",
        )
    refresh_indexes(root)

    for question, expected_path in (
        ("N/record模块有哪些方法？", "raw/sources/references/n-record.md"),
        ("N/search模块有哪些方法？", "raw/sources/references/n-search.md"),
        ("N/record module methods", "raw/sources/references/n-record.md"),
        ("N/search module methods", "raw/sources/references/n-search.md"),
    ):
        result = run_query_v2(root, question, retrieval_mode="lexical")

        assert [item["path"] for item in result["results"]] == [expected_path]
        assert result["pipeline"]["fallback"]["level"] == "raw"
        assert result["pipeline"]["lexical"]["mode"] == "qualified_code"


def test_v2_wiki_relaxed_answer_blocks_raw_fallback(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    reference = root / "raw" / "sources" / "references" / "location.md"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(
        "# Custom Record Type Object Custom Fields\n\nLocation uses selectrecordtype -103 for a SELECT field.",
        encoding="utf-8",
    )
    _write(
        root,
        "wiki/concepts/noisy-location.md",
        "Location overview",
        "Location is a standard record that can be selected on transactions.",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "NetSuite 的 Location 字段 selectrecordtype 数字 ID 是多少？",
        retrieval_mode="lexical",
    )

    paths = [item["path"] for item in result["results"]]
    assert paths == ["wiki/concepts/noisy-location.md"]
    assert result["pipeline"]["fallback"]["level"] == "none"
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] > 0



def test_relaxed_fill_is_strong_detects_title_phrase_and_identifier() -> None:
    weak_hit = PassageHit(
        passage_id="p-weak",
        page_path="wiki/concepts/noise.md",
        title="杂谈",
        heading_path=(),
        text="治理 单位 列表 摩擦 owner 模块 深度",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    title_hit = PassageHit(
        passage_id="p-title",
        page_path="wiki/concepts/location.md",
        title="Location overview",
        heading_path=(),
        text="unrelated body noise only",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    phrase_hit = PassageHit(
        passage_id="p-phrase",
        page_path="wiki/concepts/connector.md",
        title="misc",
        heading_path=(),
        text="Configure the NetSuite AI Connector before enabling MCP tools.",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    ident_hit = PassageHit(
        passage_id="p-ident",
        page_path="wiki/concepts/n-record.md",
        title="record helpers",
        heading_path=(),
        text="Use N/record create and load for standard records.",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )

    assert not relaxed_fill_is_strong(
        "架构深化 技术债 剩余摩擦 owner 模块",
        [{"hit": weak_hit}],
    )
    assert relaxed_fill_is_strong(
        "NetSuite 的 Location 字段 selectrecordtype 数字 ID 是多少？",
        [{"hit": title_hit}],
    )
    assert relaxed_fill_is_strong(
        "配置 NetSuite AI Connector 需要哪些权限",
        [{"hit": phrase_hit}],
    )
    assert relaxed_fill_is_strong(
        "n/record模块有哪些方法",
        [{"hit": ident_hit}],
    )
    assert not relaxed_fill_is_strong("anything", [])

    fuzzy_hit = PassageHit(
        passage_id="p-fuzzy",
        page_path="wiki/concepts/chatbot-guide.md",
        title="NetSuite ChatBot Guide",
        heading_path=(),
        text="Suitelet page script notes.",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    assert relaxed_fill_is_strong(
        "我想写一个llm chatbox的sl页面脚本",
        [{"hit": fuzzy_hit}],
    )
    # Body-only expansion alias must stay weak (title/path evidence only).
    assert not relaxed_fill_is_strong(
        "sl 页面脚本",
        [{"hit": fuzzy_hit}],
        extra_terms=["suitelet"],
    )
    suitelet_hit = PassageHit(
        passage_id="p-suitelet",
        page_path="wiki/concepts/suitelet-guide.md",
        title="Suitelet Guide",
        heading_path=(),
        text="unrelated body without the expansion cue",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    assert relaxed_fill_is_strong(
        "sl 页面脚本",
        [{"hit": suitelet_hit}],
        extra_terms=["suitelet"],
    )


def test_v2_weak_wiki_relaxed_abstains_and_allows_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Wiki pages share only loose OR tokens with the query — no identifier/title hit
    _write(
        root,
        "wiki/concepts/noise-a.md",
        "杂谈",
        "治理 单位 列表 摩擦 owner 模块 深度",
        type="concept",
    )
    _write(
        root,
        "wiki/concepts/noise-b.md",
        "随笔",
        "架构 剩余 技术债 思考 guide",
        type="concept",
    )
    raw = root / "raw/sources/references/architecture-debt.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# 架构深化 技术债\n\nowner 模块 剩余摩擦 权威说明。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    # Query with no N/… and no multiword English identifier phrase;
    # strict misses; relaxed would only OR-match noise; raw has topical doc.
    result = run_query_v2(
        root,
        "架构深化 技术债 剩余摩擦 owner 模块",
        scope="knowledge",
        retrieval_mode="lexical",
    )
    # After weak abstain, either empty (if raw unavailable in fixture path)
    # or raw_zero filled — never the noise-only wiki pages as a fake answer.
    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/noise-a.md" not in paths
    assert "wiki/concepts/noise-b.md" not in paths
    assert result["pipeline"]["counters"]["fts_hits"] == 0
    assert paths, "expected raw_zero fill after weak abstain"
    assert paths[0].startswith("raw/")
    assert result["pipeline"]["fallback"]["level"] == "raw"



def test_primary_fill_is_strong_uses_qualified_hits_and_relaxed_signals() -> None:
    weak_hit = PassageHit(
        passage_id="p-weak",
        page_path="wiki/concepts/noise.md",
        title="杂谈笔记",
        heading_path=(),
        text="mcp 部署 standard 权限 tools 勾选 server 说明 suitescript guide 安装清单",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    title_hit = PassageHit(
        passage_id="p-title",
        page_path="wiki/concepts/suitescript-guide.md",
        title="SuiteScript Guide",
        heading_path=(),
        text="unrelated body noise only",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    question = "MCP Standard Tools 安装清单 Server SuiteScript"
    assert not primary_fill_is_strong(question, [])
    assert not primary_fill_is_strong(question, [{"hit": weak_hit}])
    assert primary_fill_is_strong(
        question, [{"hit": weak_hit}], qualified_fts_hits=1
    )
    assert primary_fill_is_strong(question, [{"hit": title_hit}])




def test_primary_fill_is_strong_ignores_body_only_phrase() -> None:
    body_phrase_hit = PassageHit(
        passage_id="p-body",
        page_path="wiki/concepts/noise.md",
        title="杂谈笔记",
        heading_path=(),
        text="see Custom Record Type Object Custom Fields in passing",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    title_phrase_hit = PassageHit(
        passage_id="p-title",
        page_path="wiki/concepts/custom-fields.md",
        title="Custom Record Type Object Custom Fields",
        heading_path=(),
        text="unrelated",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    q = "Custom Record Type Object Custom Fields"
    assert not primary_fill_is_strong(q, [{"hit": body_phrase_hit}])
    assert primary_fill_is_strong(q, [{"hit": title_phrase_hit}])
    # relaxed path still treats body phrase as strong (P0 default)
    assert relaxed_fill_is_strong(q, [{"hit": body_phrase_hit}])


def test_title_token_overlap_strong_formula() -> None:
    assert not _title_token_overlap_is_strong({"record"}, body_evidence=False)
    assert _title_token_overlap_is_strong({"record", "type"}, body_evidence=False)
    assert _title_token_overlap_is_strong({"suitescript"}, body_evidence=False)
    assert _title_token_overlap_is_strong({"record"}, body_evidence=True)


def test_primary_fill_is_strong_rejects_single_generic_title_token() -> None:
    """0018: N/record 模块 ∩ Custom Record… → {record} must not be primary-strong."""
    hit = PassageHit(
        passage_id="p-n-record",
        page_path="wiki/concepts/netsuite-ss-modules/n-record.md",
        title="N/record 模块",
        heading_path=(),
        text="unrelated body noise only",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    q = "Custom Record Type Object Custom Fields"
    assert not primary_fill_is_strong(q, [{"hit": hit}])
    # relaxed path unchanged: single title-token overlap still strong
    assert relaxed_fill_is_strong(q, [{"hit": hit}])


def test_primary_fill_is_strong_accepts_multi_generic_title_token_overlap() -> None:
    """All-generic overlap with |ov|≥2 stays primary-strong (approved exception)."""
    hit = PassageHit(
        passage_id="p-record-type",
        page_path="wiki/concepts/record-type.md",
        title="Record Type helpers",
        heading_path=(),
        text="unrelated body noise only",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    # tokens(title) ⊇ {record, type, helpers}; ∩ query → {record, type} (|ov|=2)
    q = "Custom Record Type Object Custom Fields"
    assert primary_fill_is_strong(q, [{"hit": hit}])


def _passage_hit(path: str, title: str, text: str = "unrelated body noise only") -> PassageHit:
    return PassageHit(
        passage_id=f"p-{path}",
        page_path=path,
        title=title,
        heading_path=(),
        text=text,
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )


def test_fts_ranked_evidence_keeps_only_fts_rank() -> None:
    fts = candidate_item(
        _passage_hit("wiki/concepts/netsuite-ss-modules/n-record.md", "N/record 模块"),
        score=1.0,
        fts_rank=2,
    )
    title_only = candidate_item(
        _passage_hit(
            "wiki/concepts/script-types/Custom-Tool-Script.md",
            "SuiteScript 2.1 脚本类型：Custom Tool Script",
        ),
        score=0.5,
        title_rank=9,
    )
    graphish = candidate_item(
        _passage_hit("wiki/other.md", "Other"),
        score=0.1,
        # fts_rank left None — not FTS evidence
    )
    out = fts_ranked_evidence([fts, title_only, graphish])
    assert out == [fts]
    assert fts_ranked_evidence([]) == []
    # is not None: fts_rank=0 must be kept (truthiness would wrongly drop it)
    zero = candidate_item(
        _passage_hit("wiki/concepts/zero.md", "Zero"),
        score=1.0,
        fts_rank=0,
    )
    assert fts_ranked_evidence([zero, title_only]) == [zero]


def test_primary_strong_evidence_ignores_title_only_custom_token() -> None:
    """0018 residual: title-candidate {custom} must not make primary strong
    when FTS slice is only weak {record}.
    """
    fts_weak = candidate_item(
        _passage_hit("wiki/concepts/netsuite-ss-modules/n-record.md", "N/record 模块"),
        score=1.0,
        fts_rank=2,
    )
    title_custom = candidate_item(
        _passage_hit(
            "wiki/concepts/script-types/Custom-Tool-Script.md",
            "SuiteScript 2.1 脚本类型：Custom Tool Script",
        ),
        score=0.5,
        title_rank=9,
    )
    q = "Custom Record Type Object Custom Fields"
    # Alone-strong lock: title-only {custom} is still "strong" as a page today
    # (custom ∉ GENERIC); call site must not pass it as primary-strong evidence.
    assert primary_fill_is_strong(q, [title_custom])
    assert not primary_fill_is_strong(q, fts_ranked_evidence([fts_weak, title_custom]))
    # FTS non-generic still strong even if title-only noise is in the seed
    suitescript = candidate_item(
        _passage_hit("wiki/guides/suitescript.md", "SuiteScript Guide"),
        score=1.0,
        fts_rank=1,
    )
    assert primary_fill_is_strong(
        "How does SuiteScript work?", fts_ranked_evidence([suitescript, title_custom])
    )


def test_v2_body_only_primary_phrase_allows_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/noise-primary.md",
        "杂谈笔记",
        "see Custom Record Type Object Custom Fields mentioned once",
        type="concept",
    )
    raw = (
        root
        / "raw/sources/file/NetSuite Help Docs"
        / "Custom Record Type Object Custom Fields.md"
    )
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# Custom Record Type Object Custom Fields\n\n"
        "SDF customrecordcustomfields XML example and role limits.\n",
        encoding="utf-8",
    )
    refresh_indexes(root)
    result = run_query_v2(
        root,
        '"Custom Record Type Object Custom Fields"',
        scope="knowledge",
        retrieval_mode="lexical",
    )
    paths = [item["path"] for item in result["results"]]
    assert any(path.startswith("raw/") for path in paths), paths
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert "wiki_weak_primary" in result["pipeline"]["fallback"]["reasons"]


def test_v2_weak_primary_allows_capped_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Strict AND hits via scattered Latin tokens + contiguous CJK; no title /
    # identifier-phrase overlap so primary_fill_is_strong stays false. All
    # Latin query terms appear in the wiki body so coverage does not schedule.
    _write(
        root,
        "wiki/concepts/noise-primary.md",
        "杂谈笔记",
        "mcp 部署 standard 权限 tools 勾选 server 说明 suitescript guide 安装清单",
        type="concept",
    )
    raw = root / "raw/sources/references/mcp-install-checklist.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# MCP Standard Tools 安装清单\n\n"
        "Server SuiteScript 与 REST Web Services 勾选后安装 MCP Standard Tools。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "MCP Standard Tools 安装清单 Server SuiteScript",
        scope="knowledge",
        retrieval_mode="lexical",
    )
    paths = [item["path"] for item in result["results"]]
    assert paths, "expected results"
    # Merge path: raw must enter the pool; wiki may remain (not wholesale replace).
    assert any(path.startswith("raw/") for path in paths), paths
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert "wiki_weak_primary" in result["pipeline"]["fallback"]["reasons"]
    assert result["pipeline"]["counters"]["fts_hits"] > 0




def test_v2_weak_primary_raw_merge_keeps_wiki(tmp_path: Path) -> None:
    """Weak primary + raw must MERGE: wiki paths stay when raw also fills."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/noise-primary.md",
        "杂谈笔记",
        "mcp 部署 standard 权限 tools 勾选 server 说明 suitescript guide 安装清单",
        type="concept",
    )
    raw = root / "raw/sources/references/mcp-install-checklist.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# MCP Standard Tools 安装清单\n\n"
        "Server SuiteScript 与 REST Web Services 勾选后安装 MCP Standard Tools。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "MCP Standard Tools 安装清单 Server SuiteScript",
        scope="knowledge",
        retrieval_mode="lexical",
        top_k=10,
    )
    paths = [item["path"] for item in result["results"]]
    assert paths, "expected results"
    assert any(path.startswith("wiki/") for path in paths), paths
    assert any(path.startswith("raw/") for path in paths), paths
    assert "wiki_weak_primary" in result["pipeline"]["fallback"]["reasons"]


def test_merge_weak_primary_raw_keeps_wiki_and_caps_raw() -> None:
    wiki_hit = PassageHit(
        passage_id="p-wiki",
        page_path="wiki/concepts/noise-primary.md",
        title="杂谈笔记",
        heading_path=(),
        text="mcp standard tools server suitescript",
        score=12.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    raw_hit = PassageHit(
        passage_id="p-raw",
        page_path="raw/sources/references/mcp-install-checklist.md",
        title="MCP Standard Tools 安装清单",
        heading_path=(),
        text="Server SuiteScript checklist",
        score=40.0,
        corpus="raw",
        authority="",
        source_kind="raw",
        ordinal=0,
    )
    merged = merge_weak_primary_raw(
        [{"hit": wiki_hit, "score": 12.0}],
        [{"hit": raw_hit, "score": 40.0}],
        raw_cap=20,
    )
    paths = [item["hit"].page_path for item in merged]
    assert "wiki/concepts/noise-primary.md" in paths
    assert "raw/sources/references/mcp-install-checklist.md" in paths


def test_v2_strong_primary_still_blocks_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/suitescript-guide.md",
        "SuiteScript Guide",
        "Server SuiteScript notes for MCP Standard Tools install checklist.",
        type="concept",
    )
    raw = root / "raw/sources/references/mcp-install-checklist.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# MCP Standard Tools 安装清单\n\n"
        "Server SuiteScript 与 REST Web Services 勾选后安装 MCP Standard Tools。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "MCP Standard Tools 安装清单 Server SuiteScript",
        scope="knowledge",
        retrieval_mode="lexical",
    )
    paths = [item["path"] for item in result["results"]]
    assert paths, "expected results"
    assert paths[0].startswith("wiki/"), paths
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
    assert result["pipeline"]["fallback"]["level"] != "raw"


def test_v2_raw_fallback_combines_identifier_phrase_docs_above_bigram_noise(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    install = root / "raw" / "sources" / "references" / "install-mcp-std-tools.md"
    install.parent.mkdir(parents=True, exist_ok=True)
    install.write_text(
        "# Installing the MCP Standard Tools SuiteApp\n\n"
        "To install the MCP Standard Tools SuiteApp for the NetSuite AI Connector Service: "
        "check the Server SuiteScript box and the REST Web Services box on the SuiteCloud subtab. "
        "Then go to the SuiteApps tab and install MCP Standard Tools. "
        "Connect using https://<accountid>.suitetalk.api.netsuite.com/services/mcp/v1/suiteapp/com.netsuite.mcpstandardtools",
        encoding="utf-8",
    )
    permissions = (
        root / "raw" / "sources" / "references" / "required-features-permissions.md"
    )
    permissions.parent.mkdir(parents=True, exist_ok=True)
    permissions.write_text(
        "# Required Features and Permissions\n\n"
        "The NetSuite AI Connector Service needs Server SuiteScript and OAuth 2.0 enabled. "
        "Add the MCP Server Connection permission and Log in using OAuth 2.0 Access Tokens to each role.",
        encoding="utf-8",
    )
    connect = root / "raw" / "sources" / "references" / "connect-ai-connector.md"
    connect.parent.mkdir(parents=True, exist_ok=True)
    connect.write_text(
        "# Connect to the NetSuite AI Connector Service\n\n"
        "In claude.ai go to Search and tools and add the NetSuite AI connector, "
        "paste the MCP server URL, then connect to the NetSuite AI Connector Service.",
        encoding="utf-8",
    )
    noise = root / "raw" / "sources" / "references" / "netsuite_quiz" / "ai-notes.md"
    noise.parent.mkdir(parents=True, exist_ok=True)
    noise.write_text(
        "# AI 综合测验\n\n"
        "mcp.json 配置 CDKB 和 IDS Tool 的 Account ID。需要安装工具，勾选配置内容，介绍完整步骤，系统要求详细说明。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "配置netsuite系统内ai connector的mcp工具的完整步骤是什么，需要详细介绍需要安装和勾选的配置内容",
        retrieval_mode="lexical",
    )

    paths = [item["path"] for item in result["results"]]
    expected = {
        "raw/sources/references/install-mcp-std-tools.md",
        "raw/sources/references/required-features-permissions.md",
        "raw/sources/references/connect-ai-connector.md",
    }
    assert set(paths[:3]) == expected
    assert result["pipeline"]["lexical"]["mode"] == "identifier_phrase"
    assert result["pipeline"]["fallback"]["level"] == "raw"
    combined = "\n".join(
        item["content"] for item in result["results"] if "content" in item
    )
    assert "Server SuiteScript" in combined
    assert "suitetalk.api.netsuite.com" in combined
    assert "MCP Server Connection" in combined
    assert "claude.ai" in combined
    noise_path = noise.relative_to(root).as_posix()
    assert noise_path not in paths
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 3


def test_v2_raw_fallback_returns_only_the_best_matching_passage_per_source_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = root / "raw" / "sources" / "manual.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "# Raw source\n\n" + "unique raw de-duplication sentinel " * 1_000,
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "unique raw de-duplication sentinel", retrieval_mode="lexical"
    )

    assert [item["path"] for item in result["results"]] == ["raw/sources/manual.md"]
    assert result["pipeline"]["counters"]["raw_fts_hits"] > 1


def test_v2_identifier_phrase_fills_procedural_sections_of_selected_guide_pages(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    guide = root / "raw" / "sources" / "references" / "connect-ai-connector.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text(
        "# Connect to the NetSuite AI Connector Service\n\n"
        "## Connect using Claude\n\n"
        "In claude.ai go to Search and tools, add the NetSuite AI connector "
        "and paste the MCP server URL for the NetSuite AI Connector Service.\n\n"
        "## Enable the required features\n\n"
        "Go to Setup > Company > Enable Features, check the Server SuiteScript "
        "and REST Web Services boxes on the SuiteCloud subtab, then click Save.\n\n"
        "## Note\n\n"
        "Execution log data is retained for 21 days in production.\n",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "配置netsuite系统内ai connector的mcp工具的完整步骤是什么，需要详细介绍需要安装和勾选的配置内容",
        retrieval_mode="lexical",
    )

    assert result["pipeline"]["lexical"]["mode"] == "identifier_phrase"
    passages = result["results"]
    combined = "\n".join(item["content"] for item in passages if "content" in item)
    assert "claude.ai" in combined
    assert "Server SuiteScript" in combined
    assert "REST Web Services" in combined


def test_v2_step_guide_bonus_prefers_numbered_procedural_page(tmp_path: Path) -> None:
    """Among pages with comparable lexical scores, the page written as numbered
    steps wins the relaxed recovery even though no keyword list is involved."""

    root = tmp_path / "vault"
    create_wiki_root(root)
    steps = "wiki/projects/example/troubleshooting/codegraph-fix-steps.md"
    _write(
        root,
        steps,
        "CodeGraph 修复：SuiteScript 解析",
        (
            "## 修复步骤\n\n"
            "1. 停掉 codegraph daemon 并释放数据库锁\n"
            "2. 修改 tree-sitter.js 补上名字回查\n"
            "3. 用 codegraph index 重建 SuiteScript 索引\n"
            "4. 用 codegraph explore 验证函数关系恢复\n"
        ),
        type="troubleshooting",
    )
    prose = "wiki/projects/example/troubleshooting/codegraph-fix-prose.md"
    _write(
        root,
        prose,
        "CodeGraph 修复：SuiteScript 解析",
        (
            "## 修复说明\n\n"
            "停掉 codegraph daemon 释放数据库锁，修改 tree-sitter.js 补上名字回查，"
            "然后用 codegraph index 重建 SuiteScript 索引，最后用 codegraph explore "
            "验证函数关系恢复。\n"
        ),
        type="troubleshooting",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "如何修复 codegraph 解析失败，具体步骤是什么",
        retrieval_mode="lexical",
    )

    assert result["results"][0]["path"] == steps


def test_v2_adaptive_expand_keeps_close_scoring_pages_above_boundary(
    tmp_path: Path,
) -> None:
    """With top_k=10, pages ranked 11+ that still score within 90% of the
    boundary are kept (up to 40); a clear score drop stops the expansion."""

    root = tmp_path / "vault"
    create_wiki_root(root)
    pages: list[str] = []
    for index in range(14):
        name = f"similar-{index:02d}"
        path = f"wiki/projects/example/troubleshooting/{name}.md"
        _write(
            root,
            path,
            f"CodeGraph 修复：SuiteScript 解析 {index}",
            (
                "## 问题\n\n"
                "codegraph 工具更新后，本地修复的 SuiteScript 脚本解析补丁失效。\n\n"
                "## 修复步骤\n\n"
                "1. 停掉 codegraph daemon 释放数据库锁\n"
                "2. 修改 tree-sitter.js 补上名字回查\n"
                "3. 用 codegraph index 重建 SuiteScript 索引\n"
                "4. 用 codegraph explore 验证函数关系恢复\n"
            ),
            type="troubleshooting",
        )
        pages.append(path)
    _write(
        root,
        "wiki/concepts/unrelated.md",
        "Unrelated overview",
        "这是一个完全无关的页面，介绍其他主题。",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "如何修复 codegraph 解析失败，具体步骤是什么",
        retrieval_mode="lexical",
        top_k=10,
    )

    returned = [item["path"] for item in result["results"]]
    all_selected = returned + [item["path"] for item in result["additional_results"]]
    assert len(returned) == 10
    assert all(path in all_selected for path in pages[:12])
    assert not any("unrelated" in path for path in all_selected)
    assert result["additional_results"]


def test_adaptive_expand_applies_global_score_floor() -> None:
    ranked = [
        {"score": score, "path": f"page-{index}"}
        for index, score in enumerate(
            [
                48.2,
                46.1,
                44.0,
                42.0,
                40.0,
                39.0,
                38.0,
                37.0,
                36.5,
                36.0,
                35.0,
                33.8,
                33.0,
                24.0,
                15.0,
            ]
        )
    ]

    selected = adaptive_expand(ranked, base_top_k=10)

    assert [item["score"] for item in selected] == [
        48.2,
        46.1,
        44.0,
        42.0,
        40.0,
        39.0,
        38.0,
        37.0,
        36.5,
        36.0,
        35.0,
        33.8,
    ]
    assert MAX_ADAPTIVE_SCORE_RATIO == 0.7


def test_v2_relaxes_multilingual_questions_after_strict_fts_returns_no_results(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/map-reduce-limits.md",
        "MapReduce 脚本各阶段点数消耗与用时上限",
        "MapReduce 脚本执行各阶段限制。 MapReduce script phases have governance limits.",
        type="concept",
    )
    _write(
        root,
        "wiki/concepts/noisy.md",
        "Generic script reference",
        "map reduce script",
        type="concept",
    )
    refresh_indexes(root)

    for question in ("mr脚本执行各阶段限制。", "map reduce script phases limits"):
        result = run_query_v2(root, question, retrieval_mode="lexical")

        assert result["results"][0]["path"] == "wiki/concepts/map-reduce-limits.md"
        assert result["pipeline"]["fallback"]["level"] == "none"
        assert result["pipeline"]["counters"]["fts_hits"] == 0
        assert result["pipeline"]["counters"]["relaxed_fts_hits"] > 0
        assert result["pipeline"]["lexical"]["mode"] == "relaxed"


def test_v2_raw_index_unavailable_yields_structured_warning(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = run_query_v2(root, "unique missing term", retrieval_mode="lexical")

    assert result["ok"] is True
    assert result["code"] == "index_missing"
    assert (
        result["message"]
        == "The retrieval index is unavailable; the query was not executed."
    )
    assert result["results"] == []
    assert "index_missing" in result["pipeline"]["warnings"]
    assert result["pipeline"]["fallback"]["level"] == "none"


def test_v2_raw_fallback_respects_project_and_type_filters(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Project filtering applies to ingest snapshots under raw/sources/projects/.
    # Vendor file trees (raw/sources/file/) intentionally leave project empty.
    alpha = root / "raw" / "sources" / "projects" / "alpha" / "manual" / "a.txt"
    alpha.parent.mkdir(parents=True, exist_ok=True)
    alpha.write_text("unique raw retrieval needle alpha", encoding="utf-8")
    beta = root / "raw" / "sources" / "projects" / "beta" / "manual" / "b.txt"
    beta.parent.mkdir(parents=True, exist_ok=True)
    beta.write_text("unique raw retrieval needle beta", encoding="utf-8")
    refresh_indexes(root)

    scoped = run_query_v2(
        root, "unique raw retrieval needle", project="alpha", retrieval_mode="lexical"
    )
    raw_type = run_query_v2(
        root,
        "unique raw retrieval needle",
        filters=QueryFilters(type="raw"),
        retrieval_mode="lexical",
    )
    other_type = run_query_v2(
        root,
        "unique raw retrieval needle",
        filters=QueryFilters(type="concept"),
        retrieval_mode="lexical",
    )

    assert [item["path"] for item in scoped["results"]] == [
        "raw/sources/projects/alpha/manual/a.txt"
    ]
    assert len(raw_type["results"]) == 2
    assert other_type["results"] == []

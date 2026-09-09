from pathlib import Path

from retrieval.query_pipeline import run_query_v2
from retrieval.query_recall_policy import (
    MAX_ADAPTIVE_SCORE_RATIO,
    PASSAGE_PROBE_LIMIT,
    PASSAGE_SCAN_LIMIT,
    adaptive_expand,
    classify_intent,
    merge_coverage_items,
    uncovered_latin_terms,
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
        "NetSuite 的 Location List/Record 字段 selectrecordtype 数字 ID 是多少？",
        retrieval_mode="lexical",
    )

    paths = [item["path"] for item in result["results"]]
    assert paths == ["wiki/concepts/noisy-location.md"]
    assert result["pipeline"]["fallback"]["level"] == "none"
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] > 0


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
    alpha = root / "raw" / "sources" / "file" / "alpha" / "manual" / "a.txt"
    alpha.parent.mkdir(parents=True, exist_ok=True)
    alpha.write_text("unique raw retrieval needle alpha", encoding="utf-8")
    beta = root / "raw" / "sources" / "file" / "beta" / "manual" / "b.txt"
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
        "raw/sources/file/alpha/manual/a.txt"
    ]
    assert len(raw_type["results"]) == 2
    assert other_type["results"] == []

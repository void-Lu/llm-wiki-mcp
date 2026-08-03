from pathlib import Path

from netsuite_llm_wiki_mcp.query_pipeline import QueryFilters, legacy_response_from_v2, run_query_v2
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore
from netsuite_llm_wiki_mcp.runtime_config import decode_global_config
import netsuite_llm_wiki_mcp.wiki_query as wiki_query_module
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
from netsuite_llm_wiki_mcp.wiki_index import refresh_indexes


def test_classify_intent_treats_multiword_howto_questions_as_concepts() -> None:
    from netsuite_llm_wiki_mcp.query_pipeline import classify_intent

    assert classify_intent("配置netsuite系统内ai connector的mcp工具的完整步骤是什么，需要详细介绍需要安装和勾选的配置内容") == "concept"
    assert classify_intent("subsidiary在自定义list类型字段上的内部id是什么") == "exact_evidence"
    assert classify_intent("N/record模块有哪些方法？") == "exact_evidence"
    assert classify_intent("invoice approval") == "exact_entity"


def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    write_wiki_page(root, WikiPage(Path(path), {"title": title, "generated": True, **frontmatter}, title, body), overwrite_generated_only=False)


def test_query_filters_from_empty_mapping_uses_empty_tags() -> None:
    assert QueryFilters.from_mapping({}) == QueryFilters()
    assert QueryFilters.from_mapping(None) == QueryFilters()


def test_query_filters_accepts_any_sequence_of_string_tags() -> None:
    assert QueryFilters.from_mapping({"tags": ("finance", "approved")}) == QueryFilters(tags=("finance", "approved"))
    assert QueryFilters.from_mapping({"tags": ["finance", "approved"]}) == QueryFilters(tags=("finance", "approved"))


def test_query_filters_rejects_non_sequence_or_non_string_tags() -> None:
    for bad_tags in ("finance", b"finance", 42, {"finance"}, ["finance", 1]):
        try:
            QueryFilters.from_mapping({"tags": bad_tags})
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for tags={bad_tags!r}")


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
    assert result["pipeline"]["authority"] == "active:formal>project>capsule>raw_chat;fallback:active_relaxed>raw_identifier>raw"


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


def test_v2_lexical_recall_is_not_starved_by_source_indexes(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for number in range(55):
        _write(
            root,
            f"wiki/sources/catalog/{number:02d}/index.md",
            f"Navigation {number}",
            "uncommon lexical retrieval token",
            type="source_index",
        )
    _write(
        root,
        "wiki/sources/capsules/target.md",
        "Target capsule",
        "uncommon lexical retrieval token",
        type="source_capsule",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "uncommon lexical retrieval token", retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == ["wiki/sources/capsules/target.md"]
    assert result["pipeline"]["counters"]["fts_hits"] == 1


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


def test_v2_raw_fallback_uses_the_dedicated_fts_store_without_scanning_sources(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw" / "sources" / "manual.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# API evidence\n\nfield_id is custbody_approval_state token=super-secret", encoding="utf-8")
    refresh_indexes(root)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw source scan")))
    result = run_query_v2(root, "custbody_approval_state", retrieval_mode="lexical")

    raw_passages = [item for item in result["context_pack"]["passages"] if item["evidence_kind"] == "raw_evidence"]
    assert raw_passages and "custbody_approval_state" in raw_passages[0]["content"]
    assert "super-secret" not in raw_passages[0]["content"]
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert result["pipeline"]["fallback"]["reasons"] == ["wiki_zero_results"]
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 1


def test_v2_raw_fallback_recovers_qualified_module_names_from_chinese_questions(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for module, expected_path in (("record", "raw/sources/references/n-record.md"), ("search", "raw/sources/references/n-search.md")):
        source = root / expected_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"# N/{module} Module\n\nN/{module} module methods and API reference.", encoding="utf-8")
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


def test_v2_raw_fallback_ranks_strong_raw_answer_above_noisy_wiki_relaxed_match(tmp_path: Path) -> None:
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

    result = run_query_v2(root, "NetSuite 的 Location List/Record 字段 selectrecordtype 数字 ID 是多少？", retrieval_mode="lexical")

    paths = [item["path"] for item in result["results"]]
    assert paths[0] == "raw/sources/references/location.md"
    assert "wiki/concepts/noisy-location.md" in paths
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 1
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] > 0


def test_v2_context_pack_carries_multiple_answer_passages_from_the_leading_page(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    note = "wiki/concepts/netsuite-object-playbooks/typeid-cheatsheet.md"
    _write(
        root,
        note,
        "NetSuite 记录类型内部 ID 速查",
        (
            "# 类型对照\n\n"
            "本页速查 NetSuite 标准记录在自定义 list 字段上的 selectrecordtype 数字。\n\n"
            "## 标准记录 typeId 表\n\n"
            "| 记录类型 | typeId |\n"
            "| --- | --- |\n"
            "| SUBSIDIARY | -117 |\n"
            "| ACCOUNT | -112 |\n"
            "| LOCATION | -103 |\n\n"
            "## SDF 示例\n\n"
            "<selectrecordtype>-117</selectrecordtype>"
        ),
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "subsidiary在自定义list类型字段上的内部id是什么", retrieval_mode="lexical")

    assert result["results"][0]["path"] == note
    assert len(result["context_pack"]["passages"]) == 3
    assert all(item["path"] == note for item in result["context_pack"]["passages"])
    assert any("-117" in item["content"] for item in result["context_pack"]["passages"])
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["fallback"]["level"] == "none"


def test_v2_list_record_field_label_does_not_trigger_qualified_code_priority(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    note = "wiki/concepts/netsuite-object-playbooks/typeid-cheatsheet.md"
    _write(
        root,
        note,
        "Subsidiary List/Record typeId 速查",
        (
            "# List/Record 字段\n\n"
            "自定义 List/Record 字段关联 Subsidiary 标准记录时 selectrecordtype 是 -117。\n\n"
            "## 对照表\n\n"
            "| SUBSIDIARY | -117 |\n"
            "| ACCOUNT | -112 |"
        ),
        type="concept",
    )
    raw = root / "raw" / "sources" / "references" / "list-record-fields.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# List/Record Fields\n\n"
        "List/Record 字段的 selectrecordtype 值说明，N/record 模块方法参考。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "自定义 List/Record 字段关联 Subsidiary 标准记录 typeId 是多少", retrieval_mode="lexical")

    assert result["results"][0]["path"] == note
    assert any(item["path"] == "raw/sources/references/list-record-fields.md" for item in result["results"])
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["fallback"]["level"] == "none"


def test_v2_raw_fallback_combines_identifier_phrase_docs_above_bigram_noise(tmp_path: Path) -> None:
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
    permissions = root / "raw" / "sources" / "references" / "required-features-permissions.md"
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
    combined = "\n".join(item["content"] for item in result["context_pack"]["passages"])
    assert "Server SuiteScript" in combined
    assert "suitetalk.api.netsuite.com" in combined
    assert "MCP Server Connection" in combined
    assert "claude.ai" in combined
    noise_path = noise.relative_to(root).as_posix()
    assert noise_path not in paths
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 3


def test_v2_raw_fallback_returns_only_the_best_matching_passage_per_source_file(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = root / "raw" / "sources" / "manual.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Raw source\n\n" + "unique raw de-duplication sentinel " * 1_000, encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "unique raw de-duplication sentinel", retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == ["raw/sources/manual.md"]
    assert result["pipeline"]["counters"]["raw_fts_hits"] > 1


def test_v2_identifier_phrase_fills_procedural_sections_of_selected_guide_pages(tmp_path: Path) -> None:
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
    passages = result["context_pack"]["passages"]
    combined = "\n".join(item["content"] for item in passages)
    assert "claude.ai" in combined
    assert any("Enable the required features" in (item["heading"] or "") for item in passages)
    assert "Server SuiteScript" in combined
    assert "REST Web Services" in combined


def test_v2_page_ordered_context_keeps_fix_steps_in_later_sections(tmp_path: Path) -> None:
    """A troubleshooting note whose fix steps live after long repro sections
    must keep those steps in the context pack even though BM25 ranks the repro
    paragraphs higher."""

    root = tmp_path / "vault"
    create_wiki_root(root)
    note = "wiki/projects/example/troubleshooting/codegraph-patch.md"
    _write(
        root,
        note,
        "CodeGraph 修复：SuiteScript 解析失效",
        (
            "## 背景\n\n"
            "codegraph 工具更新后本地补丁被覆盖，SuiteScript 脚本的解析失效。\n\n"
            "## 复现\n\n"
            "```javascript\n"
            "define([\"N/search\"], function (search) {\n"
            "  const helper = (x) => x * 2;\n"
            "  return { helper };\n"
            "});\n"
            "```\n\n"
            "## 最终处理方式\n\n"
            "在 tree-sitter.js 的 visitFunctionBody 守卫门补上 variable_declarator "
            "名字回查，然后重新安装 codegraph 并重建索引：\n\n"
            "1. npm i -g @colbymchenry/codegraph\n"
            "2. codegraph index\n"
            "3. codegraph explore 验证函数关系\n"
        ),
        type="troubleshooting",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "codegraph工具更新后，本地修复的对suitescripts脚本的解析就会失效，具体修复步骤是什么",
        retrieval_mode="lexical",
    )

    assert result["results"][0]["path"] == note
    passages = result["context_pack"]["passages"]
    combined = "\n".join(item["content"] for item in passages)
    assert any("最终处理方式" in (item["heading"] or "") for item in passages)
    assert "npm i -g" in combined
    assert "codegraph index" in combined
    assert "codegraph explore" in combined


def test_v2_weak_pages_contribute_only_their_top_hits(tmp_path: Path) -> None:
    """A page that matches many low-scoring OR tokens (a clipping or quiz note)
    must not be filled wholesale; only its top hit passages join the pack."""

    root = tmp_path / "vault"
    create_wiki_root(root)
    strong = "wiki/projects/example/troubleshooting/codegraph-fix.md"
    _write(
        root,
        strong,
        "CodeGraph 修复：SuiteScript 解析",
        (
            "## 问题\n\n"
            "codegraph 工具更新后，本地修复的 SuiteScript 脚本解析补丁失效。"
            "codegraph 对 SuiteScript AMD 模块的函数关系解析全部失效，"
            "codegraph explore 和 codegraph index 无法解析脚本内部调用。\n\n"
            "## 修复步骤\n\n"
            "1. 停掉 codegraph daemon，释放 codegraph 数据库锁\n"
            "2. 修改 tree-sitter.js，补上 SuiteScript 箭头函数名字回查\n"
            "3. 用 codegraph index 全量重建 SuiteScript 索引\n"
            "4. 用 codegraph explore 验证函数关系是否恢复\n"
        ),
        type="troubleshooting",
    )
    weak = "wiki/concepts/gstack-notes.md"
    _write(
        root,
        weak,
        "gstack 工具链深度分析",
        (
            "## 设计哲学\n\n"
            "gstack 强调角色边界与流程驱动，每个角色只做一件事。\n\n"
            "## 角色阵容\n\n"
            "包含 23 个角色化 skill，覆盖产品、设计、开发与发布阶段。\n\n"
            "## 安装步骤\n\n"
            "介绍工具的安装步骤和配置方法。\n\n"
            "## 兼容性\n\n"
            "支持 Claude Code、Cursor 等多个客户端。\n\n"
            "## 参考资料\n\n"
            "更多背景阅读见延伸文档。\n"
        ),
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "codegraph工具更新后，本地修复的对suitescripts脚本的解析就会失效，具体修复步骤是什么",
        retrieval_mode="lexical",
    )

    strong_segs = [p for p in result["context_pack"]["passages"] if "codegraph-fix.md" in p["path"]]
    weak_segs = [p for p in result["context_pack"]["passages"] if "gstack-notes.md" in p["path"]]
    assert len(strong_segs) == 2  # the strongly matched page is filled wholesale
    assert 1 <= len(weak_segs) <= 3  # the weak page keeps only its top hits


def test_v2_freshness_ranks_newer_fix_note_above_older_one(tmp_path: Path) -> None:
    """Among wiki pages with comparable lexical scores, the one updated most
    recently wins the relaxed fallback so the latest fix patch is preferred."""

    root = tmp_path / "vault"
    create_wiki_root(root)
    older = root / "wiki" / "projects" / "example" / "troubleshooting" / "old-patch.md"
    newer = root / "wiki" / "projects" / "example" / "troubleshooting" / "new-patch.md"
    older.parent.mkdir(parents=True, exist_ok=True)
    newer.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "## 问题\n\n"
        "codegraph 工具更新后本地补丁被覆盖，SuiteScript 解析失效。\n\n"
        "## 修复\n\n"
        "重新安装 resolver 并重建索引。\n"
    )
    older.write_text(
        f"---\ntitle: \"旧补丁\"\nupdated_at: \"2026-06-01\"\n---\n\n{body}",
        encoding="utf-8",
    )
    newer.write_text(
        f"---\ntitle: \"新补丁\"\nupdated_at: \"2026-07-22\"\n---\n\n{body}",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "codegraph工具更新后，本地修复的对suitescripts脚本的解析就会失效，具体修复步骤是什么",
        retrieval_mode="lexical",
    )

    paths = [item["path"] for item in result["results"]]
    assert paths.index(str(newer.relative_to(root)).replace("\\", "/")) < paths.index(str(older.relative_to(root)).replace("\\", "/"))


def test_v2_two_phase_pack_keeps_every_selected_page_represented(tmp_path: Path) -> None:
    """A long leading page must not starve later relevant pages entirely out of
    the context pack: every selected page contributes at least its best hit."""

    root = tmp_path / "vault"
    create_wiki_root(root)
    long_page = "wiki/projects/example/troubleshooting/long-guide.md"
    _write(
        root,
        long_page,
        "Long guide",
        (
            "## 背景\n\n"
            + "这是很长的背景段落，包含大量填充词。" * 80
            + "\n\n## 修复步骤\n\n"
            "执行 codegraph index --force 并重启 daemon。\n"
        ),
        type="troubleshooting",
    )
    second = "wiki/projects/example/troubleshooting/second-note.md"
    _write(
        root,
        second,
        "Second note",
        "codegraph 重建索引后，需要验证 SuiteScript 函数关系恢复。",
        type="troubleshooting",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "codegraph工具更新后，本地修复的对suitescripts脚本的解析就会失效，具体修复步骤是什么",
        retrieval_mode="lexical",
        top_k=5,
    )

    pack_paths = [item["path"] for item in result["context_pack"]["passages"]]
    assert long_page in pack_paths
    assert second in pack_paths


def test_v2_relaxes_multilingual_questions_after_strict_fts_returns_no_results(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/map-reduce-limits.md",
        "MapReduce 脚本各阶段点数消耗与用时上限",
        "MapReduce 脚本执行各阶段限制。 MapReduce script phases have governance limits.",
        type="concept",
    )
    _write(root, "wiki/concepts/noisy.md", "Generic script reference", "map reduce script", type="concept")
    refresh_indexes(root)

    for question in ("mr脚本执行各阶段限制。", "map reduce script phases limits"):
        result = run_query_v2(root, question, retrieval_mode="lexical")

        assert result["results"][0]["path"] == "wiki/concepts/map-reduce-limits.md"
        assert result["pipeline"]["fallback"]["level"] == "none"
        assert result["pipeline"]["counters"]["fts_hits"] == 0
        assert result["pipeline"]["counters"]["relaxed_fts_hits"] > 0
        assert result["pipeline"]["lexical"]["mode"] == "relaxed"


def test_v2_wiki_hits_do_not_fall_back_to_raw_fts(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw" / "sources" / "manual.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("invoice approval raw-only evidence", encoding="utf-8")
    _write(root, "wiki/entities/approval.md", "Approval", "invoice approval workflow", type="entity")
    refresh_indexes(root)

    result = run_query_v2(root, "invoice approval", retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == ["wiki/entities/approval.md"]
    assert result["pipeline"]["fallback"]["level"] == "none"
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0


def test_v2_active_scope_excludes_legacy_chatlog_paths(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    legacy = root / "raw" / "sources" / "chat" / "legacy" / "session.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy-only retrieval sentinel", encoding="utf-8")
    refresh_indexes(root)

    legacy_result = run_query_v2(root, "legacy-only retrieval sentinel", scope="all", retrieval_mode="lexical")

    assert legacy_result["results"] == []

    current = root / "raw" / "sources" / "chat" / "2026" / "08" / "03" / "session.md"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("current-chat-only marker", encoding="utf-8")
    refresh_indexes(root)

    current_result = run_query_v2(root, "current-chat-only marker", scope="all", retrieval_mode="lexical")

    assert [item["path"] for item in current_result["results"]] == ["raw/sources/chat/2026/08/03/session.md"]


def test_v2_raw_index_unavailable_yields_structured_warning(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = run_query_v2(root, "unique missing term", retrieval_mode="lexical")

    assert result["ok"] is True
    assert result["code"] == "index_missing"
    assert result["message"] == "The retrieval index is unavailable; the query was not executed."
    assert result["results"] == []
    assert "index_missing" in result["pipeline"]["warnings"]
    assert result["pipeline"]["fallback"]["level"] == "none"


def test_v2_returns_explicit_no_results_without_scanning_source_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/known.md", "Known topic", "A known indexed topic.", type="concept")
    refresh_indexes(root)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source scan")))

    result = run_query_v2(root, "unmatched retrieval sentinel", retrieval_mode="lexical")

    assert result["ok"] is True
    assert result["code"] == "no_results"
    assert result["message"] == "No indexed documentation matched the query."
    assert result["results"] == []


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

    scoped = run_query_v2(root, "unique raw retrieval needle", project="alpha", retrieval_mode="lexical")
    raw_type = run_query_v2(root, "unique raw retrieval needle", filters=QueryFilters(type="raw"), retrieval_mode="lexical")
    other_type = run_query_v2(root, "unique raw retrieval needle", filters=QueryFilters(type="concept"), retrieval_mode="lexical")

    assert [item["path"] for item in scoped["results"]] == ["raw/sources/file/alpha/manual/a.txt"]
    assert len(raw_type["results"]) == 2
    assert other_type["results"] == []


def test_v2_raw_content_never_enters_vector_records(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw" / "sources" / "manual.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw-only vector-leak sentinel", encoding="utf-8")
    refresh_indexes(root)

    records = wiki_query_module.vector_index_records(root)

    assert all("vector-leak" not in record["text"] for record in records)


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

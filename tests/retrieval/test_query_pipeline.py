from pathlib import Path

from retrieval.query_pipeline import run_query_v2
import retrieval.entity_batch as entity_batch_module
from retrieval.entity_batch import AdaptiveCandidateScorePolicy, build_store_specs, select_candidates
from retrieval.query_shared import QueryFilters, probe_hit
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit, RetrievalIndexStore
from retrieval.vector_index import vector_index_records
import retrieval.query_pipeline as query_pipeline_module
from runtime.runtime_config import EmbeddingSettings
from tests.helpers import write_test_page
from wiki.wiki_paths import create_wiki_root
from wiki.wiki_index import refresh_indexes






def _write(root: Path, path: str, title: str, body: str, **frontmatter: object) -> None:
    write_test_page(root, path, {"title": title, "generated": True, **frontmatter}, body)


def test_probe_hit_is_an_empty_eligibility_probe() -> None:
    hit = probe_hit(
        "wiki/concepts/target.md",
        "Target",
        corpus="history",
        authority="project",
        source_kind="raw_chat",
    )

    assert hit == PassageHit(
        "",
        "wiki/concepts/target.md",
        "Target",
        (),
        "",
        0.0,
        "history",
        "project",
        "raw_chat",
    )


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


def test_query_filters_accepts_path_prefix() -> None:
    filters = QueryFilters.from_mapping({"path_prefix": "wiki/concepts/netsuite-script-types/"})
    assert filters.path_prefix == "wiki/concepts/netsuite-script-types/"


def test_query_filters_normalizes_path_prefix_backslashes() -> None:
    filters = QueryFilters.from_mapping({"path_prefix": "wiki\\concepts\\general"})
    assert filters.path_prefix == "wiki/concepts/general"


def test_query_filters_rejects_non_string_path_prefix() -> None:
    try:
        QueryFilters.from_mapping({"path_prefix": 42})
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-string path_prefix")


def test_v2_path_prefix_respects_directory_boundary(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/target.md", "Target", "path boundary marker", type="concept")
    sibling = root / "wiki/concept-extras/sibling.md"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text("---\ntitle: Sibling\ntype: concept\n---\n\npath boundary marker", encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "path boundary marker",
        filters=QueryFilters.from_mapping({"path_prefix": "wiki/concepts"}),
    )

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/target.md"]


def test_v2_returns_result_body_without_context_pack(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "Invoice approval requires a role.", type="concept")
    refresh_indexes(root)
    result = run_query_v2(root, "invoice approval")
    assert result["ok"] is True
    assert result["results"][0]["content"]
    assert "context_pack" not in result
    assert result["pipeline"]["corpus"] == "active"
    assert result["pipeline"]["authority"] == "active:formal>project>raw_chat;fallback:wiki_relaxed>raw"
    assert result["budget"]["used"] == sum(item["tokens"] for item in result["results"])


def test_v2_uses_results_as_the_single_public_context_source(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for index in range(12):
        _write(
            root,
            f"wiki/concepts/invoice-{index:02d}.md",
            f"Invoice {index}",
            "invoice approval requires a role and a matching workflow step.",
            type="concept",
        )
    refresh_indexes(root)

    result = run_query_v2(root, "invoice approval", top_k=2, retrieval_mode="lexical")

    assert "context_pack" not in result
    assert len(result["results"]) == 2
    assert [item["citation"] for item in result["results"]] == ["[1]", "[2]"]
    assert all(item["content"] for item in result["results"])
    assert all("snippet" not in item for item in result["results"])
    assert result["budget"]["used"] > 0
    assert len(result["additional_results"]) > 0
    assert result["additional_results"][0]["citation"] == "[3]"
    assert all("content" not in item for item in result["additional_results"])
    assert result["budget"]["used"] == sum(item["tokens"] for item in result["results"])
    assert "discovery" not in result["pipeline"]
    assert "batch" not in result["pipeline"]


def test_v2_raw_scope_includes_project_raw_pages(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/active.md", "Active", "raw scope sentinel", type="concept")
    raw = root / "raw/sources/references/raw-scope.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("---\ntitle: Raw Scope\n---\n\nraw scope sentinel", encoding="utf-8")
    project_raw = root / "raw/sources/projects/demo/requirements/raw-scope.md"
    project_raw.parent.mkdir(parents=True)
    project_raw.write_text("---\ntitle: Project Raw\n---\n\nraw scope sentinel", encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "raw scope sentinel", scope="raw", retrieval_mode="lexical", top_k=5)

    assert {item["path"] for item in result["results"]} == {
        "raw/sources/references/raw-scope.md",
        "raw/sources/projects/demo/requirements/raw-scope.md",
    }
    assert all(item["source_kind"] == "raw" for item in result["results"])
    assert result["pipeline"]["fallback"] == {
        "level": "none",
        "reasons": [],
        "allowed_source_paths": [],
    }


def test_v2_raw_scope_skips_vector_and_graph_stages(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/references/raw-only.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("---\ntitle: Raw Only\n---\n\nraw-only stage sentinel", encoding="utf-8")
    refresh_indexes(root)

    def fail_vector(*_args, **_kwargs):
        raise AssertionError("raw scope must not load vector provider or index")

    def fail_graph(*_args, **_kwargs):
        raise AssertionError("raw scope must not expand Wiki graph")

    monkeypatch.setattr("retrieval.query_pipeline._vector_hits", fail_vector)
    monkeypatch.setattr(query_pipeline_module, "_graph_expand", fail_graph)

    result = run_query_v2(root, "raw-only stage sentinel", scope="raw", retrieval_mode="hybrid")

    assert result["results"][0]["path"] == "raw/sources/references/raw-only.md"
    assert result["pipeline"]["counters"]["vector_hits"] == 0
    assert result["pipeline"]["counters"]["graph_hits"] == 0


def test_v2_returns_at_most_one_best_passage_per_page(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/invoice.md", "Invoice", "invoice approval " * 1_000, type="concept")
    refresh_indexes(root)

    result = run_query_v2(root, "invoice approval", top_k=5)

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/invoice.md"]
    assert result["results"][0]["content"]


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
    provenance = history_scope["results"][0]["metadata"]["provenance"]
    assert provenance == {
        "session_id": "2026/07/31/review-123",
        "occurred_at": "2026-07-31 08:30:00+00:00",
        "project": "billing",
        "content_hash": provenance["content_hash"],
    }
    assert len(provenance["content_hash"]) == 64




















def test_v2_raw_scope_index_unavailable_uses_structured_index_response(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/references/raw-only.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# Raw Only\n\nraw scope index marker", encoding="utf-8")
    refresh_indexes(root)
    RetrievalIndexStore(root, scope="raw").path.unlink()

    result = run_query_v2(root, "raw scope index marker", scope="raw", retrieval_mode="lexical")

    assert result["ok"] is True
    assert result["code"] == "index_missing"
    assert result["results"] == []
    assert result["pipeline"]["fallback"] == {
        "level": "none",
        "reasons": ["index_unavailable"],
        "allowed_source_paths": [],
    }






def test_entity_batch_owner_uses_passed_snapshot_without_metadata_reload() -> None:
    pages = ({"path": "wiki/entities/auth.md", "frontmatter": {"type": "entity"}, "title": "N/auth"},)
    snapshot = QueryCorpusSnapshot("active", pages, {"wiki/entities/auth.md": {"type": "entity"}}, {})
    store = object()

    specs = build_store_specs(
        store,  # type: ignore[arg-type]
        "knowledge",
        snapshot=snapshot,
        raw_snapshot=None,
        raw_store=None,
    )

    assert specs[0].snapshot is snapshot


def test_v2_excludes_retired_source_namespace_even_when_legacy_files_remain(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    legacy = root / "wiki/sources/capsules/action-examples.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ntype: source_capsule\ngenerated: true\n---\n\n# Action Examples\n\nlegacy retrieval noise", encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "legacy retrieval noise")

    assert result["results"] == []




def test_v2_raw_fallback_creates_one_raw_store_per_query(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.", encoding="utf-8")
    refresh_indexes(root)

    original_init = RetrievalIndexStore.__init__
    raw_store_inits = 0

    def count_raw_store_init(self: RetrievalIndexStore, *args, **kwargs) -> None:
        nonlocal raw_store_inits
        if kwargs.get("scope") == "raw":
            raw_store_inits += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(RetrievalIndexStore, "__init__", count_raw_store_init)
    result = run_query_v2(root, "access reports", retrieval_mode="lexical")

    assert result["results"]
    assert raw_store_inits == 1


def test_v2_wiki_relaxed_recall_does_not_open_raw_store(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/curated.md", "Curated Alpha", "alpha is the curated answer", type="concept")
    raw = root / "raw/sources/file/default/raw.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("missing raw evidence", encoding="utf-8")
    refresh_indexes(root)

    original_status = RetrievalIndexStore.status

    def reject_raw_status(self: RetrievalIndexStore) -> dict[str, object]:
        if self.scope == "raw":
            raise AssertionError("Wiki relaxed recall must finish before raw store access")
        return original_status(self)

    monkeypatch.setattr(RetrievalIndexStore, "status", reject_raw_status)
    result = run_query_v2(root, "alpha missing", retrieval_mode="lexical")

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/curated.md"]
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
    assert result["pipeline"]["fallback"]["level"] == "none"


def test_v2_history_scope_never_opens_raw_fallback_store(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/known.md", "Known", "curated material", type="concept")
    raw = root / "raw/sources/file/default/history-boundary.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("history boundary raw marker", encoding="utf-8")
    refresh_indexes(root)

    original_status = RetrievalIndexStore.status

    def reject_raw_status(self: RetrievalIndexStore) -> dict[str, object]:
        if self.scope == "raw":
            raise AssertionError("history scope must not access raw store")
        return original_status(self)

    monkeypatch.setattr(RetrievalIndexStore, "status", reject_raw_status)
    result = run_query_v2(root, "history boundary raw marker", scope="history", retrieval_mode="lexical")

    assert result["results"] == []
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
    assert result["pipeline"]["fallback"]["level"] == "none"




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


def test_v2_archive_scope_never_opens_raw_fallback_store(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    archive_page = root / "archives/bundles/2026/08/wiki/concepts/old.md"
    archive_page.parent.mkdir(parents=True, exist_ok=True)
    archive_page.write_text("---\ntype: concept\n---\n\narchived-only marker", encoding="utf-8")
    raw = root / "raw/sources/file/default/archive-boundary.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("archive boundary raw marker", encoding="utf-8")
    archive_store = RetrievalIndexStore(root, scope="archive")
    archive_store.build(archive_store.iter_vault_pages())

    original_status = RetrievalIndexStore.status

    def reject_raw_status(self: RetrievalIndexStore) -> dict[str, object]:
        if self.scope == "raw":
            raise AssertionError("archive scope must not access raw store")
        return original_status(self)

    monkeypatch.setattr(RetrievalIndexStore, "status", reject_raw_status)
    result = run_query_v2(root, "archive boundary raw marker", scope="archive", retrieval_mode="lexical")

    assert result["results"] == []
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0
    assert result["pipeline"]["fallback"]["level"] == "none"


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
    debug_item = next(item for item in result["pipeline"]["debug"] if item["path"] == "wiki/concepts/neighbor.md")
    assert debug_item["fts_rank"] is None
    assert debug_item["rrf"] == 0.0
    assert debug_item["graph_reasons"]


def test_v2_graph_expansion_requires_every_requested_tag(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/seed.md", "Seed", "unique needle [[partial.md]]", type="concept", tags=["finance", "approved"])
    _write(root, "wiki/concepts/partial.md", "Partial", "related material", type="concept", tags=["finance"])
    refresh_indexes(root)

    result = run_query_v2(root, "unique needle", filters=QueryFilters(tags=("finance", "approved")), debug=True)

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/seed.md"]












def test_v2_result_body_combines_multiple_answer_passages_from_the_leading_page(tmp_path: Path) -> None:
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
    body = result["results"][0]["content"]
    assert "-117" in body
    assert "-103" in body
    assert "<selectrecordtype>" in body
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["fallback"]["level"] == "none"


def test_v2_wiki_result_does_not_mix_raw_list_record_evidence(tmp_path: Path) -> None:
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
    assert all(not item["path"].startswith("raw/") for item in result["results"])
    assert result["pipeline"]["lexical"]["mode"] == "relaxed"
    assert result["pipeline"]["fallback"]["level"] == "none"
    assert result["pipeline"]["counters"]["raw_fts_hits"] == 0








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
    body = result["results"][0]["content"]
    assert "npm i -g" in body
    assert "codegraph index" in body
    assert "codegraph explore" in body


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

    strong_results = [p for p in result["results"] if "codegraph-fix.md" in p["path"]]
    weak_results = [p for p in result["results"] if "gstack-notes.md" in p["path"]]
    assert len(strong_results) == 1
    assert len(weak_results) == 1
    assert strong_results[0]["content"]
    assert weak_results[0]["content"]


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

    pack_paths = [item["path"] for item in result["results"] if item.get("content")]
    assert long_page in pack_paths
    assert second in pack_paths


def test_v2_top_k_scales_context_budget(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/projects/example/troubleshooting/codegraph-fix.md",
        "CodeGraph 修复：SuiteScript 解析",
        (
            "## 修复步骤\n\n"
            "1. 停掉 codegraph daemon\n"
            "2. 修改 tree-sitter.js\n"
            "3. codegraph index 重建\n"
        ),
        type="troubleshooting",
    )
    refresh_indexes(root)

    question = "如何修复 codegraph 解析失败，具体步骤是什么"
    ten = run_query_v2(root, question, retrieval_mode="lexical", top_k=10)
    twenty = run_query_v2(root, question, retrieval_mode="lexical", top_k=20)

    assert ten["budget"]["total"] == 4_000
    assert twenty["budget"]["total"] == 8_000








def test_v2_adaptive_expand_budget_scales_with_returned_count(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/projects/example/troubleshooting/codegraph-fix.md",
        "CodeGraph 修复：SuiteScript 解析",
        (
            "## 修复步骤\n\n"
            "1. 停掉 codegraph daemon\n"
            "2. 修改 tree-sitter.js\n"
            "3. codegraph index 重建\n"
        ),
        type="troubleshooting",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "如何修复 codegraph 解析失败，具体步骤是什么",
        retrieval_mode="lexical",
        top_k=10,
    )

    # A single-page vault keeps the intent floor (concept=4000): the budget
    # never shrinks below the requested top_k base, and grows with returns.
    assert result["budget"]["total"] == 4_000
    assert result["budget"]["total"] >= 400 * len(result["results"])




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




def test_v2_raw_content_never_enters_vector_records(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw" / "sources" / "manual.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw-only vector-leak sentinel", encoding="utf-8")
    refresh_indexes(root)

    records = vector_index_records(root)

    assert all("vector-leak" not in record.text for record in records)


def test_v2_adds_vector_only_passages_without_scanning_markdown(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/semantic.md", "Semantic", "no lexical overlap", type="concept")
    refresh_indexes(root)

    monkeypatch.setattr(
        "retrieval.query_pipeline._vector_hits",
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
        "retrieval.query_pipeline._vector_hits",
        lambda *_args, **_kwargs: ({vector_passage_id: (1, 0.9)}, []),
    )

    result = run_query_v2(root, "unique lexical term", retrieval_mode="vector")

    assert [item["path"] for item in result["results"]] == ["wiki/concepts/vector.md"]
    assert result["pipeline"]["retrieval_mode"] == "vector"
    assert result["pipeline"]["counters"]["fts_hits"] == 0


def test_v2_lexical_disabled_forces_vector_mode(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/vector.md", "Vector", "semantic meaning", type="concept")
    refresh_indexes(root)
    passage_id = next(iter(RetrievalIndexStore(root).vector_records()))["passage_id"]
    monkeypatch.setattr(
        "retrieval.query_pipeline._vector_hits",
        lambda *_args, **_kwargs: ({passage_id: (1, 0.9)}, []),
    )

    result = run_query_v2(root, "semantic query", lexical_enabled=False, retrieval_mode="hybrid")

    assert result["pipeline"]["retrieval_mode"] == "vector"
    assert result["pipeline"]["lexical_enabled"] is False
    assert result["pipeline"]["counters"]["fts_hits"] == 0


def test_v2_serves_stale_index_with_explicit_warning(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/stale.md", "Stale", "stale index sentinel", type="concept")
    refresh_indexes(root)
    RetrievalIndexStore(root).mark_stale()

    result = run_query_v2(root, "stale index sentinel", retrieval_mode="lexical")

    assert result["results"]
    assert "index_stale" in result["pipeline"]["warnings"]


def test_v2_vector_only_recall_does_not_bypass_filters(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/alpha.md", "Alpha", "no lexical overlap", type="concept", project="alpha", tags=["finance"])
    refresh_indexes(root)
    passage_id = str(next(iter(RetrievalIndexStore(root).vector_records()))["passage_id"])
    monkeypatch.setattr("retrieval.query_pipeline._vector_hits", lambda *_args, **_kwargs: ({passage_id: (1, 0.9)}, []))

    result = run_query_v2(root, "unrelated query", project="beta", filters=QueryFilters(type="concept", tags=("finance",)))
    assert result["results"] == []


def test_v2_lexical_only_ignores_embedding_rrf_k(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/rrf.md", "RRF", "rrf lexical setting", type="concept")
    refresh_indexes(root)

    baseline = run_query_v2(root, "rrf lexical setting", retrieval_mode="lexical")
    configured = run_query_v2(
        root,
        "rrf lexical setting",
        retrieval_mode="lexical",
        embedding=EmbeddingSettings(rrf_k=10),
    )

    assert [item["path"] for item in configured["results"]] == [item["path"] for item in baseline["results"]]
    assert [item["scores"] for item in configured["results"]] == [item["scores"] for item in baseline["results"]]


def test_v2_discovers_structured_qualified_entities_and_batches_in_discovery_order(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/module-catalog.md",
        "Module catalog",
        "# Module catalog\n\n- N/auth Authentication API\n- N/search Search API\n- N/record Record API",
        type="concept",
    )
    for name, title in (("auth", "N/auth"), ("search", "N/search"), ("record", "N/record")):
        _write(root, f"wiki/entities/n-{name}.md", title, f"{title} module API reference", type="entity")
    refresh_indexes(root)

    result = run_query_v2(root, "N/auth N/search N/record", retrieval_mode="lexical", top_k=5)

    discovery = result["pipeline"]["discovery"]
    assert discovery["triggered"] is True
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == ["n/auth", "n/search", "n/record"]
    assert all(item["evidence"]["path"] == "wiki/concepts/module-catalog.md" for item in discovery["candidate_entities"])
    batch = result["pipeline"]["batch"]
    assert batch["status"] == "success"
    assert [item["entity"] for item in batch["entities"]] == ["n/auth", "n/search", "n/record"]
    assert [item["primary"]["path"] for item in batch["entities"]] == [
        "wiki/entities/n-auth.md",
        "wiki/entities/n-search.md",
        "wiki/entities/n-record.md",
    ]
    assert all(item["primary"]["context"] for item in batch["entities"])
    assert all(item["primary"]["path"] != "wiki/concepts/module-catalog.md" for item in batch["entities"])


def test_v2_batch_does_not_recurse_into_a_third_query_stage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/catalog.md", "Catalog", "# Catalog\n\n- N/auth\n- N/search", type="concept")
    _write(root, "wiki/entities/n-auth.md", "N/auth", "N/auth API reference", type="entity")
    _write(root, "wiki/entities/n-search.md", "N/search", "N/search API reference", type="entity")
    refresh_indexes(root)

    original_batch = entity_batch_module.run_entity_batch
    batch_calls = 0
    active = False

    def guarded_batch(*args, **kwargs):
        nonlocal active, batch_calls
        assert active is False, "entity batch must not recursively start another batch"
        active = True
        batch_calls += 1
        try:
            return original_batch(*args, **kwargs)
        finally:
            active = False

    monkeypatch.setattr(entity_batch_module, "run_entity_batch", guarded_batch)
    result = run_query_v2(root, "Catalog", retrieval_mode="lexical")

    assert batch_calls == 1
    assert result["pipeline"]["batch"]["status"] == "success"


def test_v2_does_not_batch_sample_names_without_structured_enumeration(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/sample.md",
        "Sample",
        "Examples mention N/auth and N/search in one paragraph, but this is not a catalog.",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "N/auth N/search", retrieval_mode="lexical")

    assert "discovery" not in result["pipeline"]
    assert "batch" not in result["pipeline"]


def test_v2_treats_same_level_headings_and_table_rows_as_generic_enumeration(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/script-catalog.md",
        "Script catalog",
        "# Script catalog\n\n| Script Type | Entry Point |\n| --- | --- |\n| Client Script | client |\n| User Event Script | user event |",
        type="concept",
    )
    _write(root, "wiki/entities/client-script.md", "Client Script", "Client Script entry points", type="entity")
    _write(root, "wiki/entities/user-event-script.md", "User Event Script", "User Event Script entry points", type="entity")
    refresh_indexes(root)

    result = run_query_v2(root, "Script catalog", retrieval_mode="lexical")

    discovery = result["pipeline"]["discovery"]
    assert discovery["triggered"] is True
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == ["client script", "user event script"]
    assert result["pipeline"]["batch"]["status"] == "success"


def test_adaptive_entity_selection_keeps_high_score_platform_and_drops_score_cliff() -> None:
    items = [{"score": 10.0}, {"score": 9.0}, {"score": 4.0}]
    selected, error = select_candidates(items, AdaptiveCandidateScorePolicy())

    assert error == ""
    assert len(selected) == 2
    assert selected[1]["selection_reason"] == "same_high_score_platform"

    unresolved, error = select_candidates([{"score": 0.01}], AdaptiveCandidateScorePolicy())
    assert unresolved == []
    assert error == "unresolved:below_minimum_relevance"


def test_v2_isolates_partial_and_ambiguous_entities_and_marks_shared_sources(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/catalog.md",
        "Catalog",
        "# Catalog\n\n- N/auth\n- N/search\n- N/error",
        type="concept",
    )
    refresh_indexes(root)

    shared = PassageHit(
        "shared",
        "wiki/entities/shared.md",
        "N/auth N/search shared",
        (),
        "N/auth N/search shared API",
        1.5,
        "active",
        "high",
        "entity",
    )

    def fake_qualified_search(self, identifier, **_kwargs):
        del self
        if identifier.canonical_id == "n/error":
            raise RuntimeError("synthetic entity failure")
        if identifier.canonical_id == "n/search":
            return [shared]
        return [
            PassageHit("auth-a", "wiki/entities/n-auth-a.md", "N/auth guide A", (), "N/auth API", 1.0, "active", "high", "entity"),
            PassageHit("auth-b", "wiki/entities/n-auth-b.md", "N/auth guide B", (), "N/auth API", 1.0, "active", "high", "entity"),
            shared,
        ]

    monkeypatch.setattr(RetrievalIndexStore, "search_qualified_identifier", fake_qualified_search)
    result = run_query_v2(root, "Catalog", retrieval_mode="lexical")

    batch = result["pipeline"]["batch"]
    by_entity = {item["entity"]: item for item in batch["entities"]}
    assert batch["status"] == "partial_success"
    assert by_entity["n/auth"]["status"] == "ambiguous"
    assert by_entity["n/auth"]["needs_review"] is True
    assert by_entity["n/auth"]["shared_source"] is True
    assert by_entity["n/search"]["shared_source"] is True
    assert by_entity["n/error"]["status"] == "error"
    assert batch["failed_entities"] == ["n/error"]


def test_v2_requires_confirmation_before_querying_more_than_40_entities(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    lines = [f"- N/mod{index} module" for index in range(41)]
    _write(root, "wiki/concepts/catalog.md", "Catalog", "# Catalog\n\n" + "\n".join(lines), type="concept")
    refresh_indexes(root)

    called = False

    def fail_batch_search(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("batch search must wait for confirmation")

    monkeypatch.setattr(RetrievalIndexStore, "search_qualified_identifier", fail_batch_search)
    result = run_query_v2(root, "Catalog", retrieval_mode="lexical")

    batch = result["pipeline"]["batch"]
    assert batch["status"] == "confirmation_required"
    assert batch["entity_count"] == 41
    assert batch["max_batch_items"] == 40
    assert batch["confirmation_token"]
    assert called is False


def test_v2_raw_batch_keeps_24_short_entity_pages_out_of_a_160_plus_sample_pool(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    entity_names = [f"N/mod{index}" for index in range(24)]
    catalog = root / "raw/sources/references/catalog.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# Catalog\n\n" + "\n".join(f"- {name} module reference" for name in entity_names),
        encoding="utf-8",
    )
    for index, name in enumerate(entity_names):
        page = root / f"raw/sources/references/short-{index}.md"
        page.write_text(f"# {name}\n\n{name} API reference", encoding="utf-8")
    sample_body = " ".join(entity_names) + " sample implementation details"
    for index in range(170):
        page = root / f"raw/sources/samples/sample-{index:03d}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"# Sample {index}\n\n{sample_body}", encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "Catalog", scope="raw", retrieval_mode="lexical", top_k=5)

    batch = result["pipeline"]["batch"]
    assert batch["status"] == "success"
    assert len(batch["entities"]) == 24
    assert all(
        item["primary"]["path"] == f"raw/sources/references/short-{index}.md"
        for index, item in enumerate(batch["entities"])
    )
    assert batch["counters"]["raw_hits"] > 160


def test_v2_raw_scope_uses_qualified_aliases_on_the_primary_path(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    source = root / "raw/sources/references/n-auth.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# N/auth\n\nN/auth API reference", encoding="utf-8")
    refresh_indexes(root)

    for form in ("N/auth", "N auth", "Nauth", "N-auth", "N_auth", "nauth"):
        result = run_query_v2(root, form, scope="raw", retrieval_mode="lexical")
        assert [item["path"] for item in result["results"]] == ["raw/sources/references/n-auth.md"]
        assert result["pipeline"]["lexical"]["mode"] == "qualified_code"


def test_v2_namespace_wildcard_discovers_raw_catalog_before_entity_batch(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/module-catalog.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript Module Catalog\n\n"
        "- N/auth Authentication module\n"
        "- N/search Search module\n"
        "- N/record Record module\n",
        encoding="utf-8",
    )
    for name in ("auth", "search", "record"):
        page = root / f"raw/sources/references/n-{name}.md"
        page.write_text(f"# N/{name}\n\nN/{name} API reference", encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "列出 N/* modules", scope="raw", retrieval_mode="lexical")

    discovery = result["pipeline"]["discovery"]
    assert discovery["requested"] is True
    assert discovery["source_pages"] == ["raw/sources/references/module-catalog.md"]
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == [
        "n/auth",
        "n/search",
        "n/record",
    ]
    batch = result["pipeline"]["batch"]
    assert batch["status"] == "success"
    assert [item["entity"] for item in batch["entities"]] == ["n/auth", "n/search", "n/record"]
    assert [item["primary"]["path"] for item in batch["entities"]] == [
        "raw/sources/references/n-auth.md",
        "raw/sources/references/n-search.md",
        "raw/sources/references/n-record.md",
    ]


def test_v2_namespace_wildcard_discovers_nested_raw_module_paths(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/module-catalog.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript Module Catalog\n\n"
        "| Module | Description |\n"
        "| --- | --- |\n"
        "| N/crypto/certificate | Certificate module |\n"
        "| N/crypto/random | Random module |\n"
        "| N/ui/serverWidget | Server widget module |\n",
        encoding="utf-8",
    )
    for module_id, filename in (
        ("N/crypto/certificate", "n-crypto-certificate.md"),
        ("N/crypto/random", "n-crypto-random.md"),
        ("N/ui/serverWidget", "n-ui-serverwidget.md"),
    ):
        (catalog.parent / filename).write_text(
            f"# {module_id}\n\n{module_id} API reference", encoding="utf-8"
        )
    refresh_indexes(root)

    result = run_query_v2(root, "列出 N/* modules", scope="raw", retrieval_mode="lexical")

    discovery = result["pipeline"]["discovery"]
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == [
        "n/crypto/certificate",
        "n/crypto/random",
        "n/ui/serverwidget",
    ]
    assert [item["entity"] for item in result["pipeline"]["batch"]["entities"]] == [
        "n/crypto/certificate",
        "n/crypto/random",
        "n/ui/serverwidget",
    ]
    assert [item["primary"]["path"] for item in result["pipeline"]["batch"]["entities"]] == [
        "raw/sources/references/n-crypto-certificate.md",
        "raw/sources/references/n-crypto-random.md",
        "raw/sources/references/n-ui-serverwidget.md",
    ]


def test_v2_discovery_only_is_distinct_from_no_results(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/module-catalog.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript Module Catalog\n\n"
        "- N/auth Authentication module\n"
        "- N/search Search module\n",
        encoding="utf-8",
    )
    for name in ("auth", "search"):
        (catalog.parent / f"n-{name}.md").write_text(
            f"# N/{name}\n\nN/{name} API reference", encoding="utf-8"
        )
    refresh_indexes(root)

    result = run_query_v2(root, "列出 N/*", scope="raw", retrieval_mode="lexical")

    assert result["results"] == []
    assert result["code"] == "discovery_only"
    assert "pipeline.discovery" in result["message"]


def test_v2_namespace_wildcard_filters_structured_noise_and_reads_flattened_tables(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/module-catalog.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript Module Catalog\n\n"
        "| Module | Description |\n"
        "| --- | --- |\n"
        "| N/auth | Authentication module |\n"
        "| N/search | Search module |\n",
        encoding="utf-8",
    )
    noise = root / "raw/sources/references/record-catalog.md"
    noise.write_text(
        "# Record Catalog\n\n"
        "| Record | Description |\n"
        "| --- | --- |\n"
        "| Customer | Customer record |\n"
        "| Vendor | Vendor record |\n",
        encoding="utf-8",
    )
    for name in ("auth", "search"):
        (catalog.parent / f"N{name}.md").write_text(
            f"# N/{name}\n\nN/{name} API reference", encoding="utf-8"
        )
    refresh_indexes(root)

    result = run_query_v2(root, "列出 N/* modules", scope="raw", retrieval_mode="lexical")

    discovery = result["pipeline"]["discovery"]
    assert discovery["source_pages"] == ["raw/sources/references/module-catalog.md"]
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == [
        "n/auth",
        "n/search",
    ]
    assert [item["entity"] for item in result["pipeline"]["batch"]["entities"]] == [
        "n/auth",
        "n/search",
    ]


def test_v2_script_type_listing_reads_flattened_table_and_ignores_sample_noise(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/SuiteScript 2.1 Script Types.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript 2.1 Script Types\n\n"
        "| Script Type | Description |\n"
        "| --- | --- |\n"
        "| Client Script | Runs in the browser |\n"
        "| User Event Script | Runs on record events |\n"
        "| Scheduled Script | Runs on a schedule |\n\n"
        "- Do not hard-code passwords in scripts.\n"
        "- Use built in functions for reading/writing Date/Currency fields.\n",
        encoding="utf-8",
    )
    noise = root / "raw/sources/references/SuiteScript Samples Catalog.md"
    noise.write_text(
        "# SuiteScript Samples Catalog\n\n"
        "- Example One\n"
        "- Example Two\n",
        encoding="utf-8",
    )
    for name, body in (
        (
            "Client Script Type",
            "# Client Script Type\n\n- pageInit\n- saveRecord\n- validateField\n",
        ),
        (
            "Scheduled Script Type",
            "# Scheduled Script Type\n\n- execute\n- governance\n- deployment\n",
        ),
    ):
        (catalog.parent / f"{name}.md").write_text(body, encoding="utf-8")
    refresh_indexes(root)

    result = run_query_v2(root, "查询SuiteScript脚本类型相关内容", scope="raw", retrieval_mode="lexical")

    discovery = result["pipeline"]["discovery"]
    assert discovery["source_pages"] == ["raw/sources/references/SuiteScript 2.1 Script Types.md"]
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == [
        "client script",
        "user event script",
        "scheduled script",
    ]
    assert [item["entity"] for item in result["pipeline"]["batch"]["entities"]] == [
        "client script",
        "user event script",
        "scheduled script",
    ]


def test_v2_discovery_keeps_title_case_record_names_as_generic_entities(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/SuiteScript 2.1 Record Types.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript 2.1 Record Types\n\n"
        "| Record Type | Description |\n"
        "| --- | --- |\n"
        "| Customer | Customer record |\n"
        "| Vendor | Vendor record |\n"
        "| Object | Object record |\n"
        "| Method | Method record |\n",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "record type list", scope="raw", retrieval_mode="lexical")

    discovery = result["pipeline"]["discovery"]
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == [
        "customer",
        "vendor",
        "object",
        "method",
    ]


def test_v2_english_listing_query_finds_module_catalog_and_batches_entities(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/SuiteScript 2.1 Modules.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript 2.1 Modules\n\n"
        "SuiteScript 2.1 APIs are organized into various modules based on behavior.\n\n"
        "- N/auth Authentication module\n"
        "- N/search Search module\n"
        "- N/record Record module\n",
        encoding="utf-8",
    )
    for name in ("auth", "search", "record"):
        (catalog.parent / f"N{name}.md").write_text(
            f"# N/{name}\n\nN/{name} API reference", encoding="utf-8"
        )
    (catalog.parent / "suitelet-faq.md").write_text(
        "# Scriptable Cart FAQ\n\nSuiteScript 2.1 scriptable cart FAQ", encoding="utf-8"
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "NetSuite SuiteScript 2.1 standard N modules overview list API reference",
        scope="raw",
        retrieval_mode="lexical",
        top_k=20,
    )

    discovery = result["pipeline"]["discovery"]
    assert discovery["source_pages"] == ["raw/sources/references/SuiteScript 2.1 Modules.md"]
    assert [item["canonical_id"] for item in discovery["candidate_entities"]] == [
        "n/auth",
        "n/search",
        "n/record",
    ]
    assert [item["entity"] for item in result["pipeline"]["batch"]["entities"]] == [
        "n/auth",
        "n/search",
        "n/record",
    ]


def test_v2_discovery_prioritizes_catalog_metadata_after_fts_pool_fills_with_noise(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog = root / "raw/sources/references/SuiteScript 2.1 Modules.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "# SuiteScript 2.1 Modules\n\n"
        "- N/auth Authentication module\n"
        "- N/search Search module\n",
        encoding="utf-8",
    )
    for name in ("auth", "search"):
        (catalog.parent / f"N{name}.md").write_text(
            f"# N/{name}\n\nN/{name} API reference", encoding="utf-8"
        )
    for index in range(48):
        noise = root / f"raw/sources/noise/generic-{index:03d}.md"
        noise.parent.mkdir(parents=True, exist_ok=True)
        noise.write_text(
            "# NetSuite Product Notes\n\n"
            "NetSuite SuiteScript 2.1 standard API reference and overview "
            "list for generic product notes. " * 8,
            encoding="utf-8",
        )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "NetSuite SuiteScript 2.1 standard N modules overview list API reference",
        scope="raw",
        retrieval_mode="lexical",
        top_k=20,
    )

    assert result["pipeline"]["discovery"]["source_pages"] == [
        "raw/sources/references/SuiteScript 2.1 Modules.md"
    ]
    assert [item["entity"] for item in result["pipeline"]["batch"]["entities"]] == [
        "n/auth",
        "n/search",
    ]


def test_v2_original_chinese_raw_listing_query_finds_catalog_and_batches_entities(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    catalog_dir = root / "raw/sources/references/05_SS 2.x API Reference/03_SS 2.1 Modules"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / "SuiteScript 2.1 Modules.md").write_text(
        "# SuiteScript 2.1 Modules\n\n"
        "- N/auth Authentication module\n"
        "- N/search Search module\n",
        encoding="utf-8",
    )
    (catalog_dir / "NetSuite overview.md").write_text(
        "# NetSuite Overview\n\nNetSuite standard SuiteScript examples", encoding="utf-8"
    )
    for name in ("auth", "search"):
        (catalog_dir / f"N{name}.md").write_text(
            f"# N/{name}\n\nN/{name} API reference", encoding="utf-8"
        )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "查询raw文档，整理NetSuite标准 N/* 平台模块内容",
        scope="raw",
        retrieval_mode="lexical",
        top_k=20,
    )

    assert result["pipeline"]["discovery"]["source_pages"] == [
        "raw/sources/references/05_SS 2.x API Reference/03_SS 2.1 Modules/SuiteScript 2.1 Modules.md"
    ]
    assert [item["entity"] for item in result["pipeline"]["batch"]["entities"]] == [
        "n/auth",
        "n/search",
    ]


def test_v2_mixed_language_wildcard_ignores_generic_netsuite_passage_for_discovery(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/netsuite-generic.md",
        "NetSuite Standard Reference",
        "NetSuite standard modules and examples without an entity enumeration.",
        type="concept",
    )
    _write(
        root,
        "wiki/concepts/suitescript-module-catalog.md",
        "SuiteScript Module Catalog",
        "# SuiteScript Module Catalog\n\n"
        "- N/auth Authentication API\n"
        "- N/search Search API",
        type="concept",
    )
    _write(root, "wiki/entities/n-auth.md", "N/auth", "N/auth API reference", type="entity")
    _write(root, "wiki/entities/n-search.md", "N/search", "N/search API reference", type="entity")
    refresh_indexes(root)

    result = run_query_v2(root, "NetSuite 中有哪些 N/* modules？", retrieval_mode="lexical")

    assert [item["canonical_id"] for item in result["pipeline"]["discovery"]["candidate_entities"]] == [
        "n/auth",
        "n/search",
    ]
    assert result["pipeline"]["batch"]["status"] == "success"
    assert result["expansion_suggestions"] == []


def test_v2_path_prefix_filter_restricts_results(tmp_path: Path) -> None:
    """path_prefix filter should restrict results to the specified directory."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(root, "wiki/concepts/domain-a/page.md", "Page A", "invoice approval workflow", type="concept")
    _write(root, "wiki/concepts/domain-b/page.md", "Page B", "invoice approval process", type="concept")
    refresh_indexes(root)

    result = run_query_v2(
        root, "invoice approval", scope="knowledge",
        filters=QueryFilters(path_prefix="wiki/concepts/domain-a/"),
    )
    paths = {item["path"] for item in result["results"]}
    assert paths == {"wiki/concepts/domain-a/page.md"}

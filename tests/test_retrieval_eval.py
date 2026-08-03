from __future__ import annotations

import json
import math
import os
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.retrieval_eval import (
    Relevance,
    RetrievalEvalCase,
    RetrievalEvalDataset,
    RetrievalEvalError,
    RetrievalEvalManifest,
    calculate_ranking_metrics,
    load_retrieval_dataset,
    percentile_95,
    run_retrieval_evaluation,
    validate_dataset_paths,
    write_retrieval_eval_report,
    _results_match_filters,
)
from netsuite_llm_wiki_mcp.vector_index import VectorIndexStore
from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore
from netsuite_llm_wiki_mcp.archive_service import ArchiveService
from netsuite_llm_wiki_mcp.knowledge_compiler import filesystem_path
from netsuite_llm_wiki_mcp.vector_provider import DeterministicFakeProvider
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
import netsuite_llm_wiki_mcp.wiki_query as wiki_query_module


_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "retrieval"
_V2_40_FIXTURE_ROOT = _FIXTURE_ROOT / "v2_40"


def _copy_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(_FIXTURE_ROOT / "vault", vault)
    return vault


def _copy_dataset(tmp_path: Path) -> Path:
    dataset = tmp_path / "fixture.jsonl"
    shutil.copy2(_FIXTURE_ROOT / "fixture.jsonl", dataset)
    shutil.copy2(_FIXTURE_ROOT / "fixture.manifest.json", tmp_path / "fixture.manifest.json")
    return dataset


def _build_passage_store(vault: Path) -> None:
    store = RetrievalIndexStore(vault)
    store.build(store.iter_vault_pages())


def _copy_v2_40_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Copy the historical source-only fixture so each evaluation stays isolated."""
    vault = tmp_path / "v2-40-vault"
    shutil.copytree(_V2_40_FIXTURE_ROOT / "vault", vault)
    dataset = tmp_path / "v2-40-cases.jsonl"
    manifest = tmp_path / "v2-40-cases.manifest.json"
    shutil.copy2(_V2_40_FIXTURE_ROOT / "cases.jsonl", dataset)
    shutil.copy2(_V2_40_FIXTURE_ROOT / "cases.manifest.json", manifest)
    return vault, dataset, manifest


def test_v2_report_fields_mark_missing_frozen_comparison_unproven(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    RetrievalIndexStore(vault).build(RetrievalIndexStore(vault).iter_vault_pages())
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    report = run_retrieval_evaluation(vault, dataset, measure_context_budget=True, context_budget_case_limit=1)
    assert report["metadata"]["query_v2"]["comparison_status"] == "unproven_without_frozen_v2_baseline"
    assert report["metadata"]["query_v2"]["cold_start_latency_ms"] is None
    assert "warm_p95_latency_ms" in report["metrics"]
    assert "fallback_reason_distribution" in report["metrics"]


def test_v2_evaluator_reads_existing_passage_store_without_telemetry_write(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    create_wiki_root(vault)
    page = Path("wiki/concepts/invoice.md")
    write_wiki_page(vault, WikiPage(page, {"title": "Invoice", "generated": True, "type": "concept"}, "Invoice", "invoice approval workflow"), overwrite_generated_only=False)
    RetrievalIndexStore(vault).build(RetrievalIndexStore(vault).iter_vault_pages())
    dataset = RetrievalEvalDataset(RetrievalEvalManifest("v2", "1", 0.0), (RetrievalEvalCase("invoice", "invoice approval", (Relevance(page.as_posix(), 3),), {}, True, "en", (), ""),))
    report = run_retrieval_evaluation(vault, dataset, measure_context_budget=False)
    assert report["metadata"]["parameters"]["query_version"] == "v2"
    assert report["metrics"]["recall_at_k_macro"] == 1.0
    assert not (vault / ".llm-wiki" / "state.sqlite3").exists()


def test_reviewed_v2_forty_case_fixture_is_archive_only(tmp_path: Path) -> None:
    """Keep the historical label set out of active retrieval and test it only in archive scope."""
    vault, dataset_path, manifest_path = _copy_v2_40_fixture(tmp_path)
    dataset = load_retrieval_dataset(dataset_path, manifest_path)

    assert len(dataset.cases) == 40
    assert sum(case.answerable for case in dataset.cases) == 36
    assert sum(not case.answerable for case in dataset.cases) == 4
    source_count = len(list((vault / "wiki" / "sources" / "capsules").glob("*.md")))
    assert source_count == 36
    assert not (vault / "raw").exists()
    _build_passage_store(vault)
    assert RetrievalIndexStore(vault).page_candidates() == []

    service = ArchiveService(vault, actor="test-archive")
    archive_plan = service.plan_archive(
        [path.relative_to(vault).as_posix() for path in (vault / "wiki" / "sources").rglob("*.md")],
        reason="retention",
        force_namespace="wiki/sources",
        restorable=False,
    )
    archive = service.apply(archive_plan["plan_id"])
    assert archive["ok"] is True
    prefix = f"archives/bundles/{archive['archive_id'][:4]}/{archive['archive_id'][4:6]}/{archive['archive_id']}/"
    dataset = replace(
        dataset,
        cases=tuple(
            replace(case, relevant=tuple(replace(item, path=prefix + item.path) for item in case.relevant))
            for case in dataset.cases
        ),
    )
    validate_dataset_paths(dataset, vault)

    report = run_retrieval_evaluation(
        vault,
        dataset,
        query_version="v2",
        retrieval_mode="lexical",
        measure_context_budget=False,
        scope="archive",
    )

    assert report["metadata"]["parameters"]["query_version"] == "v2"
    assert report["metadata"]["parameters"]["retrieval_mode"] == "lexical"
    assert report["metadata"]["parameters"]["scope"] == "archive"
    assert report["metrics"]["relevant_total"] == 36
    assert report["metrics"]["no_answer_cases"] == 4
    assert report["metrics"]["latency_sample_count"] == 40
    assert all(len(case["ranking_runs"]) == 1 for case in report["cases"])



def test_v2_vector_evaluation_uses_the_requested_vault_relative_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    create_wiki_root(vault)
    path = Path("wiki/concepts/semantic.md")
    write_wiki_page(vault, WikiPage(path, {"title": "Semantic", "generated": True, "type": "concept"}, "Semantic", "accounts payable operations"), overwrite_generated_only=False)
    RetrievalIndexStore(vault).build(RetrievalIndexStore(vault).iter_vault_pages())
    provider = DeterministicFakeProvider({"expense automation": [1, 0, 0, 0], "accounts payable operations": [1, 0, 0, 0]})
    index_path = vault / ".llm-wiki" / "v2-eval"
    VectorIndexStore(vault, index_path).build(wiki_query_module.vector_index_records(vault), provider, include_raw_sources=False)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.query_pipeline.LocalBgeM3Provider", lambda *_args, **_kwargs: provider)
    dataset = RetrievalEvalDataset(RetrievalEvalManifest("v2-vector", "1", 0.5), (RetrievalEvalCase("semantic", "expense automation", (Relevance(path.as_posix(), 3),), {}, True, "en", (), ""),))

    report = run_retrieval_evaluation(
        vault,
        dataset,
        measure_context_budget=False,
        retrieval_mode="vector",
        query_version="v2",
        vector_config={"model_path": str(tmp_path / "model"), "index_path": str(index_path.relative_to(vault))},
    )

    assert report["metrics"]["recall_at_k_macro"] == 1.0


def test_vector_and_hybrid_evaluation_improve_zero_lexical_recall_without_metric_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    create_wiki_root(vault)
    path = Path("wiki/concepts/semantic.md")
    write_wiki_page(
        vault,
        WikiPage(path, {"title": "Semantic result", "generated": True, "type": "concept"}, "Semantic result", "accounts payable operations"),
        overwrite_generated_only=False,
    )
    model = tmp_path / "local-bge-m3"
    model.mkdir()
    provider = DeterministicFakeProvider(
        {
            "expense automation": [1, 0, 0, 0],
            "accounts payable operations": [1, 0, 0, 0],
        }
    )
    _build_passage_store(vault)
    store = VectorIndexStore(vault)
    store.build(wiki_query_module.vector_index_records(vault), provider, include_raw_sources=False)
    monkeypatch.setattr("netsuite_llm_wiki_mcp.query_pipeline.LocalBgeM3Provider", lambda *args, **kwargs: provider)
    dataset = RetrievalEvalDataset(
        RetrievalEvalManifest("vector-ablation", "1", 0.5),
        (
            RetrievalEvalCase(
                "semantic-only",
                "expense automation",
                (Relevance(path.as_posix(), 3),),
                {},
                True,
                "en",
                (),
                "semantic recall fixture",
            ),
        ),
    )
    config = {"provider": "local_bge_m3", "model_path": str(model)}

    lexical = run_retrieval_evaluation(vault, dataset, top_k=10, measure_context_budget=False, retrieval_mode="lexical", query_version="v2")
    vector = run_retrieval_evaluation(vault, dataset, top_k=10, measure_context_budget=False, retrieval_mode="vector", vector_config=config, query_version="v2")
    hybrid = run_retrieval_evaluation(vault, dataset, top_k=10, measure_context_budget=False, retrieval_mode="hybrid", vector_config=config, query_version="v2")

    assert lexical["metrics"]["recall_at_k_macro"] == 0.0
    assert vector["metrics"]["recall_at_k_macro"] == 1.0
    assert hybrid["metrics"]["recall_at_k_macro"] > lexical["metrics"]["recall_at_k_macro"]
    assert hybrid["metrics"]["mrr_at_k_macro"] >= lexical["metrics"]["mrr_at_k_macro"]
    assert hybrid["metrics"]["ndcg_at_k_macro"] >= lexical["metrics"]["ndcg_at_k_macro"]


def test_ranking_metrics_match_hand_calculation() -> None:
    metrics = calculate_ranking_metrics(
        ["not-relevant", "grade-one", "grade-three"],
        [Relevance("grade-three", 3), Relevance("grade-one", 1)],
        top_k=3,
    )

    expected_dcg = 1 / math.log2(3) + 7 / math.log2(4)
    expected_ideal = 7 + 1 / math.log2(3)
    assert metrics == {
        "recall": 1.0,
        "mrr": 0.5,
        "ndcg": pytest.approx(expected_dcg / expected_ideal),
        "hits": 2,
        "relevant_total": 2,
    }
    assert percentile_95([1.0, 2.0, 3.0, 4.0, 5.0]) == 5.0


def test_ranking_metrics_deduplicate_repeated_document_paths() -> None:
    relevant = [Relevance("wiki/concepts/invoice.md", 3)]

    metrics = calculate_ranking_metrics(
        ["wiki/concepts/invoice.md", "wiki/concepts/invoice.md"],
        relevant,
    )

    assert metrics == {"recall": 1.0, "mrr": 1.0, "ndcg": 1.0, "hits": 1, "relevant_total": 1}


def test_filter_evaluation_requires_every_requested_tag() -> None:
    results = [{"path": "wiki/concepts/invoice.md", "metadata": {"tags": ["finance"]}}]

    assert _results_match_filters(results, {"filter_tags": ["finance"]})
    assert not _results_match_filters(results, {"filter_tags": ["finance", "approval"]})


def test_loader_rejects_invalid_grade_duplicate_id_and_filters(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    rows = dataset.read_text(encoding="utf-8").splitlines()
    invalid_grade = json.loads(rows[0])
    invalid_grade["relevant"][0]["grade"] = 4
    dataset.write_text(json.dumps(invalid_grade) + "\n", encoding="utf-8")
    with pytest.raises(RetrievalEvalError, match="integer from 1 to 3"):
        load_retrieval_dataset(dataset)

    duplicate = json.loads(rows[0])
    dataset.write_text("\n".join([rows[0], json.dumps(duplicate)]) + "\n", encoding="utf-8")
    with pytest.raises(RetrievalEvalError, match="duplicate case id"):
        load_retrieval_dataset(dataset)

    invalid_filters = json.loads(rows[0])
    invalid_filters["filters"] = {"unsupported": "value"}
    dataset.write_text(json.dumps(invalid_filters) + "\n", encoding="utf-8")
    with pytest.raises(RetrievalEvalError, match="unsupported filters"):
        load_retrieval_dataset(dataset)


def test_path_validation_rejects_missing_relevant_page(tmp_path: Path) -> None:
    dataset_path = _copy_dataset(tmp_path)
    rows = [json.loads(row) for row in dataset_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["relevant"][0]["path"] = "wiki/concepts/missing.md"
    dataset_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    with pytest.raises(RetrievalEvalError, match="missing path"):
        validate_dataset_paths(load_retrieval_dataset(dataset_path), _copy_vault(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths are platform-specific")
def test_path_validation_accepts_existing_long_relevant_page(tmp_path: Path) -> None:
    relative = Path("raw/sources/references")
    for index in range(9):
        relative /= f"deep-capsule-provenance-segment-{index:02d}-with-descriptive-name"
    relative /= "source.md"
    target = filesystem_path(tmp_path / relative)
    target.parent.mkdir(parents=True)
    target.write_text("# capsule\n", encoding="utf-8")
    assert len(str(tmp_path / relative)) > 260
    dataset = RetrievalEvalDataset(
        RetrievalEvalManifest("long-path", "1", 0.5),
        (RetrievalEvalCase("long-path", "capsule", (Relevance(relative.as_posix(), 3),), {}, True, "en", (), ""),),
    )

    validate_dataset_paths(dataset, tmp_path)


def test_fixture_evaluation_is_deterministic_and_reports_all_required_metrics(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    _build_passage_store(vault)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))

    report = run_retrieval_evaluation(vault, dataset, repeats=2, query_version="v2")

    assert report["metadata"]["parameters"]["top_k"] == 10
    assert report["metadata"]["vault_fingerprint"]["file_count"] == 4
    assert report["metrics"]["recall_at_k_macro"] == 0.8
    assert report["metrics"]["mrr_at_k_macro"] == 0.8
    assert report["metrics"]["ndcg_at_k_macro"] == 0.8
    assert report["metrics"]["no_answer_false_positive_rate"] == 0.0
    assert report["metrics"]["filter_correctness"] == 1.0
    assert report["metrics"]["latency_sample_count"] == 12
    assert report["metrics"]["context_budget"]["within_budget"] is True
    assert all(case["ranking_runs"][0] == case["ranking_runs"][1] for case in report["cases"])
    assert not (vault / ".llm-wiki" / "state.sqlite3").exists()

    output = write_retrieval_eval_report(report, tmp_path / "reports")
    json_report = Path(output["json"]).read_text(encoding="utf-8")
    markdown_report = Path(output["markdown"]).read_text(encoding="utf-8")
    assert str(vault.resolve()) not in json_report
    assert "Recall@10" in markdown_report
    assert "Context budget：通过" in markdown_report

    legacy_report = deepcopy(report)
    del legacy_report["metadata"]["ranking"]
    legacy_output = write_retrieval_eval_report(legacy_report, tmp_path / "legacy-reports")
    assert "legacy-unversioned" in Path(legacy_output["markdown"]).read_text(encoding="utf-8")


def test_no_answer_without_results_is_not_a_false_positive_at_zero_threshold(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    _build_passage_store(vault)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    zero_threshold_dataset = replace(dataset, manifest=replace(dataset.manifest, abstention_threshold=0.0))

    report = run_retrieval_evaluation(vault, zero_threshold_dataset, measure_context_budget=False, query_version="v2")

    no_answer = next(case for case in report["cases"] if case["id"] == "no-answer")
    assert no_answer["ranked_paths"] == []
    assert no_answer["no_answer_false_positive"] is False


def test_context_budget_case_limit_samples_the_requested_prefix(tmp_path: Path) -> None:
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    vault = _copy_vault(tmp_path)
    _build_passage_store(vault)

    report = run_retrieval_evaluation(vault, dataset, context_budget_case_limit=1, query_version="v2")

    assert report["metadata"]["parameters"]["context_budget_case_limit"] == 1
    assert report["metrics"]["context_budget"] == {
        "measured_cases": 1,
        "case_limit": 1,
        "violations": [],
        "within_budget": True,
    }
    with pytest.raises(RetrievalEvalError, match="must be non-negative"):
        run_retrieval_evaluation(vault, dataset, context_budget_case_limit=-1, query_version="v2")

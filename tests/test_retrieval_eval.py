from __future__ import annotations

import json
import math
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
    compare_retrieval_reports,
    load_retrieval_dataset,
    percentile_95,
    run_retrieval_evaluation,
    validate_dataset_paths,
    write_retrieval_eval_report,
    write_retrieval_comparison,
)
from netsuite_llm_wiki_mcp.vector_index import VectorIndexStore
from netsuite_llm_wiki_mcp.vector_provider import DeterministicFakeProvider
from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root
import netsuite_llm_wiki_mcp.wiki_query as wiki_query_module


_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "retrieval"


def _copy_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(_FIXTURE_ROOT / "vault", vault)
    return vault


def _copy_dataset(tmp_path: Path) -> Path:
    dataset = tmp_path / "fixture.jsonl"
    shutil.copy2(_FIXTURE_ROOT / "fixture.jsonl", dataset)
    shutil.copy2(_FIXTURE_ROOT / "fixture.manifest.json", tmp_path / "fixture.manifest.json")
    return dataset


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
    store = VectorIndexStore(vault)
    store.build(wiki_query_module.vector_index_records(vault), provider, include_raw_sources=False)
    monkeypatch.setattr(wiki_query_module, "LocalBgeM3Provider", lambda *args, **kwargs: provider)
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

    lexical = run_retrieval_evaluation(vault, dataset, top_k=10, measure_context_budget=False, retrieval_mode="lexical")
    vector = run_retrieval_evaluation(vault, dataset, top_k=10, measure_context_budget=False, retrieval_mode="vector", vector_config=config)
    hybrid = run_retrieval_evaluation(vault, dataset, top_k=10, measure_context_budget=False, retrieval_mode="hybrid", vector_config=config)

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


def test_fixture_evaluation_is_deterministic_and_reports_all_required_metrics(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))

    report = run_retrieval_evaluation(vault, dataset, repeats=2)

    assert report["metadata"]["parameters"]["top_k"] == 10
    assert report["metadata"]["vault_fingerprint"]["file_count"] == 4
    assert report["metrics"]["recall_at_k_macro"] == 1.0
    assert report["metrics"]["mrr_at_k_macro"] == 1.0
    assert report["metrics"]["ndcg_at_k_macro"] == 1.0
    assert report["metrics"]["no_answer_false_positive_rate"] == 0.0
    assert report["metrics"]["filter_correctness"] == 1.0
    assert report["metrics"]["latency_sample_count"] == 12
    assert report["metrics"]["context_budget"]["within_budget"] is True
    assert all(case["ranking_runs"][0] == case["ranking_runs"][1] for case in report["cases"])
    assert not (vault / ".llm-wiki").exists()

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
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    zero_threshold_dataset = replace(dataset, manifest=replace(dataset.manifest, abstention_threshold=0.0))

    report = run_retrieval_evaluation(vault, zero_threshold_dataset, measure_context_budget=False)

    no_answer = next(case for case in report["cases"] if case["id"] == "no-answer")
    assert no_answer["ranked_paths"] == []
    assert no_answer["no_answer_false_positive"] is False


def test_context_budget_case_limit_samples_the_requested_prefix(tmp_path: Path) -> None:
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    vault = _copy_vault(tmp_path)

    report = run_retrieval_evaluation(vault, dataset, context_budget_case_limit=1)

    assert report["metadata"]["parameters"]["context_budget_case_limit"] == 1
    assert report["metrics"]["context_budget"] == {
        "measured_cases": 1,
        "case_limit": 1,
        "violations": [],
        "within_budget": True,
    }
    with pytest.raises(RetrievalEvalError, match="must be non-negative"):
        run_retrieval_evaluation(vault, dataset, context_budget_case_limit=-1)


def test_evaluation_records_experiment_metadata_and_compares_case_outcomes(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    baseline = run_retrieval_evaluation(vault, dataset, measure_context_budget=False)
    candidate = deepcopy(
        run_retrieval_evaluation(
            vault,
            dataset,
            measure_context_budget=False,
            experiment_metadata={
                "parent_baseline_id": "baseline-2026-07-27",
                "changed_item": "body_length_normalization",
                "parameters": {"body_length_exponent": 0.5},
            },
        )
    )
    candidate["metrics"]["recall_at_k_macro"] = 0.8
    candidate["cases"][0]["metrics"]["recall"] = 0.0

    comparison = compare_retrieval_reports(baseline, candidate)

    assert candidate["metadata"]["experiment"]["parent_baseline_id"] == "baseline-2026-07-27"
    assert comparison["metric_deltas"]["recall_at_k_macro"] == pytest.approx(-0.2)
    assert comparison["baseline"]["vault_fingerprint"] == comparison["candidate"]["vault_fingerprint"]
    assert next(case for case in comparison["cases"] if case["id"] == "concept-governance")["outcome"] == "loss"

    output = write_retrieval_comparison(comparison, tmp_path / "comparison")
    markdown = Path(output["markdown"]).read_text(encoding="utf-8")
    assert "candidate - baseline" in markdown
    assert Path(output["json"]).is_file()

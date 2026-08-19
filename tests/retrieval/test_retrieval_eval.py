from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from retrieval.retrieval_eval import (
    EvaluationQueryService,
    EvaluationFilterContract,
    EvaluationRuntimeSnapshot,
    McpEntryAdapter,
    Relevance,
    RetrievalEvalCase,
    RetrievalEvalDataset,
    RetrievalEvalError,
    RetrievalEvalManifest,
    calculate_ranking_metrics,
    evaluate_retrieval_gate,
    load_retrieval_dataset,
    normalize_evaluation_filter_contract,
    parse_evaluation_filters,
    percentile_95,
    run_retrieval_evaluation,
    safe_report_identity,
    validate_dataset_paths,
    write_retrieval_eval_report,
    _results_match_filters,
    _require_mcp_lexical_pipeline,
)
from retrieval.vector_index import VectorIndexStore, vector_index_records
from retrieval.retrieval_index import RetrievalIndexStore
from archive.archive_service import ArchiveService
from runtime.runtime_config import QualityGateSettings
from wiki.wiki_paths import filesystem_path
from retrieval.vector_provider import DeterministicFakeProvider
from tests.helpers import write_test_page
from wiki.wiki_paths import create_wiki_root


_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "retrieval"
_V2_40_FIXTURE_ROOT = _FIXTURE_ROOT / "v2_40"


def _copy_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(_FIXTURE_ROOT / "vault", vault)
    return vault


def _copy_dataset(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_public_evaluation_contract_normalizes_filter_aliases_and_redacts_identity() -> None:
    contract = normalize_evaluation_filter_contract(
        {"project": "Alpha", "type": "concept", "filter_tags": ["runbook"], "pathPrefix": "wiki/concepts/"},
        "aliases",
    )
    assert isinstance(contract, EvaluationFilterContract)
    assert dict(contract.public) == {"type": "concept", "tags": ["runbook"], "path_prefix": "wiki/concepts/"}
    assert dict(contract.query) == dict(contract.public)
    assert dict(contract.matcher) == {
        "type": "concept",
        "tags": ("runbook",),
        "path_prefix": "wiki/concepts/",
        "project": "Alpha",
    }
    assert parse_evaluation_filters(
        {"project": "Alpha", "type": "concept", "filter_tags": ["runbook"], "pathPrefix": "wiki/concepts/"},
        "aliases",
    ) == {
        "project": "Alpha",
        "filter_type": "concept",
        "filter_tags": ["runbook"],
        "path_prefix": "wiki/concepts/",
    }

    with pytest.raises(RetrievalEvalError, match="type and filter_type disagree"):
        normalize_evaluation_filter_contract({"type": "concept", "filter_type": "entity"}, "conflict")
    assert safe_report_identity(
        {
            "metadata": {
                "dataset_id": "fixture",
                "dataset_revision": "rev-1",
                "vault_fingerprint": {"algorithm": "sha256", "value": "abc", "file_count": 4, "path": "secret"},
                "ranking": {"version": "policy-v2", "absolute_path": "secret"},
                "absolute_path": "secret",
            }
        }
    ) == {
        "dataset_id": "fixture",
        "dataset_revision": "rev-1",
        "vault_fingerprint": {"algorithm": "sha256", "value": "abc", "file_count": 4},
        "ranking_version": "policy-v2",
    }


def test_dataset_relevant_path_rejection_uses_canonical_locator_error_translation(tmp_path: Path) -> None:
    dataset_path = tmp_path / "cases.jsonl"
    manifest_path = tmp_path / "manifest.json"
    dataset_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "colon-path",
                "query": "query",
                "answerable": True,
                "relevant": [{"path": "wiki/page:stream.md", "grade": 3}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "dataset_id": "fixture", "revision": "1", "abstention_threshold": 0}),
        encoding="utf-8",
    )

    with pytest.raises(RetrievalEvalError) as error:
        load_retrieval_dataset(dataset_path, manifest_path)
    assert error.value.code == "invalid_relevant_path"


def test_v2_evaluator_reads_existing_passage_store_without_telemetry_write(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    create_wiki_root(vault)
    page = Path("wiki/concepts/invoice.md")
    write_test_page(vault, page.as_posix(), {"title": "Invoice", "generated": True, "type": "concept"}, "invoice approval workflow")
    RetrievalIndexStore(vault).build(RetrievalIndexStore(vault).iter_vault_pages())
    dataset = RetrievalEvalDataset(RetrievalEvalManifest("v2", "1", 0.0), (RetrievalEvalCase("invoice", "invoice approval", (Relevance(page.as_posix(), 3),), {}, True, "en", (), ""),))
    report = run_retrieval_evaluation(vault, dataset, measure_context_budget=False)
    assert report["metadata"]["parameters"]["query_version"] == "v2"
    assert report["metrics"]["recall_at_k_macro"] == 1.0
    assert report["metadata"]["ranking"]["version"] == report["cases"][0]["pipeline"]["ranking_version"]
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
    write_test_page(vault, path.as_posix(), {"title": "Semantic", "generated": True, "type": "concept"}, "accounts payable operations")
    RetrievalIndexStore(vault).build(RetrievalIndexStore(vault).iter_vault_pages())
    provider = DeterministicFakeProvider({"expense automation": [1, 0, 0, 0], "accounts payable operations": [1, 0, 0, 0]})
    index_path = vault / ".llm-wiki" / "v2-eval"
    VectorIndexStore(vault, index_path).build(vector_index_records(vault), provider, include_raw_sources=False)
    monkeypatch.setattr("retrieval.query_pipeline.LocalBgeM3Provider", lambda *_args, **_kwargs: provider)
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
    write_test_page(
        vault,
        path.as_posix(),
        {"title": "Semantic result", "generated": True, "type": "concept"},
        "accounts payable operations",
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
    store.build(vector_index_records(vault), provider, include_raw_sources=False)
    monkeypatch.setattr("retrieval.query_pipeline.LocalBgeM3Provider", lambda *args, **kwargs: provider)
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
        "precision": pytest.approx(2 / 3),
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

    assert metrics == {"recall": 1.0, "precision": pytest.approx(0.1), "mrr": 1.0, "ndcg": 1.0, "hits": 1, "relevant_total": 1}


def test_filter_evaluation_requires_every_requested_tag() -> None:
    results = [{"path": "wiki/concepts/invoice.md", "metadata": {"tags": ["finance"]}}]

    assert _results_match_filters(results, {"filter_tags": ["finance"]})
    assert not _results_match_filters(results, {"filter_tags": ["finance", "approval"]})
    assert _results_match_filters(results, {"path_prefix": "wiki/concepts/"})
    assert not _results_match_filters(results, {"path_prefix": "wiki/projects/"})


def test_filter_evaluation_uses_frontmatter_project_and_rejects_scalar_tags() -> None:
    result = {
        "path": "wiki/projects/Billing/guide.md",
        "source_kind": "project",
        "frontmatter": {"project": "Billing", "tags": ["finance"]},
    }

    assert _results_match_filters([result], {"project": "billing", "filter_type": "project", "filter_tags": ["finance"]})
    scalar_tags = {**result, "frontmatter": {"project": "Billing", "tags": "finance"}}
    assert not _results_match_filters([scalar_tags], {"project": "billing", "filter_tags": ["finance"]})


def test_retrieval_gate_checks_frozen_identity_and_lexical_policy() -> None:
    baseline = {
        "metadata": {
            "dataset_id": "fixture",
            "dataset_revision": "1",
            "vault_fingerprint": {"value": "vault"},
            "ranking": {"version": "ranking-v1"},
            "parameters": {
                "retrieval_mode": "lexical",
                "vector_enabled": False,
                "measure_context_budget": True,
                "context_budget_mode": "engine_context_pack",
            },
        },
        "metrics": {
            "recall_at_k_macro": 0.8,
            "ndcg_at_k_macro": 0.75,
            "filter_correctness": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "p95_latency_ms": 100.0,
            "context_budget": {"measured_cases": 1, "within_budget": True, "violations": []},
        },
    }
    candidate = deepcopy(baseline)
    candidate["metrics"]["p95_latency_ms"] = 105.0

    gate = evaluate_retrieval_gate(candidate, baseline)

    assert gate["passed"] is True
    assert gate["status"] == "passed"
    assert gate["checks"]["lexical_only"]["passed"] is True

    candidate["metadata"]["parameters"]["vector_enabled"] = True
    failed = evaluate_retrieval_gate(candidate, baseline)
    assert failed["passed"] is False
    assert failed["checks"]["lexical_only"]["passed"] is False


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


def test_loader_normalizes_public_filter_aliases(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    row = json.loads(dataset.read_text(encoding="utf-8").splitlines()[0])
    row["filters"] = {
        "type": "concept",
        "tags": ["finance", "approval"],
        "pathPrefix": "wiki/concepts/",
    }
    dataset.write_text(json.dumps(row) + "\n", encoding="utf-8")

    loaded = load_retrieval_dataset(dataset)

    assert loaded.cases[0].filters == {
        "filter_type": "concept",
        "filter_tags": ["finance", "approval"],
        "path_prefix": "wiki/concepts/",
    }


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
    assert report["metadata"]["vault_fingerprint"]["file_count"] == 5
    assert report["metrics"]["recall_at_k_macro"] == 0.8
    assert report["metrics"]["recall_at_k_micro"] == pytest.approx(5 / 6)
    assert report["metrics"]["precision_at_k_macro"] == pytest.approx(0.1)
    assert report["metrics"]["precision_at_k_micro"] == pytest.approx(0.1)
    assert report["metrics"]["mrr_at_k_macro"] == 0.8
    assert report["metrics"]["ndcg_at_k_macro"] == 0.8
    assert set(report["metrics"]["ranking_by_k"]) == {"1", "3", "5", "10"}
    assert report["metrics"]["slice_metrics"]["language:zh-CN"]["case_count"] == 1
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
    assert "confirmation_token" not in json_report
    assert "source_pages" not in json_report
    assert "excerpt" not in json_report

    legacy_report = deepcopy(report)
    del legacy_report["metadata"]["ranking"]
    legacy_output = write_retrieval_eval_report(legacy_report, tmp_path / "legacy-reports")
    assert "legacy-unversioned" in Path(legacy_output["markdown"]).read_text(encoding="utf-8")


def test_shadow_evaluation_reports_gate_identity_and_metrics_without_changing_legacy_fp(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    _build_passage_store(vault)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))

    report = run_retrieval_evaluation(
        vault,
        dataset,
        measure_context_budget=False,
        quality_gate=QualityGateSettings(mode="shadow"),
    )

    quality_gate = report["metrics"]["quality_gate"]
    metadata_gate = report["metadata"]["quality_gate"]
    assert quality_gate["enabled"] is True
    assert quality_gate["status"] in {"proven", "unproven"}
    assert metadata_gate["gate_policy_version"] == "query-quality-policy-v0"
    assert len(metadata_gate["gate_config_hash"]) == 64
    assert report["metadata"]["gate_config_hash"] == metadata_gate["gate_config_hash"]
    observations = [case["quality_gate_observation"] for case in report["cases"]]
    assert all(observation is not None and observation["available"] is True for observation in observations)
    assert all(observation["mode"] == "shadow" for observation in observations)
    assert all("candidate_count" in observation for observation in observations)
    assert report["metrics"]["no_answer_false_positive_rate"] == 0.0
    assert not (vault / ".llm-wiki" / "state.sqlite3").exists()

    output = write_retrieval_eval_report(report, tmp_path / "shadow-reports")
    json_report = Path(output["json"]).read_text(encoding="utf-8")
    markdown_report = Path(output["markdown"]).read_text(encoding="utf-8")
    assert '"quality_gate"' in json_report
    assert "质量门禁观测" in markdown_report
    assert '"query"' not in json_report
    assert '"ranked_paths"' not in json_report


def test_mcp_entrypoint_uses_public_lexical_contract_without_vector_hits(tmp_path: Path) -> None:
    from app import server as server_module

    vault = _copy_vault(tmp_path)
    _build_passage_store(vault)
    registry_before = server_module.CONFIG_REGISTRY
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))

    report = run_retrieval_evaluation(
        vault,
        dataset,
        entrypoint="mcp",
        retrieval_mode="lexical",
        measure_context_budget=False,
    )

    assert report["metadata"]["parameters"]["entrypoint"] == "mcp"
    assert report["metadata"]["parameters"]["vector_enabled"] is False
    assert all(case["pipeline"]["retrieval_mode"] == "lexical" for case in report["cases"])
    assert all(case["pipeline"]["counters"]["vector_hits"] == 0 for case in report["cases"])
    assert report["metrics"]["filter_correctness"] == 1.0
    assert server_module.CONFIG_REGISTRY is registry_before
    assert not (vault / ".llm-wiki" / "state.sqlite3").exists()


def test_mcp_evaluation_services_are_isolated_without_registry_exchange(tmp_path: Path) -> None:
    from app import server as server_module

    first_vault = _copy_vault(tmp_path / "first")
    second_vault = _copy_vault(tmp_path / "second")
    _build_passage_store(first_vault)
    _build_passage_store(second_vault)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path / "dataset"))
    case = dataset.cases[0]
    registry_before = server_module.CONFIG_REGISTRY

    def run_case(vault: Path) -> list[str]:
        result = EvaluationQueryService(EvaluationRuntimeSnapshot.from_mcp_vault(vault)).run(
            case,
            top_k=10,
            include_context_pack=False,
            retrieval_mode="lexical",
            vector_config=None,
            query_version="v2",
            scope="knowledge",
            entrypoint="mcp",
        )
        return [str(item["path"]) for item in result["results"]]

    with ThreadPoolExecutor(max_workers=2) as executor:
        paths = list(executor.map(run_case, (first_vault, second_vault)))

    assert paths[0] == paths[1]
    assert server_module.CONFIG_REGISTRY is registry_before


def test_mcp_lexical_contract_requires_explicit_mode_and_zero_vector_hits() -> None:
    with pytest.raises(RetrievalEvalError, match="lexical-only"):
        _require_mcp_lexical_pipeline({"counters": {"vector_hits": 0}})
    with pytest.raises(RetrievalEvalError, match="lexical-only"):
        _require_mcp_lexical_pipeline({"retrieval_mode": "lexical", "counters": {}})
    with pytest.raises(RetrievalEvalError, match="lexical-only"):
        _require_mcp_lexical_pipeline({"retrieval_mode": "lexical", "counters": {"vector_hits": 1}})
    _require_mcp_lexical_pipeline({"retrieval_mode": "lexical", "counters": {"vector_hits": 0}})


def test_mcp_evaluation_uses_injected_adapter_without_importing_server_in_eval_path(tmp_path: Path) -> None:
    from app import server as server_module

    vault = _copy_vault(tmp_path)
    _build_passage_store(vault)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))
    source_resolution = server_module.resolve_tool_vault(vault_root=str(vault))
    calls: list[str] = []

    def resolve(root: str):
        calls.append(f"resolve:{root}")
        return source_resolution

    @contextmanager
    def snapshot(resolution: object):
        calls.append("snapshot-enter")
        yield
        calls.append("snapshot-exit")

    def query(**kwargs: object) -> dict[str, object]:
        calls.append("query")
        return {
            "ok": True,
            "results": [],
            "pipeline": {"retrieval_mode": "lexical", "counters": {"vector_hits": 0}},
            "budget": {},
        }

    adapter = McpEntryAdapter(resolve=resolve, snapshot=snapshot, query=query)
    runtime = EvaluationRuntimeSnapshot.from_mcp_vault(vault, adapter=adapter)
    result = EvaluationQueryService(runtime).run(
        dataset.cases[0],
        top_k=10,
        include_context_pack=False,
        retrieval_mode="lexical",
        vector_config=None,
        query_version="v2",
        scope="knowledge",
        entrypoint="mcp",
    )

    assert result["pipeline"] == {"retrieval_mode": "lexical", "counters": {"vector_hits": 0}}
    assert calls == [f"resolve:{vault}", "snapshot-enter", "query", "snapshot-exit"]


def test_retrieval_eval_keeps_server_import_lazy_at_the_mcp_seam() -> None:
    repository = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(repository / "src"), environment.get("PYTHONPATH")) if item
    )
    probe = subprocess.run(
        [sys.executable, "-c", "import sys; import retrieval.retrieval_eval; print('app.server' in sys.modules)"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "False"

    import retrieval.retrieval_eval as retrieval_eval_module
    from app import server as server_module

    adapter = retrieval_eval_module.default_mcp_entry_adapter()
    assert adapter.resolve.__module__ == server_module.__name__
    assert adapter.query is server_module.wiki_query
    assert not hasattr(retrieval_eval_module, "server_module")


def test_report_writer_sanitizes_legacy_pipeline_before_persisting(tmp_path: Path) -> None:
    report = {
        "metadata": {
            "dataset_id": "fixture",
            "dataset_revision": "rev",
            "parameters": {"top_k": 1},
            "vault_fingerprint": {"value": "fingerprint", "file_count": 1},
            "runtime_provenance": {"package_version": "test", "revision": "rev"},
            "ranking": {"version": "test"},
            "query_v2": {},
        },
        "metrics": {
            "recall_at_k_macro": 1.0,
            "recall_at_k_micro": 1.0,
            "precision_at_k_macro": 1.0,
            "precision_at_k_micro": 1.0,
            "mrr_at_k_macro": 1.0,
            "ndcg_at_k_macro": 1.0,
            "no_answer_false_positive_rate": 0.0,
            "context_budget": {"within_budget": True},
            "filter_correctness": 1.0,
            "p95_latency_ms": 1.0,
        },
        "cases": [
            {
                "id": "case",
                "ranked_paths": [],
                "filter_correct": True,
                "pipeline": {
                    "retrieval_mode": "lexical",
                    "counters": {"vector_hits": 0},
                    "discovery": {"evidence": {"excerpt": "secret"}},
                    "confirmation_token": "secret-token",
                    "fallback": {"level": "raw", "reasons": ["safe_reason", "contains spaces and details"]},
                },
            }
        ],
    }
    output = write_retrieval_eval_report(report, tmp_path / "reports")
    text = Path(output["json"]).read_text(encoding="utf-8")
    assert "secret" not in text
    assert "confirmation_token" not in text
    assert "excerpt" not in text
    assert "safe_reason" in text
    assert "contains spaces and details" not in text


def test_mcp_entrypoint_rejects_non_lexical_mode_before_query(tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    dataset = load_retrieval_dataset(_copy_dataset(tmp_path))

    with pytest.raises(RetrievalEvalError, match="lexical-only") as error:
        run_retrieval_evaluation(vault, dataset, entrypoint="mcp", retrieval_mode="hybrid", vector_config={"model_path": "unused"})

    assert error.value.code == "mcp_requires_lexical"


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

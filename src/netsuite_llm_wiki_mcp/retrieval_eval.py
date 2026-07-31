"""Deterministic, read-only evaluation for the public wiki query API."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from netsuite_llm_wiki_mcp.runtime_provenance import RUNTIME_PROVENANCE
from netsuite_llm_wiki_mcp.wiki_query import DEFAULT_TOP_K, RANKING_VERSION, wiki_query
from netsuite_llm_wiki_mcp.query_pipeline import QueryFilters, RANKING_POLICY_VERSION, run_query_v2
from netsuite_llm_wiki_mcp.runtime_config import EmbeddingSettings, TelemetrySettings
from netsuite_llm_wiki_mcp.vector_index import parse_vector_settings
from netsuite_llm_wiki_mcp.knowledge_compiler import filesystem_path


RETRIEVAL_EVAL_SCHEMA_VERSION = 1
_ALLOWED_FILTERS = {"project", "filter_type", "filter_tags"}
_COMPARISON_METRICS = (
    "recall_at_k_macro",
    "mrr_at_k_macro",
    "ndcg_at_k_macro",
    "no_answer_false_positive_rate",
    "filter_correctness",
    "p95_latency_ms",
)


class RetrievalEvalError(ValueError):
    """A user-correctable dataset or evaluation configuration error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Relevance:
    path: str
    grade: int


@dataclass(frozen=True)
class RetrievalEvalCase:
    id: str
    query: str
    relevant: tuple[Relevance, ...]
    filters: dict[str, Any]
    answerable: bool
    language: str
    tags: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class RetrievalEvalManifest:
    dataset_id: str
    revision: str
    abstention_threshold: float


@dataclass(frozen=True)
class RetrievalEvalDataset:
    manifest: RetrievalEvalManifest
    cases: tuple[RetrievalEvalCase, ...]


def load_retrieval_dataset(
    dataset_path: str | Path,
    manifest_path: str | Path | None = None,
) -> RetrievalEvalDataset:
    """Load a versioned JSONL case set and its small JSON manifest."""
    cases_file = Path(dataset_path).expanduser().resolve()
    manifest_file = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else cases_file.with_name(f"{cases_file.stem}.manifest.json")
    )
    if not cases_file.is_file():
        raise RetrievalEvalError("dataset_missing", f"dataset does not exist: {cases_file}")
    if not manifest_file.is_file():
        raise RetrievalEvalError("manifest_missing", f"manifest does not exist: {manifest_file}")

    try:
        manifest_raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalEvalError("manifest_invalid", f"invalid manifest: {exc}") from exc
    manifest = _parse_manifest(manifest_raw)

    cases: list[RetrievalEvalCase] = []
    seen_ids: set[str] = set()
    try:
        lines = cases_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RetrievalEvalError("dataset_unreadable", f"cannot read dataset: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetrievalEvalError("case_invalid_json", f"line {line_number}: {exc.msg}") from exc
        case = _parse_case(raw, line_number)
        if case.id in seen_ids:
            raise RetrievalEvalError("duplicate_case_id", f"duplicate case id: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise RetrievalEvalError("dataset_empty", "dataset must contain at least one case")
    return RetrievalEvalDataset(manifest=manifest, cases=tuple(cases))


def validate_dataset_paths(dataset: RetrievalEvalDataset, vault_root: str | Path) -> None:
    """Ensure labels point at files inside the supplied vault without reading bodies."""
    root = filesystem_path(vault_root)
    if not root.is_dir():
        raise RetrievalEvalError("vault_missing", f"vault root is not a directory: {root}")
    for case in dataset.cases:
        for relevance in case.relevant:
            target = (root / relevance.path).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise RetrievalEvalError(
                    "relevant_path_missing",
                    f"case {case.id} references a missing path: {relevance.path}",
                )


def calculate_ranking_metrics(
    ranked_paths: Sequence[str],
    relevant: Sequence[Relevance],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, float | int | None]:
    """Calculate Recall, MRR and nDCG without any dependency on the query code."""
    if top_k <= 0:
        raise RetrievalEvalError("invalid_top_k", "top_k must be greater than zero")
    grades = {item.path: item.grade for item in relevant}
    # A ranking evaluates documents, not repeated passages from the same
    # document. Keep first occurrence so malformed or legacy callers cannot
    # inflate Recall or nDCG by returning one relevant path repeatedly.
    ranked = list(dict.fromkeys(ranked_paths))[:top_k]
    hits = [path for path in ranked if path in grades]
    relevant_total = len(grades)
    if not relevant_total:
        return {"recall": None, "mrr": None, "ndcg": None, "hits": 0, "relevant_total": 0}

    first_rank = next((index for index, path in enumerate(ranked, 1) if path in grades), None)
    dcg = sum((2**grades[path] - 1) / math.log2(index + 1) for index, path in enumerate(ranked, 1) if path in grades)
    ideal_grades = sorted(grades.values(), reverse=True)[:top_k]
    ideal_dcg = sum((2**grade - 1) / math.log2(index + 1) for index, grade in enumerate(ideal_grades, 1))
    return {
        "recall": len(hits) / relevant_total,
        "mrr": 1 / first_rank if first_rank is not None else 0.0,
        "ndcg": dcg / ideal_dcg if ideal_dcg else 0.0,
        "hits": len(hits),
        "relevant_total": relevant_total,
    }


def percentile_95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def vault_fingerprint(vault_root: str | Path) -> dict[str, Any]:
    """Hash queryable wiki files by relative path and content without exposing either."""
    root = filesystem_path(vault_root)
    wiki = root / "wiki"
    digest = hashlib.sha256()
    count = 0
    if wiki.is_dir():
        for path in sorted(wiki.rglob("*.md")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content_hash.encode("ascii"))
            digest.update(b"\n")
            count += 1
    return {"algorithm": "sha256(path\\0content_sha256)", "value": digest.hexdigest(), "file_count": count}


def run_retrieval_evaluation(
    vault_root: str | Path,
    dataset: RetrievalEvalDataset,
    *,
    top_k: int = DEFAULT_TOP_K,
    repeats: int = 1,
    measure_context_budget: bool = True,
    context_budget_case_limit: int | None = None,
    experiment_metadata: Mapping[str, Any] | None = None,
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "lexical",
    vector_config: Mapping[str, Any] | None = None,
    query_version: str = "v2",
    scope: Literal["auto", "knowledge", "history", "all", "archive"] = "knowledge",
) -> dict[str, Any]:
    """Execute the public query API only; this function never mutates the vault."""
    if top_k <= 0:
        raise RetrievalEvalError("invalid_top_k", "top_k must be greater than zero")
    if repeats <= 0:
        raise RetrievalEvalError("invalid_repeats", "repeats must be greater than zero")
    if context_budget_case_limit is not None and context_budget_case_limit < 0:
        raise RetrievalEvalError("invalid_context_budget_case_limit", "context_budget_case_limit must be non-negative")
    if retrieval_mode not in {"lexical", "vector", "hybrid"}:
        raise RetrievalEvalError("invalid_retrieval_mode", "retrieval_mode must be lexical, vector, or hybrid")
    if retrieval_mode != "lexical" and not vector_config:
        raise RetrievalEvalError("vector_config_missing", "vector and hybrid evaluation require a local vector configuration")
    if query_version not in {"v1", "v2"}:
        raise RetrievalEvalError("invalid_query_version", "query_version must be v1 or v2")
    if scope not in {"auto", "knowledge", "history", "all", "archive"}:
        raise RetrievalEvalError("invalid_scope", "scope must be auto, knowledge, history, all, or archive")
    root = filesystem_path(vault_root)
    validate_dataset_paths(dataset, root)

    # Warm the interpreter and parser without treating it as a latency sample.
    first = dataset.cases[0]
    _query_case(root, first, top_k=top_k, include_context_pack=False, retrieval_mode=retrieval_mode, vector_config=vector_config, query_version=query_version, scope=scope)

    cases: list[dict[str, Any]] = []
    latency_samples: list[float] = []
    recall_values: list[float] = []
    mrr_values: list[float] = []
    ndcg_values: list[float] = []
    total_hits = 0
    total_relevant = 0
    no_answer_cases = 0
    no_answer_false_positives = 0
    filter_failures = 0
    budget_violations: list[str] = []
    fallback_cases = 0
    fallback_reasons: dict[str, int] = {}
    packed_token_samples: list[int] = []
    scope_contracts: list[dict[str, object]] = []

    for case_index, case in enumerate(dataset.cases):
        query_runs: list[dict[str, Any]] = []
        rankings: list[list[str]] = []
        for _ in range(repeats):
            started = time.perf_counter()
            result = _query_case(root, case, top_k=top_k, include_context_pack=False, retrieval_mode=retrieval_mode, vector_config=vector_config, query_version=query_version, scope=scope)
            elapsed_ms = (time.perf_counter() - started) * 1_000
            latency_samples.append(elapsed_ms)
            rankings.append([str(item["path"]) for item in result["results"]])
            query_runs.append({"latency_ms": elapsed_ms, "result": result})
        if any(ranking != rankings[0] for ranking in rankings[1:]):
            raise RetrievalEvalError("non_deterministic_ranking", f"case {case.id} changed ranking across repeats")

        result = query_runs[0]["result"]
        ranked_paths = rankings[0]
        metrics = calculate_ranking_metrics(ranked_paths, case.relevant, top_k=top_k)
        recall = metrics["recall"]
        if recall is not None:
            recall_values.append(float(recall))
            mrr_values.append(float(metrics["mrr"] or 0.0))
            ndcg_values.append(float(metrics["ndcg"] or 0.0))
            total_hits += int(metrics["hits"] or 0)
            total_relevant += int(metrics["relevant_total"] or 0)

        filter_correct = _results_match_filters(result["results"], case.filters)
        if not filter_correct:
            filter_failures += 1
        top_score = float(result["results"][0]["score"]) if result["results"] else 0.0
        false_positive = (
            not case.answerable
            and bool(result["results"])
            and top_score >= dataset.manifest.abstention_threshold
        )
        if not case.answerable:
            no_answer_cases += 1
            no_answer_false_positives += int(false_positive)

        budget: dict[str, Any] | None = None
        if measure_context_budget and (context_budget_case_limit is None or case_index < context_budget_case_limit):
            context_result = _query_case(root, case, top_k=top_k, include_context_pack=True, retrieval_mode=retrieval_mode, vector_config=vector_config, query_version=query_version, scope=scope)
            raw_budget = context_result.get("budget") or context_result.get("context_pack", {}).get("budget", {})
            budget = dict(raw_budget)
            raw_used = budget.get("used", {})
            used = int(raw_used) if isinstance(raw_used, int) else sum(int(value) for value in raw_used.values())
            budget["used_total"] = used
            budget["within_budget"] = used <= int(budget.get("total", 0))
            if not budget["within_budget"]:
                budget_violations.append(case.id)
            packed_token_samples.append(used)

        pipeline = result.get("pipeline", {})
        fallback = pipeline.get("fallback", {}) if isinstance(pipeline, Mapping) else {}
        fallback_level = str(fallback.get("level", "legacy")) if isinstance(fallback, Mapping) else "legacy"
        if fallback_level not in {"none", "legacy"}:
            fallback_cases += 1
        if isinstance(fallback, Mapping):
            for reason in fallback.get("reasons", []):
                fallback_reasons[str(reason)] = fallback_reasons.get(str(reason), 0) + 1
        scope_contracts.append({
            "id": case.id,
            "scope": pipeline.get("scope", "legacy") if isinstance(pipeline, Mapping) else "legacy",
            "corpus": pipeline.get("corpus", "active") if isinstance(pipeline, Mapping) else "active",
            "authority": pipeline.get("authority", "legacy") if isinstance(pipeline, Mapping) else "legacy",
            "fallback_level": fallback_level,
        })

        cases.append(
            {
                "id": case.id,
                "query": case.query,
                "answerable": case.answerable,
                "filters": case.filters,
                "ranked_paths": ranked_paths,
                "ranking_runs": rankings,
                "latency_ms": [item["latency_ms"] for item in query_runs],
                "metrics": metrics,
                "top_score": top_score,
                "no_answer_false_positive": false_positive,
                "filter_correct": filter_correct,
                "context_budget": budget,
                "pipeline": result["pipeline"],
                "warnings": result["pipeline"].get("stage_1_5_vector_warnings", result["pipeline"].get("warnings", [])),
                "result_summary": [_result_summary(item) for item in result["results"]],
            }
        )

    return {
        "schema_version": RETRIEVAL_EVAL_SCHEMA_VERSION,
        "metadata": {
            "dataset_id": dataset.manifest.dataset_id,
            "dataset_revision": dataset.manifest.revision,
            "abstention_threshold": dataset.manifest.abstention_threshold,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "runtime_provenance": RUNTIME_PROVENANCE.to_public_dict(),
            "ranking": {"version": RANKING_POLICY_VERSION if query_version == "v2" else RANKING_VERSION},
            "experiment": _normalise_experiment_metadata(experiment_metadata),
            "parameters": {
                "top_k": top_k,
                "include_content": False,
                "include_context_pack": False,
                "repeats": repeats,
                "measure_context_budget": measure_context_budget,
                "context_budget_case_limit": context_budget_case_limit,
                "retrieval_mode": retrieval_mode,
                "vector_enabled": retrieval_mode != "lexical",
                "query_version": query_version,
                "scope": scope,
            },
            "query_v2": {
                "scope_authority_lifecycle": scope_contracts,
                "cold_start_latency_ms": None,
                "comparison_status": "unproven_without_frozen_v2_baseline",
            },
            "vault_fingerprint": vault_fingerprint(root),
        },
        "metrics": {
            "recall_at_k_macro": _mean_or_none(recall_values),
            "mrr_at_k_macro": _mean_or_none(mrr_values),
            "ndcg_at_k_macro": _mean_or_none(ndcg_values),
            "relevant_hits": total_hits,
            "relevant_total": total_relevant,
            "no_answer_false_positive_rate": no_answer_false_positives / no_answer_cases if no_answer_cases else None,
            "no_answer_cases": no_answer_cases,
            "filter_correctness": 1.0 - (filter_failures / len(cases)),
            "filter_failures": filter_failures,
            "p95_latency_ms": percentile_95(latency_samples),
            "warm_p95_latency_ms": percentile_95(latency_samples),
            "cold_start_latency_ms": None,
            "fallback_rate": fallback_cases / len(cases),
            "fallback_reason_distribution": fallback_reasons,
            "packed_token_median": _median(packed_token_samples),
            "latency_sample_count": len(latency_samples),
            "context_budget": {
                "measured_cases": sum(1 for case in cases if case["context_budget"] is not None),
                "case_limit": context_budget_case_limit,
                "violations": budget_violations,
                "within_budget": not budget_violations,
            },
        },
        "cases": cases,
    }


def write_retrieval_eval_report(report: Mapping[str, Any], output_dir: str | Path) -> dict[str, str]:
    """Write machine-readable JSON and a compact Markdown summary."""
    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "retrieval-eval.json"
    markdown_path = target / "retrieval-eval.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata = report["metadata"]
    metrics = report["metrics"]
    ranking_metadata = metadata.get("ranking")
    ranking_version = ranking_metadata.get("version", "legacy-unversioned") if isinstance(ranking_metadata, Mapping) else "legacy-unversioned"
    lines = [
        "# 检索评测报告",
        "",
        f"- 数据集：`{metadata['dataset_id']}`（revision `{metadata['dataset_revision']}`）",
        f"- `top_k`：{metadata['parameters']['top_k']}",
        f"- 语料指纹：`{metadata['vault_fingerprint']['value']}`（{metadata['vault_fingerprint']['file_count']} 个 wiki 文件）",
        f"- 运行版本：`{metadata['runtime_provenance']['package_version']}` / `{metadata['runtime_provenance']['revision']}`",
        f"- 排名版本：`{ranking_version}`",
        "",
        "## 指标",
        "",
        f"- Recall@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics['recall_at_k_macro'])}",
        f"- MRR@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics['mrr_at_k_macro'])}",
        f"- nDCG@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics['ndcg_at_k_macro'])}",
        f"- 无答案误命中率：{_format_metric(metrics['no_answer_false_positive_rate'])}",
        f"- 过滤器正确性：{_format_metric(metrics['filter_correctness'])}",
        f"- P95 延迟：{metrics['p95_latency_ms']:.3f} ms",
        f"- Context budget：{'通过' if metrics['context_budget']['within_budget'] else '失败'}",
        "",
        "## Case 摘要",
        "",
    ]
    for case in report["cases"]:
        first_path = case["ranked_paths"][0] if case["ranked_paths"] else "（无结果）"
        lines.append(f"- `{case['id']}`：首项 `{first_path}`；过滤器 {'通过' if case['filter_correct'] else '失败'}")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def compare_retrieval_reports(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two reports from the same frozen dataset and vault fingerprint.

    The function intentionally does not decide a release gate: P95 is
    environment-sensitive, while the caller owns the task-specific thresholds.
    It supplies reproducible deltas and per-case win/tie/loss evidence instead.
    """

    baseline_metadata = _mapping(baseline.get("metadata"), "baseline_invalid", "baseline metadata must be an object")
    candidate_metadata = _mapping(candidate.get("metadata"), "candidate_invalid", "candidate metadata must be an object")
    _require_comparable_reports(baseline_metadata, candidate_metadata)
    baseline_metrics = _mapping(baseline.get("metrics"), "baseline_invalid", "baseline metrics must be an object")
    candidate_metrics = _mapping(candidate.get("metrics"), "candidate_invalid", "candidate metrics must be an object")
    baseline_cases = _cases_by_id(baseline, "baseline_invalid")
    candidate_cases = _cases_by_id(candidate, "candidate_invalid")
    if baseline_cases.keys() != candidate_cases.keys():
        raise RetrievalEvalError("baseline_incompatible", "baseline and candidate cases must have identical IDs")

    metric_deltas = {
        metric: _metric_delta(baseline_metrics.get(metric), candidate_metrics.get(metric))
        for metric in _COMPARISON_METRICS
    }
    return {
        "schema_version": RETRIEVAL_EVAL_SCHEMA_VERSION,
        "baseline": _report_identity(baseline_metadata),
        "candidate": _report_identity(candidate_metadata),
        "metric_deltas": metric_deltas,
        "cases": [
            _compare_case(case_id, baseline_cases[case_id], candidate_cases[case_id])
            for case_id in sorted(baseline_cases)
        ],
    }


def write_retrieval_comparison(comparison: Mapping[str, Any], output_dir: str | Path) -> dict[str, str]:
    """Persist a compact, reviewable comparison alongside candidate reports."""

    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "retrieval-comparison.json"
    markdown_path = target / "retrieval-comparison.md"
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    deltas = _mapping(comparison.get("metric_deltas"), "comparison_invalid", "comparison metric_deltas must be an object")
    lines = [
        "# 检索评测对比",
        "",
        f"- 基线数据集：`{comparison['baseline']['dataset_id']}`（revision `{comparison['baseline']['dataset_revision']}`）",
        f"- 候选排名版本：`{comparison['candidate']['ranking_version']}`",
        "",
        "## 指标差值（candidate - baseline）",
        "",
    ]
    lines.extend(f"- `{metric}`：{_format_delta(deltas.get(metric))}" for metric in _COMPARISON_METRICS)
    lines.extend(["", "## Case 对比", ""])
    lines.extend(f"- `{case['id']}`：{case['outcome']}" for case in comparison["cases"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _parse_manifest(raw: object) -> RetrievalEvalManifest:
    data = _mapping(raw, "manifest_invalid", "manifest must be a JSON object")
    _require_schema_version(data, "manifest")
    dataset_id = _nonempty_string(data.get("dataset_id"), "manifest_invalid", "manifest.dataset_id must be a string")
    revision = _nonempty_string(data.get("revision"), "manifest_invalid", "manifest.revision must be a string")
    threshold = data.get("abstention_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        raise RetrievalEvalError("manifest_invalid", "manifest.abstention_threshold must be a non-negative number")
    return RetrievalEvalManifest(dataset_id=dataset_id, revision=revision, abstention_threshold=float(threshold))


def _parse_case(raw: object, line_number: int) -> RetrievalEvalCase:
    data = _mapping(raw, "case_invalid", f"line {line_number} must be a JSON object")
    _require_schema_version(data, f"case at line {line_number}")
    case_id = _nonempty_string(data.get("id"), "case_invalid", f"line {line_number}: id must be a string")
    query = _nonempty_string(data.get("query"), "case_invalid", f"case {case_id}: query must be a string")
    answerable = data.get("answerable")
    if type(answerable) is not bool:
        raise RetrievalEvalError("case_invalid", f"case {case_id}: answerable must be boolean")
    filters = _parse_filters(data.get("filters", {}), case_id)
    relevant_raw = data.get("relevant")
    if not isinstance(relevant_raw, list):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: relevant must be a list")
    relevance: list[Relevance] = []
    seen_paths: set[str] = set()
    for item in relevant_raw:
        item_data = _mapping(item, "case_invalid", f"case {case_id}: relevant item must be an object")
        path = _normalise_relative_path(item_data.get("path"), case_id)
        grade = item_data.get("grade")
        if type(grade) is not int or not 1 <= grade <= 3:
            raise RetrievalEvalError("invalid_grade", f"case {case_id}: grade for {path} must be an integer from 1 to 3")
        if path in seen_paths:
            raise RetrievalEvalError("duplicate_relevant_path", f"case {case_id}: duplicate relevant path {path}")
        seen_paths.add(path)
        relevance.append(Relevance(path=path, grade=grade))
    if not answerable and relevance:
        raise RetrievalEvalError("invalid_no_answer_case", f"case {case_id}: answerable=false requires no relevant paths")
    if answerable and not relevance:
        raise RetrievalEvalError("missing_relevant_path", f"case {case_id}: answerable=true requires at least one relevant path")
    language = _nonempty_string(data.get("language"), "case_invalid", f"case {case_id}: language must be a string")
    tags_raw = data.get("tags", [])
    if not isinstance(tags_raw, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags_raw):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: tags must be strings")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: notes must be a string")
    return RetrievalEvalCase(case_id, query, tuple(relevance), filters, answerable, language, tuple(tags_raw), notes)


def _parse_filters(raw: object, case_id: str) -> dict[str, Any]:
    data = _mapping(raw, "invalid_filters", f"case {case_id}: filters must be an object")
    unknown = sorted(set(data) - _ALLOWED_FILTERS)
    if unknown:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: unsupported filters: {', '.join(unknown)}")
    filters: dict[str, Any] = {}
    for name in ("project", "filter_type"):
        if name in data:
            filters[name] = _nonempty_string(data[name], "invalid_filters", f"case {case_id}: {name} must be a string")
    if "filter_tags" in data:
        tags = data["filter_tags"]
        if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise RetrievalEvalError("invalid_filters", f"case {case_id}: filter_tags must be a non-empty list of strings")
        filters["filter_tags"] = list(tags)
    return filters


def _query_case(
    root: Path,
    case: RetrievalEvalCase,
    *,
    top_k: int,
    include_context_pack: bool,
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "lexical",
    vector_config: Mapping[str, Any] | None = None,
    query_version: str = "v2",
    scope: Literal["auto", "knowledge", "history", "all", "archive"] = "knowledge",
) -> dict[str, Any]:
    if query_version == "v2":
        embedding: EmbeddingSettings | None = None
        if retrieval_mode != "lexical":
            settings = parse_vector_settings(root, dict(vector_config or {}))
            embedding = EmbeddingSettings(enabled=True, provider=settings.provider, model_path=settings.model_path, index_path=settings.index_path, device=settings.device, batch_size=settings.batch_size, max_sequence_length=settings.max_sequence_length, candidate_limit=settings.candidate_limit, rrf_k=settings.rrf_k, min_vector_score=settings.min_vector_score)
        return run_query_v2(
            root,
            case.query,
            scope=scope,
            project=case.filters.get("project"),
            filters=QueryFilters(case.filters.get("filter_type"), tuple(case.filters.get("filter_tags") or ())),
            top_k=top_k,
            embedding=embedding,
            telemetry=TelemetrySettings(enabled=False),
            include_context_pack=include_context_pack,
            retrieval_mode=retrieval_mode,
        )
    return wiki_query(
        root,
        case.query,
        top_k=top_k,
        include_content=False,
        include_context_pack=include_context_pack,
        enable_vector=retrieval_mode != "lexical",
        vector_config=dict(vector_config) if vector_config is not None else None,
        retrieval_mode=retrieval_mode,
        project=case.filters.get("project"),
        filter_type=case.filters.get("filter_type"),
        filter_tags=case.filters.get("filter_tags"),
    )


def _results_match_filters(results: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]) -> bool:
    for item in results:
        path = str(item["path"])
        frontmatter = item.get("frontmatter") or item.get("metadata")
        if not isinstance(frontmatter, Mapping):
            return False
        if "project" in filters and not _in_project_scope(path, str(filters["project"])):
            return False
        if "filter_type" in filters and frontmatter.get("type") != filters["filter_type"]:
            return False
        if "filter_tags" in filters:
            raw_tags = frontmatter.get("tags", [])
            tags = raw_tags if isinstance(raw_tags, list) else [raw_tags]
            if not set(filters["filter_tags"]).issubset({str(tag) for tag in tags}):
                return False
    return True


def _in_project_scope(path: str, project: str) -> bool:
    return path.startswith(f"wiki/projects/{project}/") or path.startswith(f"raw/sources/file/{project}/")


def _result_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": item["path"],
        "score": item["score"],
        "scores": item["scores"],
        "source_kind": item["source_kind"],
    }


def _mapping(raw: object, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise RetrievalEvalError(code, message)
    return raw


def _require_schema_version(data: Mapping[str, Any], subject: str) -> None:
    if data.get("schema_version") != RETRIEVAL_EVAL_SCHEMA_VERSION:
        raise RetrievalEvalError("unsupported_schema", f"{subject} must use schema_version={RETRIEVAL_EVAL_SCHEMA_VERSION}")


def _nonempty_string(value: object, code: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalEvalError(code, message)
    return value.strip()


def _normalise_relative_path(value: object, case_id: str) -> str:
    path = _nonempty_string(value, "case_invalid", f"case {case_id}: relevant path must be a string").replace("\\", "/")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise RetrievalEvalError("invalid_relevant_path", f"case {case_id}: relevant path must be vault-relative")
    return pure.as_posix()


def _mean_or_none(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: Sequence[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _format_metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _normalise_experiment_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise RetrievalEvalError("invalid_experiment_metadata", "experiment metadata must be an object")
    try:
        # Round-trip both verifies JSON report compatibility and makes the
        # stored metadata independent of caller-owned mutable containers.
        value = json.loads(json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise RetrievalEvalError("invalid_experiment_metadata", "experiment metadata must be JSON serializable") from exc
    if not isinstance(value, dict):
        raise RetrievalEvalError("invalid_experiment_metadata", "experiment metadata must be an object")
    return value


def _require_comparable_reports(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    for key in ("dataset_id", "dataset_revision"):
        if baseline.get(key) != candidate.get(key):
            raise RetrievalEvalError("baseline_incompatible", f"baseline and candidate differ on {key}")
    baseline_fingerprint = _mapping(baseline.get("vault_fingerprint"), "baseline_invalid", "baseline vault fingerprint must be an object")
    candidate_fingerprint = _mapping(candidate.get("vault_fingerprint"), "candidate_invalid", "candidate vault fingerprint must be an object")
    if baseline_fingerprint.get("value") != candidate_fingerprint.get("value"):
        raise RetrievalEvalError("baseline_incompatible", "baseline and candidate vault fingerprints differ")
    baseline_parameters = _mapping(baseline.get("parameters"), "baseline_invalid", "baseline parameters must be an object")
    candidate_parameters = _mapping(candidate.get("parameters"), "candidate_invalid", "candidate parameters must be an object")
    if baseline_parameters.get("top_k") != candidate_parameters.get("top_k"):
        raise RetrievalEvalError("baseline_incompatible", "baseline and candidate top_k values differ")


def _report_identity(metadata: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = _mapping(metadata.get("vault_fingerprint"), "report_invalid", "report vault fingerprint must be an object")
    ranking_raw = metadata.get("ranking", {})
    ranking = _mapping(ranking_raw, "report_invalid", "report ranking must be an object")
    experiment = metadata.get("experiment", {})
    if not isinstance(experiment, Mapping):
        raise RetrievalEvalError("report_invalid", "report experiment metadata must be an object")
    return {
        "dataset_id": metadata.get("dataset_id"),
        "dataset_revision": metadata.get("dataset_revision"),
        "vault_fingerprint": fingerprint.get("value"),
        "ranking_version": ranking.get("version", "legacy-unversioned"),
        "parent_baseline_id": experiment.get("parent_baseline_id"),
    }


def _cases_by_id(report: Mapping[str, Any], code: str) -> dict[str, Mapping[str, Any]]:
    raw_cases = report.get("cases")
    if not isinstance(raw_cases, list):
        raise RetrievalEvalError(code, "report cases must be a list")
    cases: dict[str, Mapping[str, Any]] = {}
    for raw_case in raw_cases:
        case = _mapping(raw_case, code, "report case must be an object")
        case_id = _nonempty_string(case.get("id"), code, "report case id must be a string")
        if case_id in cases:
            raise RetrievalEvalError(code, f"duplicate report case id: {case_id}")
        cases[case_id] = case
    return cases


def _compare_case(case_id: str, baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    baseline_metrics = _mapping(baseline.get("metrics"), "baseline_invalid", "baseline case metrics must be an object")
    candidate_metrics = _mapping(candidate.get("metrics"), "candidate_invalid", "candidate case metrics must be an object")
    deltas = {
        metric: _metric_delta(baseline_metrics.get(metric), candidate_metrics.get(metric))
        for metric in ("recall", "mrr", "ndcg")
    }
    recall_delta = deltas["recall"]
    if recall_delta is not None and recall_delta > 0:
        outcome = "win"
    elif recall_delta is not None and recall_delta < 0:
        outcome = "loss"
    else:
        secondary = [delta for metric, delta in deltas.items() if metric != "recall" and delta is not None]
        if secondary and all(delta >= 0 for delta in secondary) and any(delta > 0 for delta in secondary):
            outcome = "win"
        elif secondary and all(delta <= 0 for delta in secondary) and any(delta < 0 for delta in secondary):
            outcome = "loss"
        else:
            outcome = "tie"
    return {"id": case_id, "outcome": outcome, "metric_deltas": deltas}


def _metric_delta(baseline: object, candidate: object) -> float | None:
    if baseline is None or candidate is None:
        return None
    if isinstance(baseline, bool) or isinstance(candidate, bool) or not isinstance(baseline, (int, float)) or not isinstance(candidate, (int, float)):
        raise RetrievalEvalError("report_invalid", "comparison metrics must be numeric or null")
    return float(candidate) - float(baseline)


def _format_delta(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetrievalEvalError("comparison_invalid", "comparison delta must be numeric or null")
    return f"{float(value):+.4f}"

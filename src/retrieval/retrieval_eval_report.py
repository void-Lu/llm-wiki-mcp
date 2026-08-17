"""检索评测的指标、报告投影和输出 owner。

这里的核心函数只消费已归一化的 case/result 视图，不读取 vault，也不调用
查询管线。文件写入仅限于报告输出目录，输入结果会在输出边界再次脱敏。
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from retrieval.retrieval_eval_dataset import Relevance, RetrievalEvalError


__all__ = [
    "calculate_ranking_metrics",
    "evaluate_retrieval_gate",
    "percentile_95",
    "safe_report_identity",
    "write_retrieval_eval_report",
]


def calculate_ranking_metrics(
    ranked_paths: Sequence[str],
    relevant: Sequence[Relevance],
    *,
    top_k: int = 10,
) -> dict[str, float | int | None]:
    """计算按页面去重的 Recall、Precision、MRR 和 nDCG。"""

    if top_k <= 0:
        raise RetrievalEvalError("invalid_top_k", "top_k must be greater than zero")
    grades = {item.path: item.grade for item in relevant}
    # 评测对象是页面而不是 passage；重复路径不能放大指标。
    ranked = list(dict.fromkeys(ranked_paths))[:top_k]
    hits = [path for path in ranked if path in grades]
    relevant_total = len(grades)
    if not relevant_total:
        return {"recall": None, "precision": None, "mrr": None, "ndcg": None, "hits": 0, "relevant_total": 0}

    first_rank = next((index for index, path in enumerate(ranked, 1) if path in grades), None)
    dcg = sum((2**grades[path] - 1) / math.log2(index + 1) for index, path in enumerate(ranked, 1) if path in grades)
    ideal_grades = sorted(grades.values(), reverse=True)[:top_k]
    ideal_dcg = sum((2**grade - 1) / math.log2(index + 1) for index, grade in enumerate(ideal_grades, 1))
    return {
        "recall": len(hits) / relevant_total,
        "precision": len(hits) / top_k,
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


def safe_report_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    """从报告中提取有限且不含路径的比较 identity。"""

    metadata = report.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    identity: dict[str, Any] = {}
    for name in ("dataset_id", "dataset_revision"):
        value = metadata.get(name)
        if isinstance(value, str) and value:
            identity[name] = value
    fingerprint = metadata.get("vault_fingerprint")
    if isinstance(fingerprint, Mapping):
        safe_fingerprint: dict[str, Any] = {}
        algorithm = fingerprint.get("algorithm")
        value = fingerprint.get("value")
        file_count = fingerprint.get("file_count")
        if isinstance(algorithm, str) and algorithm:
            safe_fingerprint["algorithm"] = algorithm
        if isinstance(value, str) and value:
            safe_fingerprint["value"] = value
        if type(file_count) is int and file_count >= 0:
            safe_fingerprint["file_count"] = file_count
        if safe_fingerprint:
            identity["vault_fingerprint"] = safe_fingerprint
    ranking = metadata.get("ranking")
    if isinstance(ranking, Mapping):
        version = ranking.get("version")
        if isinstance(version, str) and version:
            identity["ranking_version"] = version
    return identity


def evaluate_retrieval_gate(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    max_metric_regression: float = 0.02,
    max_latency_growth: float = 0.10,
    max_no_answer_false_positive_rate: float = 0.05,
) -> dict[str, Any]:
    """用冻结 baseline 比较候选报告；缺证据时返回 failed/unproven。"""

    candidate_metadata = candidate.get("metadata")
    baseline_metadata = baseline.get("metadata")
    candidate_metrics = candidate.get("metrics")
    baseline_metrics = baseline.get("metrics")
    if (
        not isinstance(candidate_metadata, Mapping)
        or not isinstance(baseline_metadata, Mapping)
        or not isinstance(candidate_metrics, Mapping)
        or not isinstance(baseline_metrics, Mapping)
    ):
        return {"passed": False, "status": "unproven", "reason": "report_shape_missing", "checks": {}}

    checks: dict[str, dict[str, Any]] = {}

    def add_check(
        name: str,
        passed: bool,
        *,
        actual: Any = None,
        expected: Any = None,
        reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"passed": bool(passed)}
        if actual is not None:
            payload["actual"] = actual
        if expected is not None:
            payload["expected"] = expected
        if reason:
            payload["reason"] = reason
        checks[name] = payload

    candidate_identity = safe_report_identity(candidate)
    baseline_identity = safe_report_identity(baseline)
    candidate_fingerprint = candidate_identity.get("vault_fingerprint")
    baseline_fingerprint = baseline_identity.get("vault_fingerprint")
    for name, candidate_value, baseline_value in (
        ("dataset_id", candidate_identity.get("dataset_id"), baseline_identity.get("dataset_id")),
        ("dataset_revision", candidate_identity.get("dataset_revision"), baseline_identity.get("dataset_revision")),
        (
            "vault_fingerprint",
            candidate_fingerprint.get("value") if isinstance(candidate_fingerprint, Mapping) else None,
            baseline_fingerprint.get("value") if isinstance(baseline_fingerprint, Mapping) else None,
        ),
        ("ranking_version", candidate_identity.get("ranking_version"), baseline_identity.get("ranking_version")),
    ):
        add_check(
            name,
            bool(candidate_value) and candidate_value == baseline_value,
            actual=candidate_value,
            expected=baseline_value,
            reason="baseline_identity_mismatch" if candidate_value != baseline_value else None,
        )

    candidate_parameters = candidate_metadata.get("parameters")
    if not isinstance(candidate_parameters, Mapping):
        candidate_parameters = {}
    add_check(
        "lexical_only",
        candidate_parameters.get("retrieval_mode") == "lexical" and candidate_parameters.get("vector_enabled") is False,
        actual={"retrieval_mode": candidate_parameters.get("retrieval_mode"), "vector_enabled": candidate_parameters.get("vector_enabled")},
        expected={"retrieval_mode": "lexical", "vector_enabled": False},
    )
    if "side_effects" in candidate_metadata or "side_effects" in baseline_metadata:
        side_effects = candidate_metadata.get("side_effects")
        add_check(
            "read_only_evaluation",
            isinstance(side_effects, Mapping) and side_effects.get("clean") is True,
            actual=side_effects.get("clean") if isinstance(side_effects, Mapping) else None,
            expected=True,
            reason="evaluation_side_effect_detected"
            if not isinstance(side_effects, Mapping) or side_effects.get("clean") is not True
            else None,
        )

    for metric_name in ("recall_at_k_macro", "ndcg_at_k_macro"):
        candidate_value = _finite_number(candidate_metrics.get(metric_name))
        baseline_value = _finite_number(baseline_metrics.get(metric_name))
        if candidate_value is None or baseline_value is None:
            add_check(metric_name, False, actual=candidate_value, expected=baseline_value, reason="metric_unproven")
        else:
            add_check(
                metric_name,
                candidate_value >= baseline_value - max_metric_regression,
                actual=candidate_value,
                expected={"minimum": baseline_value - max_metric_regression, "baseline": baseline_value},
            )

    filter_correctness = _finite_number(candidate_metrics.get("filter_correctness"))
    add_check("filter_correctness", filter_correctness is not None and filter_correctness >= 1.0, actual=filter_correctness, expected=1.0)

    no_answer_rate = _finite_number(candidate_metrics.get("no_answer_false_positive_rate"))
    add_check(
        "no_answer_false_positive_rate",
        no_answer_rate is not None and no_answer_rate <= max_no_answer_false_positive_rate,
        actual=no_answer_rate,
        expected=max_no_answer_false_positive_rate,
    )

    candidate_p95 = _finite_number(candidate_metrics.get("p95_latency_ms"))
    baseline_p95 = _finite_number(baseline_metrics.get("p95_latency_ms"))
    if candidate_p95 is None or baseline_p95 is None:
        add_check("p95_latency_ms", False, actual=candidate_p95, expected=baseline_p95, reason="latency_unproven")
    else:
        add_check(
            "p95_latency_ms",
            candidate_p95 <= baseline_p95 * (1.0 + max_latency_growth),
            actual=candidate_p95,
            expected={"maximum": baseline_p95 * (1.0 + max_latency_growth), "baseline": baseline_p95},
        )

    context_budget = candidate_metrics.get("context_budget")
    context_mode = candidate_parameters.get("context_budget_mode")
    if context_mode == "mcp_public_budget_not_exposed":
        add_check("context_budget", True, actual="not_applicable", expected="mcp_public_budget_not_exposed")
    else:
        budget_passed = (
            candidate_parameters.get("measure_context_budget") is True
            and isinstance(context_budget, Mapping)
            and (_finite_number(context_budget.get("measured_cases")) or 0.0) > 0
            and bool(context_budget.get("within_budget"))
            and not context_budget.get("violations")
        )
        add_check("context_budget", budget_passed, actual=context_budget, expected={"within_budget": True, "violations": []})

    passed = all(bool(check.get("passed")) for check in checks.values())
    return {
        "passed": passed,
        "status": "passed" if passed else "failed",
        "thresholds": {
            "max_metric_regression": max_metric_regression,
            "max_latency_growth": max_latency_growth,
            "max_no_answer_false_positive_rate": max_no_answer_false_positive_rate,
        },
        "checks": checks,
    }


def write_retrieval_eval_report(report: Mapping[str, Any], output_dir: str | Path) -> dict[str, str]:
    """写入 JSON 和 Markdown 报告；两种格式共享同一安全投影。"""

    safe_report = _sanitise_report_for_output(report)
    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "retrieval-eval.json"
    markdown_path = target / "retrieval-eval.md"
    json_path.write_text(json.dumps(safe_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata = safe_report["metadata"]
    metrics = safe_report["metrics"]
    ranking_metadata = metadata.get("ranking")
    ranking_version = ranking_metadata.get("version", "legacy-unversioned") if isinstance(ranking_metadata, Mapping) else "legacy-unversioned"
    lines = [
        "# 检索评测报告",
        "",
        f"- 数据集：`{metadata['dataset_id']}`（revision `{metadata['dataset_revision']}`）",
        f"- 评测状态：`{metadata.get('query_v2', {}).get('evaluation_status', 'unknown')}`",
        f"- `top_k`：{metadata['parameters']['top_k']}",
        f"- 语料指纹：`{metadata['vault_fingerprint']['value']}`（{metadata['vault_fingerprint']['file_count']} 个 wiki 文件）",
        f"- 运行版本：`{metadata['runtime_provenance']['package_version']}` / `{metadata['runtime_provenance']['revision']}`",
        f"- 排名版本：`{ranking_version}`",
        "",
        "## 指标",
        "",
        f"- Recall@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics['recall_at_k_macro'])}",
        f"- Recall@{metadata['parameters']['top_k']}（micro）：{_format_metric(metrics.get('recall_at_k_micro'))}",
        f"- Precision@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics.get('precision_at_k_macro'))}",
        f"- Precision@{metadata['parameters']['top_k']}（micro）：{_format_metric(metrics.get('precision_at_k_micro'))}",
        f"- MRR@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics['mrr_at_k_macro'])}",
        f"- nDCG@{metadata['parameters']['top_k']}（macro）：{_format_metric(metrics['ndcg_at_k_macro'])}",
        f"- 无答案误命中率：{_format_metric(metrics['no_answer_false_positive_rate'])}",
        f"- 过滤器正确性：{_format_metric(metrics['filter_correctness'])}",
        f"- P95 延迟：{metrics['p95_latency_ms']:.3f} ms",
        f"- Context budget：{'通过' if metrics['context_budget']['within_budget'] else '失败'}",
        f"- 只读副作用检查：{'通过' if metadata.get('side_effects', {}).get('clean') else '失败/未证明'}",
        f"- 低召回诊断：{metrics.get('diagnosis_distribution', {})}",
        "",
        "## Case 摘要",
        "",
    ]
    for case in safe_report["cases"]:
        first_path = case["ranked_paths"][0] if case["ranked_paths"] else "（无结果）"
        lines.append(f"- `{case['id']}`：首项 `{first_path}`；过滤器 {'通过' if case['filter_correct'] else '失败'}")
    gate = safe_report.get("gate")
    if isinstance(gate, Mapping):
        lines.extend(
            [
                "",
                "## Baseline gate",
                "",
                f"- 状态：{'通过' if gate.get('passed') else '未通过'}（{gate.get('status', 'unknown')}）",
            ]
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _sanitise_report_for_output(report: Mapping[str, Any]) -> dict[str, Any]:
    """在最终输出边界重新检查每个 case 的 pipeline 白名单。"""

    try:
        safe_report = json.loads(json.dumps(dict(report), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise RetrievalEvalError("report_invalid", "retrieval evaluation report must be JSON serializable") from exc
    if not isinstance(safe_report, dict):
        raise RetrievalEvalError("report_invalid", "retrieval evaluation report must be an object")
    cases = safe_report.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict):
                continue
            pipeline_value = case.get("pipeline", {})
            pipeline = dict(pipeline_value) if _has_pipeline_summary_shape(pipeline_value) else _project_pipeline(pipeline_value)
            case["pipeline"] = pipeline
            case["warnings"] = []
            case["warning_count"] = pipeline.get("warning_count", 0)
    return safe_report


def _calculate_metrics_by_k(
    ranked_paths: Sequence[str],
    relevant: Sequence[Relevance],
    *,
    top_k: int,
) -> dict[int, dict[str, float | int | None]]:
    ks = sorted({k for k in (1, 3, 5, 10) if k <= top_k} | {top_k})
    return {k: calculate_ranking_metrics(ranked_paths, relevant, top_k=k) for k in ks}


def _build_slice_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {"all": list(cases)}
    for case in cases:
        language = case.get("language")
        if isinstance(language, str) and language:
            buckets.setdefault(f"language:{language}", []).append(case)
        tags = case.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag:
                    buckets.setdefault(f"tag:{tag}", []).append(case)
        pipeline = case.get("pipeline")
        if isinstance(pipeline, Mapping):
            scope = pipeline.get("scope")
            if isinstance(scope, str) and scope:
                buckets.setdefault(f"scope:{scope}", []).append(case)

    result: dict[str, dict[str, Any]] = {}
    for name, bucket in sorted(buckets.items()):
        ranking_metrics: list[Mapping[str, Any]] = []
        for case in bucket:
            metrics = case.get("metrics")
            if isinstance(metrics, Mapping):
                ranking_metrics.append(metrics)
        answerable = [case for case in bucket if case.get("answerable") is True]
        no_answer = [case for case in bucket if case.get("answerable") is False]
        result[name] = {
            "case_count": len(bucket),
            "answerable_case_count": len(answerable),
            "recall_at_k_macro": _mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("recall")) is not None]),
            "precision_at_k_macro": _mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("precision")) is not None]),
            "mrr_at_k_macro": _mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("mrr")) is not None]),
            "ndcg_at_k_macro": _mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("ndcg")) is not None]),
            "filter_correctness": _mean_or_none([1.0 if case.get("filter_correct") else 0.0 for case in bucket]),
            "no_answer_false_positive_rate": _mean_or_none([1.0 if case.get("no_answer_false_positive") else 0.0 for case in no_answer]),
            "p95_latency_ms": percentile_95([float(value) for case in bucket for value in case.get("latency_ms", [])]),
        }
    return result


def _pipeline_summary(pipeline: object) -> dict[str, Any]:
    """把 public pipeline 投影为唯一的报告安全视图。"""

    return _project_pipeline(pipeline)


def _project_pipeline(pipeline: object) -> dict[str, Any]:
    if not isinstance(pipeline, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in ("authority", "corpus", "scope", "retrieval_mode", "ranking_version", "lexical_enabled"):
        value = pipeline.get(key)
        if isinstance(value, (str, bool)):
            summary[key] = value

    counters = pipeline.get("counters")
    if isinstance(counters, Mapping):
        safe_counters: dict[str, int | float] = {}
        for key in (
            "additional",
            "fts_hits",
            "graph_hits",
            "qualified_fts_hits",
            "qualified_hits",
            "queries",
            "raw_fts_hits",
            "raw_hits",
            "relaxed_fts_hits",
            "relaxed_hits",
            "returned",
            "selected",
            "vector_hits",
        ):
            value = counters.get(key)
            if type(value) is int or (isinstance(value, float) and math.isfinite(value)):
                safe_counters[key] = value
        if safe_counters:
            summary["counters"] = safe_counters

    fallback = pipeline.get("fallback")
    if isinstance(fallback, Mapping):
        safe_fallback: dict[str, Any] = {}
        level = fallback.get("level")
        if isinstance(level, str):
            safe_fallback["level"] = level
        reasons = fallback.get("reasons")
        if isinstance(reasons, list):
            safe_fallback["reason_count"] = len(reasons)
            safe_reasons = [
                reason.strip()[:120]
                for reason in reasons
                if isinstance(reason, str) and reason.strip() and _is_safe_reason_code(reason)
            ]
            if safe_reasons:
                safe_fallback["reasons"] = safe_reasons
        if safe_fallback:
            summary["fallback"] = safe_fallback

    coverage = pipeline.get("coverage")
    if isinstance(coverage, Mapping):
        safe_coverage: dict[str, Any] = {}
        triggered = coverage.get("triggered")
        if isinstance(triggered, bool):
            safe_coverage["triggered"] = triggered
        uncovered = coverage.get("uncovered_latin_terms")
        if isinstance(uncovered, list):
            safe_coverage["uncovered_latin_term_count"] = len(uncovered)
        if safe_coverage:
            summary["coverage"] = safe_coverage

    batch = pipeline.get("batch")
    if isinstance(batch, Mapping):
        safe_batch: dict[str, Any] = {}
        for key in ("status", "max_batch_items"):
            value = batch.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                safe_batch[key] = value
        for key, source_key in (
            ("entity_count", "entity_count"),
            ("ambiguous_count", "ambiguous"),
            ("failed_entity_count", "failed_entities"),
            ("pending_entity_count", "pending_entities"),
            ("unresolved_count", "unresolved"),
        ):
            value = batch.get(source_key)
            if isinstance(value, list):
                safe_batch[key] = len(value)
            elif type(value) is int:
                safe_batch[key] = value
        batch_counters = batch.get("counters")
        if isinstance(batch_counters, Mapping):
            safe_batch["counters"] = {
                str(key): value
                for key, value in batch_counters.items()
                if type(value) is int or (isinstance(value, float) and math.isfinite(value))
            }
        if safe_batch:
            summary["batch"] = safe_batch

    warnings = pipeline.get("warnings")
    vector_warnings = pipeline.get("stage_1_5_vector_warnings")
    warning_count = 0
    if isinstance(warnings, list):
        warning_count += len(warnings)
    if isinstance(vector_warnings, list):
        warning_count += len(vector_warnings)
    summary["warning_count"] = warning_count
    return summary


_PIPELINE_SUMMARY_KEYS = frozenset(
    {
        "authority",
        "corpus",
        "scope",
        "retrieval_mode",
        "ranking_version",
        "lexical_enabled",
        "counters",
        "fallback",
        "coverage",
        "batch",
        "warning_count",
    }
)


def _has_pipeline_summary_shape(value: object) -> bool:
    if not isinstance(value, Mapping) or not set(value).issubset(_PIPELINE_SUMMARY_KEYS):
        return False
    for key in ("authority", "corpus", "scope", "retrieval_mode", "ranking_version"):
        if key in value and not isinstance(value[key], str):
            return False
    if "lexical_enabled" in value and not isinstance(value["lexical_enabled"], bool):
        return False
    if "warning_count" in value and type(value["warning_count"]) is not int:
        return False
    counters = value.get("counters")
    if counters is not None and (
        not isinstance(counters, Mapping)
        or not set(counters).issubset(
            {
                "additional",
                "fts_hits",
                "graph_hits",
                "qualified_fts_hits",
                "qualified_hits",
                "queries",
                "raw_fts_hits",
                "raw_hits",
                "relaxed_fts_hits",
                "relaxed_hits",
                "returned",
                "selected",
                "vector_hits",
            }
        )
        or any(type(item) is not int and not (isinstance(item, float) and math.isfinite(item)) for item in counters.values())
    ):
        return False
    fallback = value.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, Mapping) or not set(fallback).issubset({"level", "reason_count", "reasons"}):
            return False
        if "level" in fallback and not isinstance(fallback["level"], str):
            return False
        if "reason_count" in fallback and type(fallback["reason_count"]) is not int:
            return False
        reasons = fallback.get("reasons")
        if reasons is not None and (
            not isinstance(reasons, list)
            or any(not isinstance(item, str) or not _is_safe_reason_code(item) for item in reasons)
        ):
            return False
    coverage = value.get("coverage")
    if coverage is not None:
        if not isinstance(coverage, Mapping) or not set(coverage).issubset({"triggered", "uncovered_latin_term_count"}):
            return False
        if "triggered" in coverage and not isinstance(coverage["triggered"], bool):
            return False
        if "uncovered_latin_term_count" in coverage and type(coverage["uncovered_latin_term_count"]) is not int:
            return False
    batch = value.get("batch")
    if batch is not None:
        batch_keys = {
            "status",
            "max_batch_items",
            "entity_count",
            "ambiguous_count",
            "failed_entity_count",
            "pending_entity_count",
            "unresolved_count",
            "counters",
        }
        if not isinstance(batch, Mapping) or not set(batch).issubset(batch_keys):
            return False
        for key in ("status", "max_batch_items"):
            if key in batch and (not isinstance(batch[key], (str, int)) or isinstance(batch[key], bool)):
                return False
        for key in batch_keys - {"status", "max_batch_items", "counters"}:
            if key in batch and type(batch[key]) is not int:
                return False
        batch_counters = batch.get("counters")
        if batch_counters is not None and (
            not isinstance(batch_counters, Mapping)
            or any(type(item) is not int and not (isinstance(item, float) and math.isfinite(item)) for item in batch_counters.values())
        ):
            return False
    return True


def _is_safe_reason_code(value: str) -> bool:
    return all(character.isalnum() or character in {"_", ":", ".", "-"} for character in value.strip())


def _result_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": item.get("path", ""),
        "score": item.get("score", 0.0),
        "scores": item.get("scores", {}),
        "source_kind": item.get("source_kind", ""),
    }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


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
        value = json.loads(json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise RetrievalEvalError("invalid_experiment_metadata", "experiment metadata must be JSON serializable") from exc
    if not isinstance(value, dict):
        raise RetrievalEvalError("invalid_experiment_metadata", "experiment metadata must be an object")
    return value

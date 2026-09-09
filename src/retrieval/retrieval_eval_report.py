"""检索评测的指标、报告投影和输出 owner。

这里的核心函数只消费已归一化的 case/result 视图，不读取 vault，也不调用
查询管线。文件写入仅限于报告输出目录，输入结果会在输出边界再次脱敏。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from retrieval.retrieval_eval_dataset import Relevance, RetrievalEvalError
from retrieval.query_quality_policy import SCORE_FAMILIES, is_gate_reason_code, is_score_family


__all__ = [
    "assemble_quality_gate_report",
    "build_slice_metrics",
    "calculate_metrics_by_k",
    "calculate_ranking_metrics",
    "calculate_quality_gate_metrics",
    "evaluate_retrieval_gate",
    "mean_or_none",
    "median",
    "normalise_experiment_metadata",
    "pipeline_summary",
    "quality_gate_config_hash",
    "quality_gate_report_identity",
    "percentile_95",
    "result_summary",
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
    runtime = metadata.get("runtime_provenance")
    if isinstance(runtime, Mapping):
        safe_runtime: dict[str, Any] = {}
        for name in ("package_version", "revision", "revision_source"):
            value = runtime.get(name)
            if isinstance(value, str) and value and _safe_identity_token(value):
                safe_runtime[name] = value
        dirty = runtime.get("dirty")
        if isinstance(dirty, bool) or dirty is None:
            safe_runtime["dirty"] = dirty
        if safe_runtime:
            identity["runtime_provenance"] = safe_runtime

    quality_gate = metadata.get("quality_gate")
    if isinstance(quality_gate, Mapping):
        policy_version = quality_gate.get("policy_version")
        config_hash = quality_gate.get("config_hash")
        if isinstance(policy_version, str) and _safe_identity_token(policy_version):
            identity["gate_policy_version"] = policy_version
        if isinstance(config_hash, str) and _safe_identity_token(config_hash):
            identity["gate_config_hash"] = config_hash
    for name in ("gate_policy_version", "gate_config_hash"):
        value = metadata.get(name)
        if isinstance(value, str) and _safe_identity_token(value):
            identity[name] = value
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
    identity_pairs = [
        ("dataset_id", candidate_identity.get("dataset_id"), baseline_identity.get("dataset_id")),
        ("dataset_revision", candidate_identity.get("dataset_revision"), baseline_identity.get("dataset_revision")),
        (
            "vault_fingerprint",
            candidate_fingerprint.get("value") if isinstance(candidate_fingerprint, Mapping) else None,
            baseline_fingerprint.get("value") if isinstance(baseline_fingerprint, Mapping) else None,
        ),
        ("ranking_version", candidate_identity.get("ranking_version"), baseline_identity.get("ranking_version")),
        ("runtime_provenance", candidate_identity.get("runtime_provenance"), baseline_identity.get("runtime_provenance")),
        ("gate_policy_version", candidate_identity.get("gate_policy_version"), baseline_identity.get("gate_policy_version")),
        ("gate_config_hash", candidate_identity.get("gate_config_hash"), baseline_identity.get("gate_config_hash")),
    ]
    candidate_gate_declared = isinstance(candidate_metadata.get("quality_gate"), Mapping) or any(
        name in candidate_metadata for name in ("gate_policy_version", "gate_config_hash")
    )
    baseline_gate_declared = isinstance(baseline_metadata.get("quality_gate"), Mapping) or any(
        name in baseline_metadata for name in ("gate_policy_version", "gate_config_hash")
    )
    identity_mismatch = False
    for name, candidate_value, baseline_value in identity_pairs:
        # Legacy reports predate the gate/runtime identity fields. Preserve
        # their comparison compatibility when both sides omit a new field;
        # a one-sided or unequal value is still unproven.
        if name == "runtime_provenance" and candidate_value is None and baseline_value is None:
            continue
        if name in {"gate_policy_version", "gate_config_hash"} and candidate_value is None and baseline_value is None:
            if not (candidate_gate_declared or baseline_gate_declared):
                continue
        passed = bool(candidate_value) and candidate_value == baseline_value
        add_check(
            name,
            passed,
            actual=candidate_value,
            expected=baseline_value,
            reason="baseline_identity_mismatch" if candidate_value != baseline_value else None,
        )
        identity_mismatch = identity_mismatch or not passed

    if identity_mismatch:
        return {
            "passed": False,
            "status": "unproven",
            "reason": "baseline_identity_mismatch",
            "checks": checks,
        }

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
    gate_metrics = metrics.get("quality_gate")
    if isinstance(gate_metrics, Mapping):
        false_suppression = gate_metrics.get("false_suppression")
        would_accept = gate_metrics.get("would_accept")
        rank_churn = gate_metrics.get("rank_churn")
        lines.extend(
            [
                "## 质量门禁观测",
                "",
                f"- 状态：`{gate_metrics.get('status', 'unknown')}`",
                f"- False suppression：{_format_metric(false_suppression.get('rate') if isinstance(false_suppression, Mapping) else None)}",
                f"- No-answer would_accept：{_format_metric(would_accept.get('rate') if isinstance(would_accept, Mapping) else None)}",
                f"- Rank churn top-1 flip：{_format_metric(rank_churn.get('top1_flip_rate') if isinstance(rank_churn, Mapping) else None)}",
                f"- Rank churn top-k Jaccard：{_format_metric(rank_churn.get('top_k_jaccard') if isinstance(rank_churn, Mapping) else None)}",
                f"- Reason code 分桶：{gate_metrics.get('reason_counts', {})}",
                f"- Score family 分桶：{gate_metrics.get('score_family_counts', {})}",
                "",
            ]
        )
    for case in safe_report["cases"]:
        case_metrics = case.get("metrics") if isinstance(case.get("metrics"), Mapping) else {}
        lines.append(
            f"- `{case['id']}`：过滤器 {'通过' if case.get('filter_correct') else '失败'}；"
            f"Recall {_format_metric(case_metrics.get('recall') if isinstance(case_metrics, Mapping) else None)}"
        )
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
        safe_cases: list[dict[str, Any]] = []
        for case in cases:
            if not isinstance(case, dict):
                continue
            pipeline_value = case.get("pipeline", {})
            pipeline = dict(pipeline_value) if _has_pipeline_summary_shape(pipeline_value) else _project_pipeline(pipeline_value)
            safe_case: dict[str, Any] = {}
            case_id = case.get("id")
            if not isinstance(case_id, str) or not _safe_identity_token(case_id):
                continue
            safe_case["id"] = case_id
            for key in ("answerable", "no_answer_false_positive", "filter_correct"):
                value = case.get(key)
                if type(value) is bool:
                    safe_case[key] = value
            for key in ("language", "scope"):
                value = case.get(key)
                if _safe_identity_token(value):
                    safe_case[key] = value
            top_score = _finite_number(case.get("top_score"))
            if top_score is not None:
                safe_case["top_score"] = top_score
            tags = case.get("tags")
            if isinstance(tags, list):
                safe_case["tags"] = [tag for tag in tags if _safe_identity_token(tag)][:32]
            for key in ("metrics", "metrics_by_k", "context_budget"):
                value = case.get(key)
                if isinstance(value, Mapping):
                    safe_case[key] = _project_safe_output_value(value)
            safe_case["pipeline"] = pipeline
            safe_case["warnings"] = []
            safe_case["warning_count"] = pipeline.get("warning_count", 0)
            safe_cases.append(safe_case)
        safe_report["cases"] = safe_cases
    return safe_report


def _project_safe_output_value(value: object, *, depth: int = 0) -> Any:
    """递归保留指标所需的有限标量，丢弃字符串形式的路径/正文。"""

    if depth > 6:
        return None
    if value is None or type(value) is bool:
        return value
    if _finite_number(value) is not None:
        return value
    if isinstance(value, str):
        return value if _safe_identity_token(value) else None
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        blocked_keys = frozenset({"body", "content", "excerpt", "path", "query", "question", "text"})
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not _safe_identity_token(key) or str(key) in blocked_keys:
                continue
            projected_item = _project_safe_output_value(item, depth=depth + 1)
            if isinstance(item, str) and projected_item is None:
                continue
            projected[str(key)] = projected_item
        return projected
    if isinstance(value, list):
        return [_project_safe_output_value(item, depth=depth + 1) for item in value[:64]]
    return None


def calculate_metrics_by_k(
    ranked_paths: Sequence[str],
    relevant: Sequence[Relevance],
    *,
    top_k: int,
) -> dict[int, dict[str, float | int | None]]:
    ks = sorted({k for k in (1, 3, 5, 10) if k <= top_k} | {top_k})
    return {k: calculate_ranking_metrics(ranked_paths, relevant, top_k=k) for k in ks}


def build_slice_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
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
            fallback = pipeline.get("fallback")
            if isinstance(fallback, Mapping):
                level = fallback.get("level")
                if isinstance(level, str) and level:
                    buckets.setdefault(f"fallback:{level}", []).append(case)

    gate_enabled = any(_quality_gate_observation(case) is not None for case in cases)
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
            "recall_at_k_macro": mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("recall")) is not None]),
            "precision_at_k_macro": mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("precision")) is not None]),
            "mrr_at_k_macro": mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("mrr")) is not None]),
            "ndcg_at_k_macro": mean_or_none([float(value) for metrics in ranking_metrics if (value := metrics.get("ndcg")) is not None]),
            "filter_correctness": mean_or_none([1.0 if case.get("filter_correct") else 0.0 for case in bucket]),
            "no_answer_false_positive_rate": mean_or_none([1.0 if case.get("no_answer_false_positive") else 0.0 for case in no_answer]),
            "p95_latency_ms": percentile_95([float(value) for case in bucket for value in case.get("latency_ms", [])]),
        }
        gate_metrics = calculate_quality_gate_metrics(bucket, gate_enabled=gate_enabled)
        result[name]["quality_gate"] = {
            "status": gate_metrics["status"],
            "false_suppression_rate": gate_metrics["false_suppression_rate"],
            "would_accept_rate": gate_metrics["would_accept_rate"],
            "top1_flip_rate": gate_metrics["rank_churn"]["top1_flip_rate"],
            "top_k_jaccard": gate_metrics["rank_churn"]["top_k_jaccard"],
            "reason_counts": gate_metrics["reason_counts"],
            "score_family_counts": gate_metrics["score_family_counts"],
        }
    return result


_GATE_IDENTITY_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_GATE_MAX_BUCKETS = 64


def _safe_identity_token(value: object) -> bool:
    return isinstance(value, str) and bool(_GATE_IDENTITY_TOKEN.fullmatch(value))


def _bounded_gate_counts(value: object, *, kind: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    validator = is_gate_reason_code if kind == "reason" else is_score_family
    counts: dict[str, int] = {}
    for key, raw_count in value.items():
        if validator(key) and type(raw_count) is int and raw_count >= 0:
            counts[str(key)] = raw_count
    return dict(sorted(counts.items())[:_GATE_MAX_BUCKETS])


def _quality_gate_observation(case: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("quality_gate_observation", "gate_observation", "shadow_observation"):
        value = case.get(key)
        if isinstance(value, Mapping):
            return value
    pipeline = case.get("pipeline")
    if isinstance(pipeline, Mapping):
        value = pipeline.get("quality_gate")
        if isinstance(value, Mapping):
            return value
    return None


def _case_paths(case: Mapping[str, Any]) -> list[str]:
    raw_paths = case.get("ranked_paths")
    if not isinstance(raw_paths, list):
        summaries = case.get("result_summary")
        raw_paths = [item.get("path") for item in summaries if isinstance(item, Mapping)] if isinstance(summaries, list) else []
    return list(dict.fromkeys(path for path in raw_paths if isinstance(path, str) and path))


def _case_grades(case: Mapping[str, Any]) -> dict[str, int] | None:
    raw_relevant = case.get("relevant", case.get("relevance"))
    if not isinstance(raw_relevant, (list, tuple)):
        return None
    grades: dict[str, int] = {}
    for item in raw_relevant:
        if not isinstance(item, Mapping):
            return None
        path = item.get("path")
        grade = item.get("grade")
        if not isinstance(path, str) or not path or type(grade) is not int or grade not in {1, 2, 3}:
            return None
        grades[path] = grade
    return grades


def _decision_mapping(observation: Mapping[str, Any], baseline_paths: Sequence[str]) -> dict[str, Mapping[str, Any]] | None:
    """Resolve optional in-memory page decisions without persisting their paths."""

    decisions: dict[str, Mapping[str, Any]] = {}

    def add(path: object, accepted: object, raw: Mapping[str, Any] | None = None) -> None:
        if not isinstance(path, str) or not path or not isinstance(accepted, bool):
            return
        decisions[path] = {
            "accepted": accepted,
            "reason_code": raw.get("reason_code", raw.get("reason")) if raw else None,
            "score_family": raw.get("score_family", raw.get("family")) if raw else None,
        }

    raw_decisions = observation.get("decisions", observation.get("candidate_decisions"))
    if isinstance(raw_decisions, Mapping):
        for path, raw in raw_decisions.items():
            if isinstance(raw, Mapping):
                add(path, raw.get("accepted"), raw)
            else:
                add(path, raw)
    elif isinstance(raw_decisions, (list, tuple)):
        for index, raw in enumerate(raw_decisions):
            if not isinstance(raw, Mapping):
                continue
            path = raw.get("path", raw.get("page_path"))
            if path is None:
                rank = raw.get("rank", raw.get("position"))
                if type(rank) is int and 1 <= rank <= len(baseline_paths):
                    path = baseline_paths[rank - 1]
            add(path, raw.get("accepted"), raw)

    accepted_paths = observation.get("accepted_paths", observation.get("accepted_pages"))
    if isinstance(accepted_paths, (list, tuple)):
        for path in accepted_paths:
            add(path, True)
    rejected_paths = observation.get("rejected_paths", observation.get("rejected_pages"))
    if isinstance(rejected_paths, (list, tuple)):
        for path in rejected_paths:
            add(path, False)

    if baseline_paths and all(path in decisions for path in baseline_paths):
        return {path: decisions[path] for path in baseline_paths}
    if not baseline_paths and isinstance(observation, Mapping):
        return {}

    candidate_count = observation.get("candidate_count")
    accepted_count = observation.get("accepted_count")
    rejected_count = observation.get("rejected_count")
    if observation.get("fail_open") is True or (
        type(candidate_count) is int
        and type(accepted_count) is int
        and type(rejected_count) is int
        and candidate_count >= 0
        and candidate_count >= len(baseline_paths)
        and accepted_count == candidate_count
        and rejected_count == 0
    ):
        return {path: {"accepted": True, "reason_code": None, "score_family": None} for path in baseline_paths}
    return None


def _gate_observation_counts(observation: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    reason_counts = _bounded_gate_counts(observation.get("reason_counts"), kind="reason")
    family_counts = _bounded_gate_counts(observation.get("score_family_counts"), kind="family")
    decisions = observation.get("decisions", observation.get("candidate_decisions"))
    if isinstance(decisions, (list, tuple)):
        use_decision_reasons = not reason_counts
        use_decision_families = not family_counts
        for item in decisions:
            if not isinstance(item, Mapping):
                continue
            reason = item.get("reason_code", item.get("reason"))
            family = item.get("score_family", item.get("family"))
            if use_decision_reasons and is_gate_reason_code(reason):
                reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
            if use_decision_families and is_score_family(family):
                family_counts[str(family)] = family_counts.get(str(family), 0) + 1
    return dict(sorted(reason_counts.items())[:_GATE_MAX_BUCKETS]), dict(sorted(family_counts.items())[:_GATE_MAX_BUCKETS])


def quality_gate_config_hash(config: Mapping[str, Any]) -> str:
    """Return a deterministic, path-free hash for gate configuration identity."""

    canonical: dict[str, Any] = {}
    for key in ("mode", "policy_version", "calibration_revision"):
        value = config.get(key)
        if isinstance(value, str) and _safe_identity_token(value):
            canonical[key] = value
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def quality_gate_report_identity(
    cases: Sequence[Mapping[str, Any]],
    *,
    configured: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the report-level gate identity from bounded shadow observations."""

    observations = [observation for case in cases if (observation := _quality_gate_observation(case)) is not None]
    configured_mode = configured.get("mode") if isinstance(configured, Mapping) else None
    if not observations and configured_mode not in {"shadow", "enforce"}:
        return {"status": "not_enabled", "enabled": False}
    policies = {
        str(value.get("policy_version"))
        for value in observations
        if isinstance(value.get("policy_version"), str) and _safe_identity_token(value.get("policy_version"))
    }
    modes = {
        str(value.get("mode"))
        for value in observations
        if value.get("mode") in {"shadow", "enforce"}
    }
    revisions = {
        str(value.get("calibration_revision"))
        for value in observations
        if isinstance(value.get("calibration_revision"), str) and _safe_identity_token(value.get("calibration_revision"))
    }
    if isinstance(configured, Mapping):
        if isinstance(configured.get("policy_version"), str) and _safe_identity_token(configured.get("policy_version")):
            policies.add(str(configured["policy_version"]))
        if configured.get("mode") in {"shadow", "enforce"}:
            modes.add(str(configured["mode"]))
        if isinstance(configured.get("calibration_revision"), str) and _safe_identity_token(configured.get("calibration_revision")):
            revisions.add(str(configured["calibration_revision"]))
    policy_version = next(iter(policies)) if len(policies) == 1 else None
    mode = next(iter(modes)) if len(modes) == 1 else None
    calibration_revision = next(iter(revisions)) if len(revisions) == 1 else None
    explicit_hashes = {
        str(value.get("config_hash"))
        for value in observations
        if isinstance(value.get("config_hash"), str) and _safe_identity_token(value.get("config_hash"))
    }
    if isinstance(configured, Mapping) and isinstance(configured.get("config_hash"), str) and _safe_identity_token(configured.get("config_hash")):
        explicit_hashes.add(str(configured["config_hash"]))
    config_hash = next(iter(explicit_hashes)) if len(explicit_hashes) == 1 else None
    if config_hash is None and policy_version is not None and mode is not None and len(policies) == len(modes) == 1 and len(revisions) <= 1:
        config_hash = quality_gate_config_hash(
            {"mode": mode, "policy_version": policy_version, "calibration_revision": calibration_revision or ""}
        )
    observation_unavailable = any(observation.get("available") is False for observation in observations)
    complete = (
        policy_version is not None
        and config_hash is not None
        and len(policies) <= 1
        and len(modes) <= 1
        and len(revisions) <= 1
        and not observation_unavailable
    )
    result: dict[str, Any] = {
        "status": "proven" if complete else "unproven",
        "enabled": True,
    }
    if mode is not None:
        result["mode"] = mode
    if policy_version is not None:
        result["gate_policy_version"] = policy_version
    if config_hash is not None:
        result["gate_config_hash"] = config_hash
    if calibration_revision is not None:
        result["calibration_revision"] = calibration_revision
    return result


def calculate_quality_gate_metrics(
    cases: Sequence[Mapping[str, Any]],
    *,
    top_k: int = 10,
    gate_enabled: bool | None = None,
) -> dict[str, Any]:
    """Compare page-deduped public results with in-memory shadow decisions."""

    observations = [_quality_gate_observation(case) for case in cases]
    enabled = bool(gate_enabled) if gate_enabled is not None else any(observation is not None for observation in observations)
    if not enabled:
        return {
            "status": "not_enabled",
            "enabled": False,
            "false_suppression": None,
            "false_suppression_rate": None,
            "would_accept": None,
            "would_accept_rate": None,
            "rank_churn": {"top1_flip_rate": None, "top_k_jaccard": None, "compared_case_count": 0, "top_k": top_k},
            "reason_counts": {},
            "score_family_counts": {},
            "gate_reason_counts": {},
            "gate_score_family_counts": {},
        }

    unavailable = False
    acceptable_total = acceptable_suppressed = 0
    grade_one_total = grade_one_suppressed = 0
    no_answer_total = no_answer_accepted = 0
    top1_flips = 0
    jaccards: list[float] = []
    compared_cases = 0
    reason_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}

    for case, observation in zip(cases, observations):
        baseline_paths = _case_paths(case)
        if observation is None:
            unavailable = True
            decisions: dict[str, Mapping[str, Any]] | None = None
        else:
            if observation.get("available") is False:
                unavailable = True
            decisions = _decision_mapping(observation, baseline_paths)
            if decisions is None:
                unavailable = True
        if observation is not None:
            case_reasons, case_families = _gate_observation_counts(observation)
            for key, value in case_reasons.items():
                reason_counts[key] = reason_counts.get(key, 0) + value
            for key, value in case_families.items():
                family_counts[key] = family_counts.get(key, 0) + value
        if decisions is None:
            continue

        projected_paths = [path for path in baseline_paths if decisions.get(path, {}).get("accepted") is True]
        grades = _case_grades(case)
        if grades is None and case.get("answerable") is True:
            unavailable = True
        if grades is not None:
            for path in baseline_paths:
                grade = grades.get(path)
                if grade is None:
                    continue
                accepted = decisions[path].get("accepted") is True
                if grade >= 2:
                    acceptable_total += 1
                    acceptable_suppressed += int(not accepted)
                elif grade == 1:
                    grade_one_total += 1
                    grade_one_suppressed += int(not accepted)

        if case.get("answerable") is False:
            no_answer_total += 1
            no_answer_accepted += int(bool(projected_paths))

        if baseline_paths:
            compared_cases += 1
            top1_flips += int(not projected_paths or projected_paths[0] != baseline_paths[0])
        baseline_top_k = set(baseline_paths[:top_k])
        projected_top_k = set(projected_paths[:top_k])
        union = baseline_top_k | projected_top_k
        jaccards.append(len(baseline_top_k & projected_top_k) / len(union) if union else 1.0)

    status = "unproven" if unavailable else "proven"
    false_status = "unproven" if unavailable or acceptable_total == 0 else "proven"
    would_status = "unproven" if unavailable or no_answer_total == 0 else "proven"
    churn_status = "unproven" if unavailable or not jaccards else "proven"
    false_rate = acceptable_suppressed / acceptable_total if false_status == "proven" else None
    grade_one_rate = grade_one_suppressed / grade_one_total if grade_one_total else None
    would_rate = no_answer_accepted / no_answer_total if would_status == "proven" else None
    top1_rate = top1_flips / compared_cases if churn_status == "proven" and compared_cases else None
    jaccard = statistics.fmean(jaccards) if churn_status == "proven" else None
    false_suppression = {
        "rate": false_rate,
        "suppressed_acceptable_count": acceptable_suppressed,
        "acceptable_page_count": acceptable_total,
        "grade_1_rate": grade_one_rate,
        "grade_1_suppressed_count": grade_one_suppressed,
        "grade_1_page_count": grade_one_total,
        "status": false_status,
    }
    would_accept = {
        "rate": would_rate,
        "accepted_case_count": no_answer_accepted,
        "no_answer_case_count": no_answer_total,
        "status": would_status,
    }
    rank_churn = {
        "top1_flip_rate": top1_rate,
        "top_k_jaccard": jaccard,
        "top1_flip_count": top1_flips,
        "compared_case_count": compared_cases,
        "top_k": top_k,
        "status": churn_status,
    }
    bounded_reasons = dict(sorted(reason_counts.items())[:_GATE_MAX_BUCKETS])
    bounded_families = dict(sorted(family_counts.items())[:_GATE_MAX_BUCKETS])
    return {
        "status": status,
        "enabled": True,
        "false_suppression": false_suppression,
        "false_suppression_rate": false_rate,
        "would_accept": would_accept,
        "would_accept_rate": would_rate,
        "rank_churn": rank_churn,
        "reason_counts": bounded_reasons,
        "score_family_counts": bounded_families,
        "gate_reason_counts": bounded_reasons,
        "gate_score_family_counts": bounded_families,
    }


def assemble_quality_gate_report(
    cases: Sequence[Mapping[str, Any]],
    *,
    top_k: int = 10,
    configured: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the single report-owned quality-gate schema projection."""

    configured_gate = (
        dict(configured)
        if isinstance(configured, Mapping) and configured.get("mode") in {"shadow", "enforce"}
        else None
    )
    gate_metrics = calculate_quality_gate_metrics(
        cases,
        top_k=top_k,
        gate_enabled=configured_gate is not None
        or any(case.get("quality_gate_observation") is not None for case in cases),
    )
    gate_identity = quality_gate_report_identity(cases, configured=configured_gate)
    metadata: dict[str, Any] = {
        "status": gate_identity["status"],
        "enabled": gate_identity["enabled"],
    }
    for key in ("mode", "gate_policy_version", "gate_config_hash", "calibration_revision"):
        if key in gate_identity:
            metadata[key] = gate_identity[key]

    return {
        "metrics": {
            "quality_gate": gate_metrics,
            "false_suppression": gate_metrics["false_suppression"],
            "false_suppression_rate": gate_metrics["false_suppression_rate"],
            "would_accept": gate_metrics["would_accept"],
            "would_accept_rate": gate_metrics["would_accept_rate"],
            "rank_churn": gate_metrics["rank_churn"],
            "gate_reason_counts": gate_metrics["reason_counts"],
            "gate_score_family_counts": gate_metrics["score_family_counts"],
        },
        "identity": gate_identity,
        "metadata": metadata,
    }


def pipeline_summary(pipeline: object) -> dict[str, Any]:
    """把 public pipeline 投影为唯一的报告安全视图。"""

    return _project_pipeline(pipeline)


_QUALITY_GATE_SUMMARY_KEYS = frozenset(
    {
        "policy_version",
        "calibration_revision",
        "config_hash",
        "mode",
        "status",
        "candidate_count",
        "accepted_count",
        "rejected_count",
        "score_family_counts",
        "reason_counts",
        "selection_counts",
        "low_sample_buckets",
        "fail_open",
    }
)
_QUALITY_GATE_COUNT_KEYS = ("candidate_count", "accepted_count", "rejected_count")
_REQUIRED_QUALITY_GATE_KEYS = frozenset(
    {
        "policy_version",
        "mode",
        "status",
        "candidate_count",
        "accepted_count",
        "rejected_count",
        "score_family_counts",
        "reason_counts",
        "low_sample_buckets",
        "fail_open",
    }
)


def _safe_quality_gate_token(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    if not all(character.isalnum() or character in {"_", ".", ":", "-"} for character in value):
        return None
    return value


def _safe_quality_gate_bucket(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 160:
        return None
    if not all(character.isalnum() or character in {"_", ".", ":", "-", "|", "*"} for character in value):
        return None
    return value


def _project_quality_gate(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in ("policy_version", "calibration_revision", "config_hash", "status"):
        token = _safe_quality_gate_token(value.get(key))
        if token is not None:
            summary[key] = token
    mode = value.get("mode")
    if mode in {"shadow", "enforce"}:
        summary["mode"] = mode
    for key in _QUALITY_GATE_COUNT_KEYS:
        count = value.get(key)
        if type(count) is int and count >= 0:
            summary[key] = count

    family_counts = value.get("score_family_counts")
    if isinstance(family_counts, Mapping):
        summary["score_family_counts"] = {
            str(key): count
            for key, count in family_counts.items()
            if key in SCORE_FAMILIES and type(count) is int and count >= 0
        }

    reason_counts = value.get("reason_counts")
    if isinstance(reason_counts, Mapping):
        summary["reason_counts"] = {
            str(key): count
            for key, count in reason_counts.items()
            if is_gate_reason_code(key) and type(count) is int and count >= 0
        }

    selection_counts = value.get("selection_counts")
    if isinstance(selection_counts, Mapping):
        summary["selection_counts"] = {
            str(key): count
            for key, count in selection_counts.items()
            if key in {"exact", "backoff", "fail_open"} and type(count) is int and count >= 0
        }

    low_sample_buckets = value.get("low_sample_buckets")
    if isinstance(low_sample_buckets, list):
        summary["low_sample_buckets"] = [
            token
            for item in low_sample_buckets[:32]
            if (token := _safe_quality_gate_bucket(item)) is not None
        ]
    fail_open = value.get("fail_open")
    if isinstance(fail_open, bool):
        summary["fail_open"] = fail_open
    return summary


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

    quality_gate = _project_quality_gate(pipeline.get("quality_gate"))
    if quality_gate:
        summary["quality_gate"] = quality_gate

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
        "quality_gate",
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
    quality_gate = value.get("quality_gate")
    if quality_gate is not None:
        if not isinstance(quality_gate, Mapping) or not set(quality_gate).issubset(_QUALITY_GATE_SUMMARY_KEYS):
            return False
        required_quality_gate_keys = _REQUIRED_QUALITY_GATE_KEYS
        if not required_quality_gate_keys.issubset(quality_gate):
            return False
        for key in ("policy_version", "status"):
            if _safe_quality_gate_token(quality_gate.get(key)) is None:
                return False
        if any(
            key in quality_gate and _safe_quality_gate_token(quality_gate.get(key)) is None
            for key in ("calibration_revision", "config_hash")
        ):
            return False
        if quality_gate.get("mode") not in {"shadow", "enforce"}:
            return False
        if any(type(quality_gate.get(key)) is not int or quality_gate[key] < 0 for key in _QUALITY_GATE_COUNT_KEYS):
            return False
        family_counts = quality_gate.get("score_family_counts")
        if not isinstance(family_counts, Mapping) or any(
            not is_score_family(key) or type(item) is not int or item < 0
            for key, item in family_counts.items()
        ):
            return False
        reason_counts = quality_gate.get("reason_counts")
        if not isinstance(reason_counts, Mapping) or any(
            not is_gate_reason_code(key) or type(item) is not int or item < 0
            for key, item in reason_counts.items()
        ):
            return False
        selection_counts = quality_gate.get("selection_counts")
        if selection_counts is not None and (
            not isinstance(selection_counts, Mapping)
            or any(
                key not in {"exact", "backoff", "fail_open"} or type(item) is not int or item < 0
                for key, item in selection_counts.items()
            )
        ):
            return False
        low_sample_buckets = quality_gate.get("low_sample_buckets")
        if not isinstance(low_sample_buckets, list) or len(low_sample_buckets) > 32 or any(
            _safe_quality_gate_bucket(item) is None for item in low_sample_buckets
        ):
            return False
        if not isinstance(quality_gate.get("fail_open"), bool):
            return False
    return True


def _is_safe_reason_code(value: str) -> bool:
    return all(character.isalnum() or character in {"_", ":", ".", "-"} for character in value.strip())


def result_summary(item: Mapping[str, Any]) -> dict[str, Any]:
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


def mean_or_none(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: Sequence[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _format_metric(value: object) -> str:
    number = _finite_number(value)
    return "N/A" if number is None else f"{number:.4f}"


def normalise_experiment_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
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

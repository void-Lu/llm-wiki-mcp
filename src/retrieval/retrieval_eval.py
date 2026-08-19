"""面向 public wiki query 的确定性、只读检索评测编排。

数据集 schema 与指标/报告分别由 ``retrieval_eval_dataset`` 和
``retrieval_eval_report`` 拥有；本模块只保留运行时快照、两个真实查询
adapter、统一 query service，以及跨层评测编排。这样 engine/MCP 的 envelope
检查停留在 adapter seam，runner 只消费统一的 case 视图。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from retrieval.metadata_filters import page_matches_filters, path_matches_prefix
from retrieval.query_pipeline import run_query_v2
from retrieval.query_recall_policy import DEFAULT_TOP_K, RANKING_POLICY_VERSION
from retrieval.query_shared import QueryFilters
from retrieval.query_telemetry import read_event_count
from retrieval.retrieval_eval_dataset import (
    RETRIEVAL_EVAL_SCHEMA_VERSION,
    EvaluationFilterContract,
    Relevance,
    RetrievalEvalCase,
    RetrievalEvalDataset,
    RetrievalEvalError,
    RetrievalEvalManifest,
    load_retrieval_dataset,
    normalize_evaluation_filter_contract,
    parse_evaluation_case,
    parse_evaluation_filters,
    parse_evaluation_manifest,
)
from retrieval.retrieval_eval_report import (
    assemble_quality_gate_report,
    build_slice_metrics,
    calculate_metrics_by_k,
    calculate_ranking_metrics,
    evaluate_retrieval_gate,
    mean_or_none,
    median,
    normalise_experiment_metadata,
    pipeline_summary,
    percentile_95,
    result_summary,
    safe_report_identity,
    write_retrieval_eval_report,
)
from retrieval.retrieval_index import RetrievalIndexStore
from retrieval.vector_index import parse_vector_settings
from runtime.runtime_config import EmbeddingSettings, QualityGateSettings, TelemetrySettings, VaultSettings
from runtime.runtime_provenance import RUNTIME_PROVENANCE
from wiki.wiki_paths import filesystem_path


__all__ = [
    "EngineQueryAdapter",
    "EvaluationFilterContract",
    "EvaluationQueryRequest",
    "EvaluationQueryService",
    "EvaluationRuntimeSnapshot",
    "McpEntryAdapter",
    "McpQueryAdapter",
    "RETRIEVAL_EVAL_SCHEMA_VERSION",
    "RetrievalEvalCase",
    "RetrievalEvalDataset",
    "RetrievalEvalError",
    "RetrievalEvalManifest",
    "Relevance",
    "calculate_ranking_metrics",
    "default_mcp_entry_adapter",
    "evaluate_retrieval_gate",
    "load_retrieval_dataset",
    "normalize_evaluation_filter_contract",
    "parse_evaluation_case",
    "parse_evaluation_filters",
    "parse_evaluation_manifest",
    "percentile_95",
    "run_retrieval_evaluation",
    "safe_report_identity",
    "validate_dataset_paths",
    "vault_fingerprint",
    "write_retrieval_eval_report",
]


@dataclass(frozen=True)
class McpEntryAdapter:
    """MCP public entrypoint 所需的最小、可注入 seam。"""

    resolve: Callable[..., Any]
    snapshot: Callable[[Any], AbstractContextManager[None]]
    query: Callable[..., Any] | None = None


def default_mcp_entry_adapter() -> McpEntryAdapter:
    """构造真实 MCP adapter；保持 server import 在惰性 seam 内。"""

    import app.server as server_module

    return McpEntryAdapter(
        resolve=server_module.resolve_tool_vault,
        snapshot=server_module.tool_runtime_snapshot,
        query=server_module.wiki_query,
    )


def _resolve_mcp_vault(adapter: McpEntryAdapter, vault_root: str | Path) -> Any:
    """Resolve the MCP vault through the production keyword-only seam."""

    return adapter.resolve(vault_root=str(vault_root))


@dataclass(frozen=True)
class EvaluationRuntimeSnapshot:
    """单次评测使用的不可变 runtime/vault settings。"""

    root: Path
    logical_name: str
    settings: VaultSettings
    adapter: McpEntryAdapter | None = None
    mcp_resolution: Any | None = None

    @classmethod
    def lexical_only(
        cls,
        vault_root: str | Path,
        *,
        quality_gate: QualityGateSettings | None = None,
    ) -> "EvaluationRuntimeSnapshot":
        root = filesystem_path(vault_root)
        settings = replace(
            VaultSettings(name=root.name, root=root),
            telemetry=TelemetrySettings(enabled=False),
        )
        if quality_gate is not None:
            settings = replace(settings, quality_gate=quality_gate)
        return cls(root=root, logical_name=root.name, settings=settings)

    @classmethod
    def from_mcp_vault(
        cls,
        vault_root: str | Path,
        *,
        adapter: McpEntryAdapter | None = None,
        quality_gate: QualityGateSettings | None = None,
    ) -> "EvaluationRuntimeSnapshot":
        """只解析一次 MCP settings，并冻结本地 telemetry-off 副本。"""

        active_adapter = adapter or default_mcp_entry_adapter()
        resolution = _resolve_mcp_vault(active_adapter, vault_root)
        settings = resolution.resolved.settings
        if quality_gate is not None:
            settings = replace(settings, quality_gate=quality_gate)
        if not settings.retrieval.lexical_enabled or settings.retrieval.embedding.enabled:
            raise RetrievalEvalError("mcp_not_lexical", "the selected vault MCP configuration is not lexical-only")
        frozen_settings = replace(settings, telemetry=TelemetrySettings(enabled=False))
        frozen_resolution = replace(
            resolution,
            resolved=replace(resolution.resolved, settings=frozen_settings),
        )
        return cls(
            root=resolution.root,
            logical_name=resolution.logical_name,
            settings=frozen_settings,
            adapter=active_adapter,
            mcp_resolution=frozen_resolution,
        )

    def tool_resolution(self) -> Any:
        """构造 telemetry-off resolution，不接触进程级 registry。"""

        if self.adapter is None:
            raise RetrievalEvalError("mcp_adapter_missing", "MCP evaluation requires an injected adapter")
        resolution = self.mcp_resolution
        if resolution is None:
            raise RetrievalEvalError("mcp_resolution_missing", "MCP evaluation resolution is not available")
        try:
            return replace(
                resolution,
                resolved=replace(resolution.resolved, settings=self.settings),
            )
        except (AttributeError, TypeError):
            # 小型测试 adapter 可以使用 opaque token；snapshot 自己持有它。
            return resolution


@dataclass(frozen=True)
class EvaluationQueryRequest:
    """engine 与 MCP adapter 共同消费的统一 query/case 视图。"""

    case: RetrievalEvalCase
    top_k: int
    include_context_pack: bool
    retrieval_mode: Literal["lexical", "vector", "hybrid"]
    vector_config: Mapping[str, Any] | None
    query_version: str
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"]


class EvaluationQueryAdapter(Protocol):
    """两个真实查询 adapter 共享的最小 interface。"""

    def run(self, request: EvaluationQueryRequest) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EngineQueryAdapter:
    """在既有 Query V2 engine 上实现统一 query interface。"""

    runtime: EvaluationRuntimeSnapshot

    def run(self, request: EvaluationQueryRequest) -> dict[str, Any]:
        response = _execute_engine_query(
            self.runtime.root,
            request,
            quality_gate=self.runtime.settings.quality_gate,
        )
        return _normalise_query_envelope(response, error_code="query_contract_invalid")


@dataclass(frozen=True)
class McpQueryAdapter:
    """在 public ``wiki_query`` seam 上实现统一 query interface。"""

    runtime: EvaluationRuntimeSnapshot

    def run(self, request: EvaluationQueryRequest) -> dict[str, Any]:
        if request.query_version != "v2":
            raise RetrievalEvalError("invalid_query_version", "retrieval evaluation only supports query_version v2")
        if request.retrieval_mode != "lexical":
            raise RetrievalEvalError("mcp_requires_lexical", "the MCP evaluation entrypoint is lexical-only")
        adapter = self.runtime.adapter
        if adapter is None or adapter.query is None:
            raise RetrievalEvalError("mcp_adapter_missing", "MCP evaluation requires an injected adapter")
        try:
            with adapter.snapshot(self.runtime.tool_resolution()):
                response = adapter.query(
                    question=request.case.query,
                    scope=request.scope,
                    project=request.case.filters.get("project"),
                    filters=_public_filters(request.case.filters),
                    top_k=request.top_k,
                    vault_root=str(self.runtime.root),
                )
        except RetrievalEvalError:
            raise
        except Exception as exc:
            raise RetrievalEvalError("mcp_query_failed", "the public wiki_query call failed") from exc
        if not isinstance(response, Mapping):
            raise RetrievalEvalError("mcp_query_failed", "the public wiki_query response was not an object")
        if response.get("ok") is not True:
            code = response.get("code")
            safe_code = str(code) if isinstance(code, str) and code else "mcp_query_error"
            raise RetrievalEvalError("mcp_query_error", f"wiki_query returned {safe_code}")
        view = _normalise_query_envelope(
            response,
            error_code="mcp_contract_invalid",
            error_message="wiki_query response envelope is invalid",
        )
        _require_mcp_lexical_pipeline(view["pipeline"])
        return view


@dataclass(frozen=True)
class EvaluationQueryService:
    """通过统一 interface 调用 engine 或 MCP adapter。"""

    runtime: EvaluationRuntimeSnapshot

    def run(self, request: EvaluationQueryRequest) -> dict[str, Any]:
        """运行已构造的统一 request。"""

        return self._adapter().run(request)

    def _adapter(self) -> EvaluationQueryAdapter:
        if self.runtime.adapter is not None:
            return McpQueryAdapter(self.runtime)
        return EngineQueryAdapter(self.runtime)


def validate_dataset_paths(dataset: RetrievalEvalDataset, vault_root: str | Path) -> None:
    """校验标签文件存在于指定 vault 内；不读取页面正文。"""

    root = filesystem_path(vault_root)
    if not root.is_dir():
        raise RetrievalEvalError("vault_missing", f"vault root is not a directory: {root}")
    for case in dataset.cases:
        for relevance in case.relevant:
            target = (root / relevance.path).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise RetrievalEvalError("relevant_path_missing", f"case {case.id} references a missing path: {relevance.path}")


def vault_fingerprint(vault_root: str | Path) -> dict[str, Any]:
    """只哈希相对 wiki 路径与内容 hash，不把路径或正文写入报告。"""

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


def _evaluation_ranking_version(cases: Sequence[Mapping[str, Any]]) -> str:
    """Use the public pipeline identity actually observed by the evaluator."""

    versions: set[str] = set()
    for case in cases:
        pipeline = case.get("pipeline")
        if not isinstance(pipeline, Mapping):
            continue
        value = pipeline.get("ranking_version")
        if isinstance(value, str) and value:
            versions.add(value)
    if len(versions) == 1:
        return next(iter(versions))
    if len(versions) > 1:
        return "mixed-ranking-versions"
    return RANKING_POLICY_VERSION


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
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"] | None = None,
    entrypoint: Literal["engine", "mcp"] = "engine",
    quality_gate: QualityGateSettings | None = None,
) -> dict[str, Any]:
    """通过统一 query service 运行只读评测，保留既有报告形状。"""

    if top_k <= 0:
        raise RetrievalEvalError("invalid_top_k", "top_k must be greater than zero")
    if repeats <= 0:
        raise RetrievalEvalError("invalid_repeats", "repeats must be greater than zero")
    if context_budget_case_limit is not None and context_budget_case_limit < 0:
        raise RetrievalEvalError("invalid_context_budget_case_limit", "context_budget_case_limit must be non-negative")
    if retrieval_mode not in {"lexical", "vector", "hybrid"}:
        raise RetrievalEvalError("invalid_retrieval_mode", "retrieval_mode must be lexical, vector, or hybrid")
    if entrypoint not in {"engine", "mcp"}:
        raise RetrievalEvalError("invalid_entrypoint", "entrypoint must be engine or mcp")
    if retrieval_mode != "lexical" and not vector_config:
        raise RetrievalEvalError("vector_config_missing", "vector and hybrid evaluation require a local vector configuration")
    if query_version != "v2":
        raise RetrievalEvalError("invalid_query_version", "retrieval evaluation only supports query_version v2")
    if scope is not None and scope not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        raise RetrievalEvalError("invalid_scope", "scope must be auto, knowledge, history, all, or archive")

    root = filesystem_path(vault_root)
    validate_dataset_paths(dataset, root)
    vault_fingerprint_before = vault_fingerprint(root)
    telemetry_before = read_event_count(root)
    index_before = RetrievalIndexStore(root).status()

    # 预热解释器和 parser，不把预热记入 latency 样本。
    first = dataset.cases[0]
    _run_case(
        root,
        first,
        top_k=top_k,
        include_context_pack=False,
        retrieval_mode=retrieval_mode,
        vector_config=vector_config,
        query_version=query_version,
        scope=_case_scope(first, scope),
        entrypoint=entrypoint,
        quality_gate=quality_gate,
    )

    cases: list[dict[str, Any]] = []
    latency_samples: list[float] = []
    recall_values: list[float] = []
    precision_values: list[float] = []
    mrr_values: list[float] = []
    ndcg_values: list[float] = []
    ranking_values_by_k: dict[int, dict[str, list[float]]] = {}
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
    diagnosis_distribution: dict[str, int] = {}

    for case_index, case in enumerate(dataset.cases):
        query_runs: list[dict[str, Any]] = []
        rankings: list[list[str]] = []
        case_scope = _case_scope(case, scope)
        for _ in range(repeats):
            started = time.perf_counter()
            result = _run_case(
                root,
                case,
                top_k=top_k,
                include_context_pack=False,
                retrieval_mode=retrieval_mode,
                vector_config=vector_config,
                query_version=query_version,
                scope=case_scope,
                entrypoint=entrypoint,
                quality_gate=quality_gate,
            )
            elapsed_ms = (time.perf_counter() - started) * 1_000
            latency_samples.append(elapsed_ms)
            rankings.append([str(item["path"]) for item in result["results"]])
            query_runs.append({"latency_ms": elapsed_ms, "result": result})
        if any(ranking != rankings[0] for ranking in rankings[1:]):
            raise RetrievalEvalError("non_deterministic_ranking", f"case {case.id} changed ranking across repeats")

        result = query_runs[0]["result"]
        ranked_paths = rankings[0]
        metrics = calculate_ranking_metrics(ranked_paths, case.relevant, top_k=top_k)
        metrics_by_k = calculate_metrics_by_k(ranked_paths, case.relevant, top_k=top_k)
        for raw_k, per_k in metrics_by_k.items():
            k_metrics = ranking_values_by_k.setdefault(raw_k, {"recall": [], "precision": [], "mrr": [], "ndcg": []})
            for metric_name in ("recall", "precision", "mrr", "ndcg"):
                value = per_k[metric_name]
                if value is not None:
                    k_metrics[metric_name].append(float(value))

        recall = metrics["recall"]
        if recall is not None:
            recall_values.append(float(recall))
            precision_values.append(float(metrics["precision"] or 0.0))
            mrr_values.append(float(metrics["mrr"] or 0.0))
            ndcg_values.append(float(metrics["ndcg"] or 0.0))
            total_hits += int(metrics["hits"] or 0)
            total_relevant += int(metrics["relevant_total"] or 0)

        filter_correct = _results_match_filters(result["results"], case.filters)
        if not filter_correct:
            filter_failures += 1
        top_score = float(result["results"][0]["score"]) if result["results"] else 0.0
        false_positive = not case.answerable and bool(result["results"]) and top_score >= dataset.manifest.abstention_threshold
        if not case.answerable:
            no_answer_cases += 1
            no_answer_false_positives += int(false_positive)

        budget: dict[str, Any] | None = None
        if entrypoint == "engine" and measure_context_budget and (
            context_budget_case_limit is None or case_index < context_budget_case_limit
        ):
            context_result = _run_case(
                root,
                case,
                top_k=top_k,
                include_context_pack=True,
                retrieval_mode=retrieval_mode,
                vector_config=vector_config,
                query_version=query_version,
                scope=case_scope,
                entrypoint=entrypoint,
                quality_gate=quality_gate,
            )
            raw_budget = context_result.get("budget") or context_result.get("context_pack", {}).get("budget", {})
            budget = dict(raw_budget)
            raw_used = budget.get("used", {})
            used = int(raw_used) if isinstance(raw_used, int) else sum(int(value) for value in raw_used.values())
            budget["used_total"] = used
            budget["within_budget"] = used <= int(budget.get("total", 0))
            if not budget["within_budget"]:
                budget_violations.append(case.id)
            packed_token_samples.append(used)

        pipeline = pipeline_summary(result.get("pipeline", {}))
        quality_gate_observation = _quality_gate_observation(
            result.get("pipeline", {}),
            configured=quality_gate,
        )
        fallback = pipeline.get("fallback", {}) if isinstance(pipeline, Mapping) else {}
        fallback_level = str(fallback.get("level", "legacy")) if isinstance(fallback, Mapping) else "legacy"
        if fallback_level not in {"none", "legacy"}:
            fallback_cases += 1
        if isinstance(fallback, Mapping):
            for reason in fallback.get("reasons", []):
                fallback_reasons[str(reason)] = fallback_reasons.get(str(reason), 0) + 1

        diagnosis = _diagnose_case(
            root,
            case,
            ranked_paths,
            top_k=top_k,
            scope=case_scope,
            retrieval_mode=retrieval_mode,
            vector_config=vector_config,
            query_version=query_version,
            entrypoint=entrypoint,
            quality_gate=quality_gate,
        )
        for item in diagnosis:
            category = str(item.get("category", "unknown"))
            diagnosis_distribution[category] = diagnosis_distribution.get(category, 0) + 1
        scope_contracts.append(
            {
                "id": case.id,
                "scope": pipeline.get("scope", "legacy") if isinstance(pipeline, Mapping) else "legacy",
                "corpus": pipeline.get("corpus", "active") if isinstance(pipeline, Mapping) else "active",
                "authority": pipeline.get("authority", "legacy") if isinstance(pipeline, Mapping) else "legacy",
                "fallback_level": fallback_level,
            }
        )

        cases.append(
            {
                "id": case.id,
                "query": case.query,
                "answerable": case.answerable,
                "language": case.language,
                "tags": list(case.tags),
                "filters": case.filters,
                "scope": case_scope,
                "relevant": [{"path": item.path, "grade": item.grade} for item in case.relevant],
                "ranked_paths": ranked_paths,
                "ranking_runs": rankings,
                "latency_ms": [item["latency_ms"] for item in query_runs],
                "metrics": metrics,
                "metrics_by_k": metrics_by_k,
                "top_score": top_score,
                "no_answer_false_positive": false_positive,
                "filter_correct": filter_correct,
                "context_budget": budget,
                "pipeline": pipeline,
                "warnings": [],
                "warning_count": pipeline.get("warning_count", 0),
                "result_summary": [result_summary(item) for item in result["results"]],
                "diagnosis": diagnosis,
                "quality_gate_observation": quality_gate_observation,
            }
        )

    vault_fingerprint_after = vault_fingerprint(root)
    telemetry_after = read_event_count(root)
    index_after = RetrievalIndexStore(root).status()
    side_effects = _evaluation_side_effects(
        vault_fingerprint_before,
        vault_fingerprint_after,
        telemetry_before,
        telemetry_after,
        index_before,
        index_after,
    )

    gate_configured = (
        {"mode": quality_gate.mode, "policy_version": quality_gate.policy_version}
        if quality_gate is not None and quality_gate.mode in {"shadow", "enforce"}
        else None
    )
    gate_report = assemble_quality_gate_report(cases, top_k=top_k, configured=gate_configured)
    gate_identity = gate_report["identity"]
    metadata_quality_gate = gate_report["metadata"]
    quality_gate_metric_projection = gate_report["metrics"]

    ranking_version = _evaluation_ranking_version(cases)
    report = {
        "schema_version": RETRIEVAL_EVAL_SCHEMA_VERSION,
        "metadata": {
            "dataset_id": dataset.manifest.dataset_id,
            "dataset_revision": dataset.manifest.revision,
            "dataset_status": dataset.manifest.status,
            "dataset_schema_version": dataset.manifest.schema_version,
            "abstention_threshold": dataset.manifest.abstention_threshold,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "runtime_provenance": RUNTIME_PROVENANCE.to_public_dict(),
            "ranking": {"version": ranking_version},
            "experiment": normalise_experiment_metadata(experiment_metadata),
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
                "scope": scope or "per_case",
                "entrypoint": entrypoint,
                "telemetry_enabled": False,
                "context_budget_mode": "engine_context_pack" if entrypoint == "engine" else "mcp_public_budget_not_exposed",
            },
            "query_v2": {
                "scope_authority_lifecycle": scope_contracts,
                "cold_start_latency_ms": None,
                "comparison_status": "unproven_without_frozen_v2_baseline",
                "evaluation_status": "baseline_unproven",
            },
            "vault_fingerprint": vault_fingerprint_after,
            "side_effects": side_effects,
            "quality_gate": metadata_quality_gate,
        },
        "metrics": {
            "recall_at_k_macro": mean_or_none(recall_values),
            "recall_at_k_micro": total_hits / total_relevant if total_relevant else None,
            "precision_at_k_macro": mean_or_none(precision_values),
            "precision_at_k_micro": total_hits / (len(recall_values) * top_k) if recall_values else None,
            "mrr_at_k_macro": mean_or_none(mrr_values),
            "ndcg_at_k_macro": mean_or_none(ndcg_values),
            "ranking_by_k": {
                str(k): {
                    metric_name: mean_or_none(values[metric_name])
                    for metric_name in ("recall", "precision", "mrr", "ndcg")
                }
                for k, values in sorted(ranking_values_by_k.items())
            },
            "slice_metrics": build_slice_metrics(cases),
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
            "diagnosis_distribution": diagnosis_distribution,
            "packed_token_median": median(packed_token_samples),
            "latency_sample_count": len(latency_samples),
            "context_budget": {
                "measured_cases": sum(1 for case in cases if case["context_budget"] is not None),
                "case_limit": context_budget_case_limit,
                "violations": budget_violations,
                "within_budget": not budget_violations,
            },
            **quality_gate_metric_projection,
        },
        "cases": cases,
    }
    for key in ("gate_policy_version", "gate_config_hash"):
        if key in gate_identity:
            report["metadata"][key] = gate_identity[key]
    return report


def _quality_gate_observation(
    pipeline: object,
    *,
    configured: QualityGateSettings | None,
) -> dict[str, Any] | None:
    """保留评测所需的 shadow 摘要，详细决策只留在内存。"""

    raw_pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    raw_gate = raw_pipeline.get("quality_gate")
    if not isinstance(raw_gate, Mapping):
        if configured is None or configured.mode not in {"shadow", "enforce"}:
            return None
        return {
            "available": False,
            "mode": configured.mode,
            "policy_version": configured.policy_version,
        }

    observation: dict[str, Any] = {"available": True}
    for key in (
        "mode",
        "policy_version",
        "calibration_revision",
        "config_hash",
        "status",
        "candidate_count",
        "accepted_count",
        "rejected_count",
        "fail_open",
    ):
        if key in raw_gate:
            observation[key] = raw_gate[key]
    for key in ("reason_counts", "score_family_counts", "selection_counts"):
        value = raw_gate.get(key)
        if isinstance(value, Mapping):
            observation[key] = dict(value)
    low_sample_buckets = raw_gate.get("low_sample_buckets")
    if isinstance(low_sample_buckets, (list, tuple)):
        observation["low_sample_buckets"] = [str(value) for value in low_sample_buckets[:32] if isinstance(value, str)]

    # Future adapters may expose page-level decisions.  They are intentionally
    # kept only in the internal case view so report output remains path-free.
    for key in ("decisions", "candidate_decisions"):
        value = raw_gate.get(key)
        if isinstance(value, (Mapping, list, tuple)):
            observation[key] = value
    for key in ("accepted_paths", "rejected_paths", "accepted_pages", "rejected_pages"):
        value = raw_gate.get(key)
        if isinstance(value, (list, tuple)):
            observation[key] = list(value)

    if configured is not None and configured.mode in {"shadow", "enforce"}:
        observation.setdefault("mode", configured.mode)
        observation.setdefault("policy_version", configured.policy_version)
    return observation


def _diagnose_case(
    root: Path,
    case: RetrievalEvalCase,
    ranked_paths: Sequence[str],
    *,
    top_k: int,
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"],
    retrieval_mode: Literal["lexical", "vector", "hybrid"],
    vector_config: Mapping[str, Any] | None,
    query_version: str,
    entrypoint: Literal["engine", "mcp"],
    quality_gate: QualityGateSettings | None,
) -> list[dict[str, Any]]:
    """使用有界 top-40 只读 query 解释遗漏，不改变主评测指标。"""

    missing = [item for item in case.relevant if item.path not in ranked_paths]
    if not missing:
        return []
    try:
        diagnostic_result = _run_case(
            root,
            case,
            top_k=min(40, max(40, top_k)),
            include_context_pack=False,
            retrieval_mode=retrieval_mode,
            vector_config=vector_config,
            query_version=query_version,
            scope=scope,
            entrypoint=entrypoint,
            quality_gate=quality_gate,
        )
        diagnostic_paths = [str(item["path"]) for item in diagnostic_result.get("results", [])]
    except RetrievalEvalError as exc:
        return [{"path": item.path, "category": "diagnosis_unavailable", "reason": exc.code} for item in missing]
    index_status = RetrievalIndexStore(root).status()
    output: list[dict[str, Any]] = []
    for item in missing:
        if item.path in diagnostic_paths:
            output.append({"path": item.path, "category": "ranking_position_late", "rank": diagnostic_paths.index(item.path) + 1})
        elif not index_status.get("ok"):
            output.append({"path": item.path, "category": "index_missing", "index_code": index_status.get("code")})
        elif not _path_scope_filter_compatible(item.path, scope, case.filters):
            output.append({"path": item.path, "category": "scope_filter_boundary"})
        elif any("needs_review" in tag for tag in case.tags):
            output.append({"path": item.path, "category": "label_issue_or_vault_drift"})
        else:
            output.append({"path": item.path, "category": "lexical_coverage_or_query_mismatch"})
    return output


def _path_scope_filter_compatible(path: str, scope: str, filters: Mapping[str, Any]) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    if scope == "archive" and not normalized.startswith("archives/bundles/"):
        return False
    if scope == "raw" and not normalized.startswith("raw/"):
        return False
    if scope == "history" and not normalized.startswith("raw/sources/chat/"):
        return False
    if filters.get("path_prefix") and not path_matches_prefix(normalized, str(filters["path_prefix"])):
        return False
    return True


def _case_scope(
    case: RetrievalEvalCase,
    override: Literal["auto", "knowledge", "history", "all", "archive", "raw"] | None,
) -> Literal["auto", "knowledge", "history", "all", "archive", "raw"]:
    selected = override or case.scope or "knowledge"
    if selected not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        raise RetrievalEvalError("invalid_scope", f"case {case.id}: unsupported query scope")
    return cast(Literal["auto", "knowledge", "history", "all", "archive", "raw"], selected)


def _run_case(
    root: Path,
    case: RetrievalEvalCase,
    *,
    top_k: int,
    include_context_pack: bool,
    retrieval_mode: Literal["lexical", "vector", "hybrid"],
    vector_config: Mapping[str, Any] | None,
    query_version: str,
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"],
    entrypoint: Literal["engine", "mcp"],
    quality_gate: QualityGateSettings | None = None,
) -> dict[str, Any]:
    runtime = (
        EvaluationRuntimeSnapshot.from_mcp_vault(root, quality_gate=quality_gate)
        if entrypoint == "mcp"
        else EvaluationRuntimeSnapshot.lexical_only(root, quality_gate=quality_gate)
    )
    request = EvaluationQueryRequest(
        case=case,
        top_k=top_k,
        include_context_pack=include_context_pack,
        retrieval_mode=retrieval_mode,
        vector_config=vector_config,
        query_version=query_version,
        scope=scope,
    )
    return EvaluationQueryService(runtime).run(request)


def _require_mcp_lexical_pipeline(pipeline: Mapping[str, Any]) -> None:
    """在 MCP adapter seam 检查 lexical pipeline 和零 vector hits。"""

    mode = pipeline.get("retrieval_mode")
    counters = pipeline.get("counters")
    vector_hits = counters.get("vector_hits") if isinstance(counters, Mapping) else None
    if mode != "lexical" or type(vector_hits) is not int or vector_hits != 0:
        raise RetrievalEvalError("mcp_not_lexical", "the public wiki_query response was not lexical-only")


def _public_filters(filters: Mapping[str, Any]) -> dict[str, Any] | None:
    return dict(normalize_evaluation_filter_contract(filters).public) or None


def _execute_engine_query(
    root: Path,
    request: EvaluationQueryRequest,
    *,
    quality_gate: QualityGateSettings | None = None,
) -> dict[str, Any]:
    if request.query_version != "v2":
        raise RetrievalEvalError("invalid_query_version", "retrieval evaluation only supports query_version v2")
    embedding: EmbeddingSettings | None = None
    if request.retrieval_mode != "lexical":
        settings = parse_vector_settings(root, dict(request.vector_config or {}))
        embedding = EmbeddingSettings(
            enabled=True,
            provider=settings.provider,
            model_path=settings.model_path,
            index_path=settings.index_path,
            device=settings.device,
            batch_size=settings.batch_size,
            max_sequence_length=settings.max_sequence_length,
            candidate_limit=settings.candidate_limit,
            rrf_k=settings.rrf_k,
            min_vector_score=settings.min_vector_score,
        )
    filter_contract = normalize_evaluation_filter_contract(request.case.filters, request.case.id)
    return run_query_v2(
        root,
        request.case.query,
        scope=request.scope,
        project=request.case.filters.get("project"),
        filters=QueryFilters.from_mapping(dict(filter_contract.query)),
        top_k=request.top_k,
        embedding=embedding,
        telemetry=TelemetrySettings(enabled=False),
        quality_gate=quality_gate,
        include_context_pack=request.include_context_pack,
        retrieval_mode=request.retrieval_mode,
    )


def _normalise_query_envelope(
    response: object,
    *,
    error_code: str,
    error_message: str = "query response envelope is invalid",
) -> dict[str, Any]:
    """adapter 边界唯一的统一结果 envelope 防御检查。"""

    if not isinstance(response, Mapping):
        raise RetrievalEvalError(error_code, error_message)
    results = response.get("results")
    pipeline = response.get("pipeline")
    budget = response.get("budget", {})
    if not isinstance(results, list) or any(not isinstance(item, Mapping) for item in results):
        raise RetrievalEvalError(error_code, "query results must be a list of objects")
    if not isinstance(pipeline, Mapping):
        raise RetrievalEvalError(error_code, "query pipeline must be an object")
    if budget is None:
        budget = {}
    if not isinstance(budget, Mapping):
        raise RetrievalEvalError(error_code, "query budget must be an object")
    return {"ok": True, "results": results, "pipeline": pipeline, "budget": budget}


def _results_match_filters(results: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]) -> bool:
    try:
        normalized = normalize_evaluation_filter_contract(filters).matcher
    except RetrievalEvalError:
        return False

    for item in results:
        path = str(item.get("path") or "")
        if not path:
            return False
        frontmatter = item.get("frontmatter") or item.get("metadata")
        if not isinstance(frontmatter, Mapping):
            return False
        if not page_matches_filters(
            frontmatter,
            str(item.get("source_kind") or "wiki"),
            project=normalized.get("project"),
            page_type=normalized.get("type"),
            tags=tuple(normalized.get("tags", ())),
            path_prefix=normalized.get("path_prefix"),
            page_path=path,
        ):
            return False
    return True


def _evaluation_side_effects(
    vault_before: Mapping[str, Any],
    vault_after: Mapping[str, Any],
    telemetry_before: int | None,
    telemetry_after: int | None,
    index_before: Mapping[str, Any],
    index_after: Mapping[str, Any],
) -> dict[str, Any]:
    """保留评测六快照的只读保护和原有 clean 语义。"""

    vault_unchanged = vault_before.get("value") == vault_after.get("value")
    telemetry_created = telemetry_before is None and telemetry_after is not None
    telemetry_unchanged = not telemetry_created and (telemetry_before is None or telemetry_before == telemetry_after)
    index_unchanged = (
        index_before.get("code") == index_after.get("code")
        and index_before.get("fingerprint") == index_after.get("fingerprint")
        and index_before.get("state") == index_after.get("state")
    )
    return {
        "vault_unchanged": vault_unchanged,
        "telemetry_unchanged": telemetry_unchanged,
        "telemetry_created": telemetry_created,
        "telemetry_before": telemetry_before,
        "telemetry_after": telemetry_after,
        "index_unchanged": index_unchanged,
        "index_before": {key: index_before.get(key) for key in ("code", "state", "fingerprint", "schema_version")},
        "index_after": {key: index_after.get(key) for key in ("code", "state", "fingerprint", "schema_version")},
        "clean": vault_unchanged and telemetry_unchanged and index_unchanged,
    }

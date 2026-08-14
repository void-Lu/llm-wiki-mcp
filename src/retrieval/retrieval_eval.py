"""Deterministic, read-only evaluation for the public wiki query API."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import inspect
from pathlib import Path
from typing import Any, Literal, cast

from common.privacy_policy import LocatorError, normalize_vault_relative
from retrieval.metadata_filters import (
    QUERY_METADATA_FILTERS,
    normalize_metadata_filters,
    page_matches_filters,
    path_matches_prefix,
)
from runtime.runtime_provenance import RUNTIME_PROVENANCE
from retrieval.query_pipeline import DEFAULT_TOP_K, QueryFilters, RANKING_POLICY_VERSION, run_query_v2
from retrieval.query_telemetry import read_event_count
from retrieval.retrieval_index import RetrievalIndexStore
from runtime.runtime_config import EmbeddingSettings, TelemetrySettings, VaultSettings
from retrieval.vector_index import parse_vector_settings
from wiki.wiki_paths import filesystem_path


RETRIEVAL_EVAL_SCHEMA_VERSION = 1
_SUPPORTED_DATASET_SCHEMA_VERSIONS = {1, 2}
_ALLOWED_FILTERS = {"project", "filter_type", "filter_tags", "type", "tags", "path_prefix", "pathPrefix"}
_EVALUATION_KS = (1, 3, 5, 10)

__all__ = [
    "EvaluationQueryService",
    "EvaluationFilterContract",
    "EvaluationRuntimeSnapshot",
    "McpEntryAdapter",
    "default_mcp_entry_adapter",
    "RetrievalEvalCase",
    "RetrievalEvalDataset",
    "RetrievalEvalError",
    "RetrievalEvalManifest",
    "Relevance",
    "normalize_evaluation_filter_contract",
    "parse_evaluation_filters",
    "safe_report_identity",
    "vault_fingerprint",
]


class RetrievalEvalError(ValueError):
    """A user-correctable dataset or evaluation configuration error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class McpEntryAdapter:
    """The minimal MCP entry-point surface required by retrieval evaluation."""

    resolve: Callable[..., Any]
    snapshot: Callable[[Any], AbstractContextManager[None]]
    query: Callable[..., Any] | None = None


def default_mcp_entry_adapter() -> McpEntryAdapter:
    """Build the real adapter while keeping the server import at this seam."""

    import app.server as server_module

    return McpEntryAdapter(
        resolve=server_module.resolve_tool_vault,
        snapshot=server_module.tool_runtime_snapshot,
        query=server_module.wiki_query,
    )


def _resolve_mcp_vault(adapter: McpEntryAdapter, vault_root: str | Path) -> Any:
    """Call both the keyword-only production resolver and legacy test seams."""

    try:
        parameters = inspect.signature(adapter.resolve).parameters
    except (TypeError, ValueError):
        return adapter.resolve(str(vault_root))
    if "vault_root" in parameters:
        return adapter.resolve(vault_root=str(vault_root))
    return adapter.resolve(str(vault_root))


@dataclass(frozen=True)
class EvaluationFilterContract:
    """Pure projections of one evaluation filter boundary.

    ``internal`` keeps the historical dataset representation, ``public`` is
    the nested MCP filter payload, ``query`` is ready for ``QueryFilters``,
    and ``matcher`` is the production metadata predicate input.  Keeping the
    projections together prevents each evaluation entrypoint from pairing
    public/legacy aliases independently.
    """

    internal: Mapping[str, Any]
    public: Mapping[str, Any]
    query: Mapping[str, Any]
    matcher: Mapping[str, Any]


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
    scope: str = "knowledge"


@dataclass(frozen=True)
class RetrievalEvalManifest:
    dataset_id: str
    revision: str
    abstention_threshold: float
    status: str = "reviewed"
    schema_version: int = RETRIEVAL_EVAL_SCHEMA_VERSION


@dataclass(frozen=True)
class RetrievalEvalDataset:
    manifest: RetrievalEvalManifest
    cases: tuple[RetrievalEvalCase, ...]


@dataclass(frozen=True)
class EvaluationRuntimeSnapshot:
    """Immutable runtime/vault settings owned by one evaluation service."""

    root: Path
    logical_name: str
    settings: VaultSettings
    adapter: McpEntryAdapter | None = None
    mcp_resolution: Any | None = None

    @classmethod
    def lexical_only(cls, vault_root: str | Path) -> "EvaluationRuntimeSnapshot":
        root = filesystem_path(vault_root)
        settings = replace(
            VaultSettings(name=root.name, root=root),
            telemetry=TelemetrySettings(enabled=False),
        )
        return cls(root=root, logical_name=root.name, settings=settings)

    @classmethod
    def from_mcp_vault(
        cls,
        vault_root: str | Path,
        *,
        adapter: McpEntryAdapter | None = None,
    ) -> "EvaluationRuntimeSnapshot":
        """Resolve MCP settings once, then freeze a telemetry-off copy locally."""

        active_adapter = adapter or default_mcp_entry_adapter()
        resolution = _resolve_mcp_vault(active_adapter, vault_root)
        settings = resolution.resolved.settings
        if not settings.retrieval.lexical_enabled or settings.retrieval.embedding.enabled:
            raise RetrievalEvalError(
                "mcp_not_lexical",
                "the selected vault MCP configuration is not lexical-only",
            )
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
        """Build a telemetry-off resolution without touching the process registry."""

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
            # Small test adapters may use an opaque resolution token. Their
            # snapshot owns the token and does not need a server-side clone.
            return resolution


@dataclass(frozen=True)
class EvaluationQueryService:
    """Explicit engine/MCP adapter boundary for one runtime snapshot."""

    runtime: EvaluationRuntimeSnapshot

    def run(
        self,
        case: RetrievalEvalCase,
        *,
        top_k: int,
        include_context_pack: bool,
        retrieval_mode: Literal["lexical", "vector", "hybrid"],
        vector_config: Mapping[str, Any] | None,
        query_version: str,
        scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"],
        entrypoint: Literal["engine", "mcp"],
    ) -> dict[str, Any]:
        if entrypoint == "mcp":
            return self._run_mcp(case, top_k=top_k, query_version=query_version, scope=scope)
        return self._run_engine(
            case,
            top_k=top_k,
            include_context_pack=include_context_pack,
            retrieval_mode=retrieval_mode,
            vector_config=vector_config,
            query_version=query_version,
            scope=scope,
        )

    def _run_mcp(
        self,
        case: RetrievalEvalCase,
        *,
        top_k: int,
        query_version: str,
        scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"],
    ) -> dict[str, Any]:
        """Call the real public MCP function under a context-local snapshot."""

        if query_version != "v2":
            raise RetrievalEvalError("invalid_query_version", "retrieval evaluation only supports query_version v2")
        adapter = self.runtime.adapter
        if adapter is None or adapter.query is None:
            raise RetrievalEvalError("mcp_adapter_missing", "MCP evaluation requires an injected adapter")
        try:
            with adapter.snapshot(self.runtime.tool_resolution()):
                response = adapter.query(
                    question=case.query,
                    scope=scope,
                    project=case.filters.get("project"),
                    filters=_public_filters(case.filters),
                    top_k=top_k,
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
        results = response.get("results")
        pipeline = response.get("pipeline")
        if not isinstance(results, list):
            raise RetrievalEvalError("mcp_contract_invalid", "wiki_query results must be a list")
        if not isinstance(pipeline, Mapping):
            raise RetrievalEvalError("mcp_contract_invalid", "wiki_query pipeline must be an object")
        _require_mcp_lexical_pipeline(pipeline)
        return {
            "ok": True,
            "results": results,
            "pipeline": pipeline,
            "budget": response.get("budget", {}),
        }

    def _run_engine(
        self,
        case: RetrievalEvalCase,
        *,
        top_k: int,
        include_context_pack: bool,
        retrieval_mode: Literal["lexical", "vector", "hybrid"],
        vector_config: Mapping[str, Any] | None,
        query_version: str,
        scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"],
    ) -> dict[str, Any]:
        return _query_case(
            self.runtime.root,
            case,
            top_k=top_k,
            include_context_pack=include_context_pack,
            retrieval_mode=retrieval_mode,
            vector_config=vector_config,
            query_version=query_version,
            scope=scope,
        )


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
    """Calculate Recall, Precision, MRR and nDCG without query-code dependencies."""
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


def vault_fingerprint(vault_root: str | Path) -> dict[str, Any]:
    """Hash queryable wiki files by relative path and content without exposing either.

    This is the single evaluator-owned fallback fingerprint contract. Callers
    should reuse its result for both the report and comparison gates rather
    than walking the vault independently.
    """
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


def safe_report_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only bounded, non-path identity fields from an eval report."""

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
) -> dict[str, Any]:
    """Run a read-only retrieval evaluation through the engine or MCP boundary."""
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
    if entrypoint == "mcp" and retrieval_mode != "lexical":
        raise RetrievalEvalError("mcp_requires_lexical", "the MCP evaluation entrypoint is lexical-only")
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

    # Warm the interpreter and parser without treating it as a latency sample.
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
        metrics_by_k = _calculate_metrics_by_k(ranked_paths, case.relevant, top_k=top_k)
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
        false_positive = (
            not case.answerable
            and bool(result["results"])
            and top_score >= dataset.manifest.abstention_threshold
        )
        if not case.answerable:
            no_answer_cases += 1
            no_answer_false_positives += int(false_positive)

        budget: dict[str, Any] | None = None
        if entrypoint == "engine" and measure_context_budget and (context_budget_case_limit is None or case_index < context_budget_case_limit):
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

        pipeline = _pipeline_summary(result.get("pipeline", {}))
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
        )
        for item in diagnosis:
            category = str(item.get("category", "unknown"))
            diagnosis_distribution[category] = diagnosis_distribution.get(category, 0) + 1
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
                "language": case.language,
                "tags": list(case.tags),
                "filters": case.filters,
                "scope": case_scope,
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
                "result_summary": [_result_summary(item) for item in result["results"]],
                "diagnosis": diagnosis,
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

    return {
        "schema_version": RETRIEVAL_EVAL_SCHEMA_VERSION,
        "metadata": {
            "dataset_id": dataset.manifest.dataset_id,
            "dataset_revision": dataset.manifest.revision,
            "dataset_status": dataset.manifest.status,
            "dataset_schema_version": dataset.manifest.schema_version,
            "abstention_threshold": dataset.manifest.abstention_threshold,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "runtime_provenance": RUNTIME_PROVENANCE.to_public_dict(),
            "ranking": {"version": RANKING_POLICY_VERSION},
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
        },
        "metrics": {
            "recall_at_k_macro": _mean_or_none(recall_values),
            "recall_at_k_micro": total_hits / total_relevant if total_relevant else None,
            "precision_at_k_macro": _mean_or_none(precision_values),
            "precision_at_k_micro": total_hits / (len(recall_values) * top_k) if recall_values else None,
            "mrr_at_k_macro": _mean_or_none(mrr_values),
            "ndcg_at_k_macro": _mean_or_none(ndcg_values),
            "ranking_by_k": {
                str(k): {
                    metric_name: _mean_or_none(values[metric_name])
                    for metric_name in ("recall", "precision", "mrr", "ndcg")
                }
                for k, values in sorted(ranking_values_by_k.items())
            },
            "slice_metrics": _build_slice_metrics(cases),
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


def evaluate_retrieval_gate(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    max_metric_regression: float = 0.02,
    max_latency_growth: float = 0.10,
    max_no_answer_false_positive_rate: float = 0.05,
) -> dict[str, Any]:
    """Compare a candidate report with a frozen baseline using stable gates."""
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

    def add_check(name: str, passed: bool, *, actual: Any = None, expected: Any = None, reason: str | None = None) -> None:
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
        ("vault_fingerprint", candidate_fingerprint.get("value") if isinstance(candidate_fingerprint, Mapping) else None, baseline_fingerprint.get("value") if isinstance(baseline_fingerprint, Mapping) else None),
        ("ranking_version", candidate_identity.get("ranking_version"), baseline_identity.get("ranking_version")),
    ):
        add_check(name, bool(candidate_value) and candidate_value == baseline_value, actual=candidate_value, expected=baseline_value, reason="baseline_identity_mismatch" if candidate_value != baseline_value else None)

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
            reason="evaluation_side_effect_detected" if not isinstance(side_effects, Mapping) or side_effects.get("clean") is not True else None,
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
    """Write machine-readable JSON and a compact Markdown summary."""
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
    """Copy a report and remove evidence-bearing pipeline fields before output."""

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
            pipeline = (
                dict(pipeline_value)
                if _has_pipeline_summary_shape(pipeline_value)
                else _project_pipeline(pipeline_value)
            )
            case["pipeline"] = pipeline
            case["warnings"] = []
            case["warning_count"] = pipeline.get("warning_count", 0)
    return safe_report


def _parse_manifest(raw: object) -> RetrievalEvalManifest:
    data = _mapping(raw, "manifest_invalid", "manifest must be a JSON object")
    _require_schema_version(data, "manifest")
    dataset_id = _nonempty_string(data.get("dataset_id"), "manifest_invalid", "manifest.dataset_id must be a string")
    revision = _nonempty_string(data.get("revision"), "manifest_invalid", "manifest.revision must be a string")
    threshold = data.get("abstention_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        raise RetrievalEvalError("manifest_invalid", "manifest.abstention_threshold must be a non-negative number")
    schema_version = int(data.get("schema_version", RETRIEVAL_EVAL_SCHEMA_VERSION))
    status = data.get("status", "reviewed")
    if not isinstance(status, str) or not status.strip():
        raise RetrievalEvalError("manifest_invalid", "manifest.status must be a string")
    return RetrievalEvalManifest(
        dataset_id=dataset_id,
        revision=revision,
        abstention_threshold=float(threshold),
        status=status.strip(),
        schema_version=schema_version,
    )


def _parse_case(raw: object, line_number: int) -> RetrievalEvalCase:
    data = _mapping(raw, "case_invalid", f"line {line_number} must be a JSON object")
    _require_schema_version(data, f"case at line {line_number}")
    case_id = _nonempty_string(data.get("id"), "case_invalid", f"line {line_number}: id must be a string")
    query = _nonempty_string(data.get("query"), "case_invalid", f"case {case_id}: query must be a string")
    answerable = data.get("answerable")
    if type(answerable) is not bool:
        raise RetrievalEvalError("case_invalid", f"case {case_id}: answerable must be boolean")
    filters = parse_evaluation_filters(data.get("filters", {}), case_id)
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
    scope = data.get("scope", "knowledge")
    if not isinstance(scope, str) or scope not in {"auto", "knowledge", "history", "all", "archive", "raw"}:
        raise RetrievalEvalError("invalid_scope", f"case {case_id}: scope must be a supported query scope")
    tags_raw = data.get("tags", [])
    if not isinstance(tags_raw, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags_raw):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: tags must be strings")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise RetrievalEvalError("case_invalid", f"case {case_id}: notes must be a string")
    return RetrievalEvalCase(case_id, query, tuple(relevance), filters, answerable, language, tuple(tags_raw), notes, scope)


def normalize_evaluation_filter_contract(
    raw: object,
    case_id: str = "<unknown>",
) -> EvaluationFilterContract:
    """Build every evaluator filter projection from one pure boundary.

    Dataset fixtures historically used ``filter_type``/``filter_tags`` while
    the MCP boundary uses ``type``/``tags``.  This helper pairs those aliases,
    validates the public vocabulary, and returns the internal, MCP, query and
    production-matcher projections together so callers cannot drift apart.
    """

    data = _mapping(raw, "invalid_filters", f"case {case_id}: filters must be an object")
    unknown = sorted(set(data) - _ALLOWED_FILTERS)
    if unknown:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: unsupported filters: {', '.join(unknown)}")

    project: str | None = None
    if "project" in data:
        project = _nonempty_string(data["project"], "invalid_filters", f"case {case_id}: project must be a string")

    public_input: dict[str, Any] = {}
    for public_name, legacy_name in (("type", "filter_type"), ("tags", "filter_tags")):
        if public_name in data and legacy_name in data and data[public_name] != data[legacy_name]:
            raise RetrievalEvalError("invalid_filters", f"case {case_id}: {public_name} and {legacy_name} disagree")
        if public_name in data:
            public_input[public_name] = data[public_name]
        elif legacy_name in data:
            public_input[public_name] = data[legacy_name]
    if "type" in public_input and (not isinstance(public_input["type"], str) or not public_input["type"].strip()):
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: type must be a non-empty string")
    if "path_prefix" in data:
        public_input["path_prefix"] = data["path_prefix"]
    if "pathPrefix" in data:
        public_input["pathPrefix"] = data["pathPrefix"]

    try:
        normalized = normalize_metadata_filters(
            public_input,
            allowed=QUERY_METADATA_FILTERS,
            preserve_path_trailing=True,
        )
    except ValueError as exc:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: {exc}") from exc

    internal: dict[str, Any] = {}
    if project is not None:
        internal["project"] = project
    if "type" in normalized:
        internal["filter_type"] = normalized["type"]
    if "tags" in normalized:
        tags = list(normalized["tags"])
        if not tags:
            raise RetrievalEvalError("invalid_filters", f"case {case_id}: tags must be a non-empty list of strings")
        internal["filter_tags"] = tags
    if "path_prefix" in normalized:
        internal["path_prefix"] = normalized["path_prefix"]
    elif "path_prefix" in public_input or "pathPrefix" in public_input:
        raise RetrievalEvalError("invalid_filters", f"case {case_id}: path_prefix must be a non-empty vault-relative string")

    public: dict[str, Any] = {}
    if "type" in normalized:
        public["type"] = normalized["type"]
    if "tags" in normalized:
        public["tags"] = list(normalized["tags"])
    if "path_prefix" in normalized:
        public["path_prefix"] = normalized["path_prefix"]

    matcher = dict(normalized)
    if project is not None:
        matcher["project"] = project
    return EvaluationFilterContract(
        internal=internal,
        public=public,
        query=dict(public),
        matcher=matcher,
    )


def parse_evaluation_filters(raw: object, case_id: str = "<unknown>") -> dict[str, Any]:
    """Compatibility facade returning the historical internal filter shape."""

    return dict(normalize_evaluation_filter_contract(raw, case_id).internal)


def _parse_filters(raw: object, case_id: str) -> dict[str, Any]:
    """Compatibility alias for older in-repository callers."""

    return parse_evaluation_filters(raw, case_id)


def _calculate_metrics_by_k(
    ranked_paths: Sequence[str],
    relevant: Sequence[Relevance],
    *,
    top_k: int,
) -> dict[int, dict[str, float | int | None]]:
    ks = sorted({k for k in _EVALUATION_KS if k <= top_k} | {top_k})
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
) -> list[dict[str, Any]]:
    """Explain misses with a bounded read-only top-40 query."""

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
        )
        diagnostic_paths = [str(item["path"]) for item in diagnostic_result.get("results", [])]
    except RetrievalEvalError as exc:
        return [{"path": item.path, "category": "diagnosis_unavailable", "reason": exc.code} for item in missing]
    index_status = RetrievalIndexStore(root).status()
    output: list[dict[str, Any]] = []
    for item in missing:
        if item.path in diagnostic_paths:
            output.append(
                {
                    "path": item.path,
                    "category": "ranking_position_late",
                    "rank": diagnostic_paths.index(item.path) + 1,
                }
            )
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
) -> dict[str, Any]:
    runtime = EvaluationRuntimeSnapshot.from_mcp_vault(root) if entrypoint == "mcp" else EvaluationRuntimeSnapshot.lexical_only(root)
    return EvaluationQueryService(runtime).run(
        case,
        top_k=top_k,
        include_context_pack=include_context_pack,
        retrieval_mode=retrieval_mode,
        vector_config=vector_config,
        query_version=query_version,
        scope=scope,
        entrypoint=entrypoint,
    )


def _mcp_query_case(
    root: Path,
    case: RetrievalEvalCase,
    *,
    top_k: int,
    query_version: str,
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"],
) -> dict[str, Any]:
    """Compatibility wrapper for callers that still name the MCP adapter."""

    return EvaluationQueryService(EvaluationRuntimeSnapshot.from_mcp_vault(root)).run(
        case,
        top_k=top_k,
        include_context_pack=False,
        retrieval_mode="lexical",
        vector_config=None,
        query_version=query_version,
        scope=scope,
        entrypoint="mcp",
    )


def _require_mcp_lexical_pipeline(pipeline: Mapping[str, Any]) -> None:
    """Enforce the public MCP contract used by lexical-only evaluations."""

    mode = pipeline.get("retrieval_mode")
    counters = pipeline.get("counters")
    vector_hits = counters.get("vector_hits") if isinstance(counters, Mapping) else None
    if mode != "lexical" or type(vector_hits) is not int or vector_hits != 0:
        raise RetrievalEvalError("mcp_not_lexical", "the public wiki_query response was not lexical-only")


def _pipeline_summary(pipeline: object) -> dict[str, Any]:
    """Project public pipeline data to the report view exactly once per case.

    The overlap with ``run_query_v2``'s public pipeline dictionary is
    intentional: evaluation reads the public response and creates a separate
    safe report view rather than merging two competing pipeline contracts.
    """

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
    """Accept only bounded identifier-like fallback reason codes."""

    return all(character.isalnum() or character in {"_", ":", ".", "-"} for character in value.strip())


def _public_filters(filters: Mapping[str, Any]) -> dict[str, Any] | None:
    return dict(normalize_evaluation_filter_contract(filters).public) or None


def _query_case(
    root: Path,
    case: RetrievalEvalCase,
    *,
    top_k: int,
    include_context_pack: bool,
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "lexical",
    vector_config: Mapping[str, Any] | None = None,
    query_version: str = "v2",
    scope: Literal["auto", "knowledge", "history", "all", "archive", "raw"] = "knowledge",
) -> dict[str, Any]:
    if query_version != "v2":
        raise RetrievalEvalError("invalid_query_version", "retrieval evaluation only supports query_version v2")
    embedding: EmbeddingSettings | None = None
    if retrieval_mode != "lexical":
        settings = parse_vector_settings(root, dict(vector_config or {}))
        embedding = EmbeddingSettings(enabled=True, provider=settings.provider, model_path=settings.model_path, index_path=settings.index_path, device=settings.device, batch_size=settings.batch_size, max_sequence_length=settings.max_sequence_length, candidate_limit=settings.candidate_limit, rrf_k=settings.rrf_k, min_vector_score=settings.min_vector_score)
    filter_contract = normalize_evaluation_filter_contract(case.filters, case.id)

    return run_query_v2(
        root,
        case.query,
        scope=scope,
        project=case.filters.get("project"),
        filters=QueryFilters.from_mapping(dict(filter_contract.query)),
        top_k=top_k,
        embedding=embedding,
        telemetry=TelemetrySettings(enabled=False),
        include_context_pack=include_context_pack,
        retrieval_mode=retrieval_mode,
    )


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


def _result_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": item.get("path", ""),
        "score": item.get("score", 0.0),
        "scores": item.get("scores", {}),
        "source_kind": item.get("source_kind", ""),
    }


def _mapping(raw: object, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise RetrievalEvalError(code, message)
    return raw


def _require_schema_version(data: Mapping[str, Any], subject: str) -> None:
    if data.get("schema_version") not in _SUPPORTED_DATASET_SCHEMA_VERSIONS:
        versions = ", ".join(str(value) for value in sorted(_SUPPORTED_DATASET_SCHEMA_VERSIONS))
        raise RetrievalEvalError("unsupported_schema", f"{subject} must use schema_version in {{{versions}}}")


def _nonempty_string(value: object, code: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalEvalError(code, message)
    return value.strip()


def _normalise_relative_path(value: object, case_id: str) -> str:
    path = _nonempty_string(value, "case_invalid", f"case {case_id}: relevant path must be a string")
    try:
        return normalize_vault_relative(path, check_sensitive=False)
    except LocatorError as exc:
        raise RetrievalEvalError("invalid_relevant_path", f"case {case_id}: relevant path must be vault-relative") from exc


def _mean_or_none(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _evaluation_side_effects(
    vault_before: Mapping[str, Any],
    vault_after: Mapping[str, Any],
    telemetry_before: int | None,
    telemetry_after: int | None,
    index_before: Mapping[str, Any],
    index_after: Mapping[str, Any],
) -> dict[str, Any]:
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


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


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

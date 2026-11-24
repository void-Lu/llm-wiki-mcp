"""Latency track: frozen timing_queries.json wall-clock around run_query_v2."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from retrieval.query_pipeline import run_query_v2

__all__ = ["load_timing_queries", "percentile_ms", "run_latency_track"]


def load_timing_queries(path: str | Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("timing queries file must be a JSON object")
    queries = raw.get("queries")
    if not isinstance(queries, list) or len(queries) != 5:
        raise ValueError("timing queries must contain exactly 5 entries")
    out: list[dict[str, Any]] = []
    for item in queries:
        if not isinstance(item, Mapping):
            raise ValueError("timing query entries must be objects")
        query = str(item.get("query") or "").strip()
        qid = str(item.get("id") or "").strip()
        if not query or not qid:
            raise ValueError("timing query requires id and query")
        for banned in ("body", "content", "text", "passage_text"):
            if banned in item:
                raise ValueError(f"timing fixture must not include {banned}")
        out.append(dict(item))
    return out


def percentile_ms(values_ms: Sequence[float], pct: float) -> float:
    if not values_ms:
        return 0.0
    if pct <= 0:
        return float(min(values_ms))
    if pct >= 100:
        return float(max(values_ms))
    ordered = sorted(float(v) for v in values_ms)
    rank = max(1, math.ceil((pct / 100.0) * len(ordered))) - 1
    return ordered[min(rank, len(ordered) - 1)]


def _telemetry_slice(result: Mapping[str, Any]) -> dict[str, Any]:
    pipeline = result.get("pipeline") if isinstance(result.get("pipeline"), Mapping) else {}
    sag2 = pipeline.get("sag2_hop") if isinstance(pipeline.get("sag2_hop"), Mapping) else {}
    out: dict[str, Any] = {}
    for key in ("hops_run", "recovery_reasons", "raw_fallback_mode", "raw_single_shot"):
        if key in sag2:
            out[key] = sag2[key]
    return out


def _load_baseline_latencies(path: str | Path) -> dict[str, float]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = None
    if isinstance(raw, Mapping):
        rows = raw.get("rows") or raw.get("timed") or raw.get("queries")
        if rows is None and isinstance(raw.get("tracks"), Mapping):
            latency = raw["tracks"].get("latency") or {}
            rows = latency.get("rows")
    if not isinstance(rows, list):
        return {}
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        qid = str(row.get("id") or "")
        ms = row.get("latency_ms", row.get("new_ms", row.get("ms")))
        if qid and ms is not None:
            out[qid] = float(ms)
    return out


def run_latency_track(
    vault: str | Path,
    *,
    queries_path: str | Path,
    output_dir: str | Path,
    top_k: int = 10,
    retrieval_mode: str = "lexical",
    baseline_report: str | Path | None = None,
    warmup: bool = True,
) -> dict[str, Any]:
    queries = load_timing_queries(queries_path)
    root = Path(vault)
    if warmup and queries:
        first = queries[0]
        run_query_v2(
            root,
            str(first["query"]),
            retrieval_mode=retrieval_mode,
            top_k=top_k,
            scope=str(first.get("scope") or "knowledge"),
        )
    rows: list[dict[str, Any]] = []
    values: list[float] = []
    for item in queries:
        question = str(item["query"])
        scope = str(item.get("scope") or "knowledge")
        started = perf_counter()
        result = run_query_v2(
            root,
            question,
            retrieval_mode=retrieval_mode,
            top_k=top_k,
            scope=scope,
        )
        elapsed_ms = (perf_counter() - started) * 1000.0
        values.append(elapsed_ms)
        results = list(result.get("results") or []) if isinstance(result, Mapping) else []
        top_path = str(results[0].get("path") or "") if results else ""
        rows.append(
            {
                "id": item["id"],
                "query": question,
                "scope": scope,
                "latency_ms": elapsed_ms,
                "ok": bool(result.get("ok")) if isinstance(result, Mapping) else False,
                "top_path": top_path,
                "telemetry": _telemetry_slice(result) if isinstance(result, Mapping) else {},
            }
        )
    mean_ms = (sum(values) / len(values)) if values else 0.0
    report: dict[str, Any] = {
        "schema_version": 1,
        "query_count": len(rows),
        "mean_ms": mean_ms,
        "p50_ms": percentile_ms(values, 50),
        "p95_ms": percentile_ms(values, 95),
        "rows": rows,
    }
    if baseline_report is not None:
        baseline = _load_baseline_latencies(baseline_report)
        deltas = []
        for row in rows:
            base = baseline.get(str(row["id"]))
            if base is None:
                row["delta_ms"] = None
                continue
            delta = float(row["latency_ms"]) - float(base)
            row["delta_ms"] = delta
            row["baseline_ms"] = base
            deltas.append(delta)
        report["delta_mean_ms"] = (sum(deltas) / len(deltas)) if deltas else None
        report["delta_p50_ms"] = percentile_ms(deltas, 50) if deltas else None
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "latency.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["reports"] = {"json": str(path)}
    return report

"""Heuristic-only weak-label track for the fixed bench suite."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from retrieval.query_pipeline import run_query_v2
from retrieval.retrieval_eval import load_retrieval_dataset
from retrieval.sag2_evidence import HeuristicEvidenceJudge

__all__ = [
    "score_topk_evidence",
    "gold_page_in_topk",
    "citation_coverage",
    "run_weak_label_track",
]


def score_topk_evidence(
    question: str,
    hits: Sequence[Mapping[str, Any]],
    *,
    judge: HeuristicEvidenceJudge | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    if judge is None:
        judge = HeuristicEvidenceJudge()
    if type(judge) is not HeuristicEvidenceJudge:
        raise TypeError("weak-label track allows HeuristicEvidenceJudge only")
    sliced = list(hits)[:top_k]
    scores: list[float] = []
    per_hit: list[dict[str, Any]] = []
    for hit in sliced:
        path = str(hit.get("path") or hit.get("page_path") or "")
        title = str(hit.get("title") or "")
        text = str(
            hit.get("snippet")
            or hit.get("text")
            or hit.get("content")
            or hit.get("passage_text")
            or ""
        )
        score = float(judge.score(question, text, title=title, page_path=path))
        scores.append(score)
        per_hit.append({"path": path, "evidence": score})
    top1 = scores[0] if scores else 0.0
    mean = (sum(scores) / len(scores)) if scores else 0.0
    max_ev = max(scores) if scores else 0.0
    return {
        "scorer": "HeuristicEvidenceJudge",
        "top_k": top_k,
        "top1_evidence": top1,
        "mean_evidence": mean,
        "max_evidence": max_ev,
        "hits": per_hit,
    }


def gold_page_in_topk(
    hit_paths: Sequence[str],
    relevant_paths: Sequence[str],
    *,
    top_k: int = 10,
) -> bool:
    top = {str(p) for p in list(hit_paths)[:top_k]}
    return any(str(path) in top for path in relevant_paths)


def citation_coverage(result: Mapping[str, Any]) -> float | None:
    trace = result.get("citation_trace")
    if not isinstance(trace, Mapping):
        return None
    hits = trace.get("hits")
    if not isinstance(hits, list):
        return None
    results = result.get("results") or []
    denom = len(results) if results else 1
    anchored = sum(1 for hit in hits if str((hit or {}).get("citation") or "").strip())
    return anchored / float(denom)


def run_weak_label_track(
    vault: str | Path,
    dataset_path: str | Path,
    *,
    output_dir: str | Path,
    top_k: int = 10,
    manifest_path: str | Path | None = None,
    retrieval_mode: str = "lexical",
) -> dict[str, Any]:
    dataset = load_retrieval_dataset(dataset_path, manifest_path)
    root = Path(vault)
    case_rows: list[dict[str, Any]] = []
    top1_scores: list[float] = []
    mean_scores: list[float] = []
    gold_hits = 0
    gold_total = 0
    citation_values: list[float] = []
    citation_unavailable = 0
    for case in dataset.cases:
        result = run_query_v2(
            root,
            case.query,
            retrieval_mode=retrieval_mode,
            top_k=top_k,
            scope=case.scope,
        )
        hits = list(result.get("results") or [])
        scored = score_topk_evidence(case.query, hits, top_k=top_k)
        top1_scores.append(float(scored["top1_evidence"]))
        mean_scores.append(float(scored["mean_evidence"]))
        paths = [str(item.get("path") or "") for item in hits]
        relevant = [rel.path for rel in case.relevant]
        in_topk = False
        if case.answerable and relevant:
            gold_total += 1
            in_topk = gold_page_in_topk(paths, relevant, top_k=top_k)
            if in_topk:
                gold_hits += 1
        cov = citation_coverage(result if isinstance(result, Mapping) else {})
        if cov is None:
            citation_unavailable += 1
        else:
            citation_values.append(cov)
        case_rows.append(
            {
                "id": case.id,
                "answerable": case.answerable,
                "gold_in_topk": in_topk,
                "evidence": scored,
                "citation_coverage": cov if cov is not None else "unavailable",
            }
        )
    report = {
        "schema_version": 1,
        "scorer": "HeuristicEvidenceJudge",
        "dataset_id": dataset.manifest.dataset_id,
        "dataset_revision": dataset.manifest.revision,
        "top_k": top_k,
        "case_count": len(case_rows),
        "mean_top1_evidence": (sum(top1_scores) / len(top1_scores)) if top1_scores else 0.0,
        "mean_topk_evidence": (sum(mean_scores) / len(mean_scores)) if mean_scores else 0.0,
        "max_top1_evidence": max(top1_scores) if top1_scores else 0.0,
        "gold_in_topk_rate": (gold_hits / gold_total) if gold_total else None,
        "citation_coverage_mean": (
            (sum(citation_values) / len(citation_values)) if citation_values else "unavailable"
        ),
        "citation_unavailable_cases": citation_unavailable,
        "cases": case_rows,
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "weak-label.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["reports"] = {"json": str(path)}
    return report

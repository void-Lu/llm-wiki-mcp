"""Fixed bench suite orchestrator (local + ci profiles)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from retrieval.bench_latency import run_latency_track
from retrieval.bench_summary import build_bench_summary, write_bench_summary
from retrieval.bench_weak_label import run_weak_label_track
from retrieval.retrieval_eval import (
    load_retrieval_dataset,
    run_retrieval_evaluation,
    write_retrieval_eval_report,
)
from retrieval.retrieval_index import RetrievalIndexStore

DEFAULT_ACCURACY_VAULT = Path("/tmp/sag2-eval/codingwork-vault")
DEFAULT_ACCURACY_DATASET = Path("/tmp/sag2-eval/codingwork-gold.jsonl")
DEFAULT_ACCURACY_MANIFEST = Path("/tmp/sag2-eval/codingwork-gold.manifest.json")
DEFAULT_ACCURACY_R10_MIN = 0.75

__all__ = [
    "DEFAULT_ACCURACY_VAULT",
    "DEFAULT_ACCURACY_DATASET",
    "DEFAULT_ACCURACY_MANIFEST",
    "DEFAULT_ACCURACY_R10_MIN",
    "repo_root",
    "graph_v1_paths",
    "extract_recall_at_10",
    "run_regression_track",
    "run_accuracy_track",
    "run_bench",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def graph_v1_paths() -> dict[str, Path]:
    root = repo_root() / "tests" / "fixtures" / "retrieval" / "graph_v1"
    return {
        "vault": root / "vault",
        "dataset": root / "cases.jsonl",
        "manifest": root / "cases.manifest.json",
    }


def default_timing_queries_path() -> Path:
    return repo_root() / "tests" / "fixtures" / "bench" / "timing_queries.json"


def extract_recall_at_10(report: Mapping[str, Any]) -> float | None:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), Mapping) else {}
    for key in ("recall_at_k_macro", "macro_recall_at_10", "recall_at_10", "R@10"):
        if key in metrics and metrics[key] is not None:
            return float(metrics[key])
    ranking = metrics.get("ranking") if isinstance(metrics.get("ranking"), Mapping) else {}
    for key in ("recall_at_10", "macro_recall_at_10"):
        if key in ranking and ranking[key] is not None:
            return float(ranking[key])
    return None




def _relativize_reports(
    reports: Mapping[str, Any] | None,
    output_dir: Path,
) -> dict[str, str]:
    """Store report paths relative to --output-dir for bench-summary."""
    if not reports:
        return {}
    root = Path(output_dir).resolve()
    out: dict[str, str] = {}
    for key, value in reports.items():
        if value is None:
            continue
        p = Path(str(value))
        try:
            rel = p.resolve().relative_to(root)
        except ValueError:
            # Outside output dir: keep as POSIX-ish relative-looking string if possible
            rel = Path(str(value).replace("\\", "/"))
            if rel.is_absolute():
                # Fall back to name under a stable key rather than leaking abs path
                rel = Path(p.name)
        out[str(key)] = rel.as_posix()
    return out


def _track_failure(exc: BaseException) -> dict[str, Any]:
    msg = f"{type(exc).__name__}: {exc}"
    if len(msg) > 240:
        msg = msg[:237] + "..."
    return {"status": "fail", "reason": msg}


def _default_run_pytest(args: Sequence[str]) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(repo_root()),
        check=False,
    )
    return int(completed.returncode)


def run_regression_track(
    output_dir: Path,
    *,
    run_pytest: Callable[[Sequence[str]], int] | None = None,
) -> dict[str, Any]:
    regress_dir = Path(output_dir) / "regression"
    regress_dir.mkdir(parents=True, exist_ok=True)
    pytest_runner = run_pytest or _default_run_pytest
    pytest_exit = pytest_runner(["tests/retrieval", "-q"])
    paths = graph_v1_paths()
    work_vault = regress_dir / "work-vault"
    if work_vault.exists():
        shutil.rmtree(work_vault)
    shutil.copytree(paths["vault"], work_vault)
    store = RetrievalIndexStore(work_vault)
    store.build(store.iter_vault_pages())
    dataset = load_retrieval_dataset(paths["dataset"], paths["manifest"])
    report = run_retrieval_evaluation(
        work_vault,
        dataset,
        top_k=10,
        measure_context_budget=False,
        retrieval_mode="lexical",
        entrypoint="engine",
        query_version="v2",
    )
    reports = write_retrieval_eval_report(report, regress_dir)
    status = "pass" if pytest_exit == 0 else "fail"
    return {
        "status": status,
        "pytest_exit_code": pytest_exit,
        "work_vault": work_vault,
        "dataset": paths["dataset"],
        "manifest": paths["manifest"],
        "reports": reports,
    }


def run_accuracy_track(
    *,
    vault: Path,
    dataset: Path,
    manifest: Path | None,
    output_dir: Path,
    r10_min: float,
) -> dict[str, Any]:
    if not vault.is_dir() or not dataset.is_file():
        return {"status": "skipped", "reason": "vault_or_dataset_missing"}
    acc_dir = Path(output_dir) / "accuracy"
    acc_dir.mkdir(parents=True, exist_ok=True)
    dataset_obj = load_retrieval_dataset(dataset, manifest)
    report = run_retrieval_evaluation(
        vault,
        dataset_obj,
        top_k=10,
        measure_context_budget=False,
        retrieval_mode="lexical",
        entrypoint="engine",
        query_version="v2",
    )
    reports = write_retrieval_eval_report(report, acc_dir)
    r10 = extract_recall_at_10(report)
    if r10 is None:
        return {
            "status": "fail",
            "reason": "r10_missing",
            "recall_at_10": None,
            "r10_min": float(r10_min),
            "reports": reports,
        }
    status = "pass" if float(r10) >= float(r10_min) else "fail"
    reason = None if status == "pass" else f"FAIL_LOUD: R@10={r10} < {r10_min}"
    return {
        "status": status,
        "recall_at_10": float(r10),
        "r10_min": float(r10_min),
        "reason": reason,
        "reports": reports,
    }


def run_bench(
    *,
    profile: Literal["local", "ci"],
    output_dir: str | Path,
    vault: str | Path | None = None,
    dataset: str | Path | None = None,
    manifest: str | Path | None = None,
    accuracy_r10_min: float = DEFAULT_ACCURACY_R10_MIN,
    timing_queries: str | Path | None = None,
    latency_baseline_report: str | Path | None = None,
    weak_label_vault: str | Path | None = None,
    weak_label_dataset: str | Path | None = None,
    skip_accuracy: bool = False,
    skip_latency: bool = False,
    skip_regression: bool = False,
    skip_weak_label: bool = False,
    run_pytest: Callable[[Sequence[str]], int] | None = None,
) -> tuple[int, dict[str, Any]]:
    started = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    tracks: dict[str, dict[str, Any]] = {}

    regress: dict[str, Any]
    if skip_regression:
        tracks["regression"] = {"status": "skipped", "reason": "skip_regression"}
        regress = {
            "status": "skipped",
            "work_vault": None,
            "dataset": graph_v1_paths()["dataset"],
            "manifest": graph_v1_paths()["manifest"],
        }
    else:
        try:
            regress = run_regression_track(out, run_pytest=run_pytest)
            reports = regress.get("reports") or {}
            tracks["regression"] = {
                "status": regress["status"],
                "pytest_exit_code": regress.get("pytest_exit_code"),
                "reports": _relativize_reports(reports, out),
            }
        except Exception as exc:  # noqa: BLE001 — keep summary writable
            tracks["regression"] = _track_failure(exc)
            regress = {
                "status": "fail",
                "work_vault": None,
                "dataset": graph_v1_paths()["dataset"],
                "manifest": graph_v1_paths()["manifest"],
            }

    accuracy_vault = Path(vault) if vault else DEFAULT_ACCURACY_VAULT
    accuracy_dataset = Path(dataset) if dataset else DEFAULT_ACCURACY_DATASET
    accuracy_manifest = Path(manifest) if manifest else DEFAULT_ACCURACY_MANIFEST

    if profile == "ci" or skip_accuracy:
        tracks["accuracy"] = {
            "status": "skipped",
            "reason": "ci_profile" if profile == "ci" else "skip_accuracy",
        }
    else:
        try:
            accuracy = run_accuracy_track(
                vault=accuracy_vault,
                dataset=accuracy_dataset,
                manifest=accuracy_manifest if accuracy_manifest.is_file() else None,
                output_dir=out,
                r10_min=accuracy_r10_min,
            )
            if isinstance(accuracy.get("reports"), Mapping):
                accuracy = dict(accuracy)
                accuracy["reports"] = _relativize_reports(accuracy.get("reports"), out)
            tracks["accuracy"] = accuracy
        except Exception as exc:  # noqa: BLE001
            tracks["accuracy"] = _track_failure(exc)

    timing_path = Path(timing_queries) if timing_queries else default_timing_queries_path()
    if profile == "ci" or skip_latency:
        tracks["latency"] = {
            "status": "skipped",
            "reason": "ci_profile" if profile == "ci" else "skip_latency",
        }
    elif not timing_path.is_file():
        tracks["latency"] = {"status": "fail", "reason": "timing_fixture_missing"}
    elif not accuracy_vault.is_dir():
        tracks["latency"] = {"status": "skipped", "reason": "vault_missing"}
    else:
        try:
            latency_report = run_latency_track(
                vault=accuracy_vault,
                queries_path=timing_path,
                output_dir=out / "latency",
                baseline_report=latency_baseline_report,
            )
            tracks["latency"] = {
                "status": "report_only",
                "mean_ms": latency_report["mean_ms"],
                "p50_ms": latency_report["p50_ms"],
                "p95_ms": latency_report["p95_ms"],
                "query_count": latency_report["query_count"],
                "reports": _relativize_reports(latency_report.get("reports"), out),
            }
        except Exception as exc:  # noqa: BLE001
            tracks["latency"] = _track_failure(exc)

    if skip_weak_label:
        tracks["weak_label"] = {"status": "skipped", "reason": "skip_weak_label"}
    else:
        try:
            if profile == "ci":
                wl_vault = (
                    Path(regress["work_vault"])
                    if regress.get("work_vault")
                    else graph_v1_paths()["vault"]
                )
                wl_dataset = Path(regress["dataset"])
                wl_manifest = Path(regress["manifest"])
            else:
                wl_vault = (
                    Path(weak_label_vault)
                    if weak_label_vault
                    else (
                        accuracy_vault
                        if accuracy_vault.is_dir()
                        else Path(regress.get("work_vault") or graph_v1_paths()["vault"])
                    )
                )
                wl_dataset = (
                    Path(weak_label_dataset)
                    if weak_label_dataset
                    else (
                        accuracy_dataset
                        if accuracy_dataset.is_file()
                        else graph_v1_paths()["dataset"]
                    )
                )
                wl_manifest = (
                    accuracy_manifest
                    if accuracy_manifest.is_file() and wl_dataset == accuracy_dataset
                    else graph_v1_paths()["manifest"]
                )
            # Keyword-only so monkeypatched fakes that accept **kwargs still work.
            weak = run_weak_label_track(
                vault=wl_vault,
                dataset_path=wl_dataset,
                output_dir=out / "weak_label",
                manifest_path=wl_manifest,
            )
            tracks["weak_label"] = {
                "status": "report_only",
                "mean_top1_evidence": weak.get("mean_top1_evidence"),
                "mean_topk_evidence": weak.get("mean_topk_evidence"),
                "gold_in_topk_rate": weak.get("gold_in_topk_rate"),
                "citation_coverage_mean": weak.get("citation_coverage_mean"),
                "reports": _relativize_reports(weak.get("reports"), out),
            }
        except Exception as exc:  # noqa: BLE001
            tracks["weak_label"] = _track_failure(exc)

    finished = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    summary = build_bench_summary(
        profile=profile,
        started_at=started,
        finished_at=finished,
        tracks=tracks,
    )
    write_bench_summary(summary, out)

    exit_code = 0
    if tracks["regression"]["status"] == "fail":
        exit_code = 2
    if tracks["latency"]["status"] == "fail":
        exit_code = 2
    if profile == "local" and tracks["accuracy"]["status"] == "fail":
        exit_code = 2
    if tracks["weak_label"]["status"] == "fail":
        exit_code = 2
    return exit_code, summary

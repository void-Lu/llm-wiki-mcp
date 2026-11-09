"""Unified bench-summary JSON + Markdown writers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

TrackStatus = Literal["pass", "fail", "skipped", "report_only"]
ProfileName = Literal["local", "ci"]

__all__ = [
    "TrackStatus",
    "ProfileName",
    "build_bench_summary",
    "write_bench_summary",
]


def build_bench_summary(
    *,
    profile: ProfileName,
    started_at: str,
    finished_at: str,
    tracks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    required = ("regression", "accuracy", "latency", "weak_label")
    missing = [name for name in required if name not in tracks]
    if missing:
        raise ValueError(f"bench summary missing tracks: {missing}")
    normalized: dict[str, Any] = {}
    for name in required:
        track = dict(tracks[name])
        status = track.get("status")
        if status not in {"pass", "fail", "skipped", "report_only"}:
            raise ValueError(f"track {name}: invalid status {status!r}")
        normalized[name] = track
    return {
        "schema_version": 1,
        "profile": profile,
        "started_at": started_at,
        "finished_at": finished_at,
        "tracks": normalized,
    }


def write_bench_summary(summary: Mapping[str, Any], output_dir: str | Path) -> dict[str, str]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "bench-summary.json"
    md_path = target / "bench-summary.md"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Bench summary",
        "",
        f"- profile: `{summary.get('profile')}`",
        f"- started_at: {summary.get('started_at')}",
        f"- finished_at: {summary.get('finished_at')}",
        "",
        "| track | status | notes |",
        "| --- | --- | --- |",
    ]
    tracks = summary.get("tracks") or {}
    for name, track in tracks.items():
        status = track.get("status")
        reason = track.get("reason") or track.get("detail") or ""
        if name == "accuracy" and "recall_at_10" in track:
            reason = f"R@10={track['recall_at_10']} (min={track.get('r10_min', '')}) {reason}".strip()
        if name == "latency" and "p50_ms" in track:
            reason = f"p50={track['p50_ms']}ms p95={track.get('p95_ms')}ms {reason}".strip()
        if name == "weak_label" and "mean_top1_evidence" in track:
            reason = f"mean_top1={track['mean_top1_evidence']} {reason}".strip()
        if name == "regression" and "pytest_exit_code" in track:
            reason = f"pytest_exit={track['pytest_exit_code']} {reason}".strip()
        lines.append(f"| {name} | {status} | {reason} |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}

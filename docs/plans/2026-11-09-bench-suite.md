# Fixed Bench Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

I'm using the writing-plans skill to create the implementation plan.

**Goal:** On branch `v1.0.0`, add one CLI entry `bench --profile local|ci` that orchestrates four fixed tracks (regression, accuracy, latency, weak-label), writes unified `bench-summary.json` + `.md`, and hooks CI to `--profile ci` — without BEIR/RAGAS, without outbound judges, and without committing real vault text.

**Architecture:** Thin orchestration over existing tools. Keep logic in small `src/retrieval/bench_*.py` modules (summary schema, weak-label, latency, runner); wire a new `bench` subcommand in `src/app/cli.py` the same way as `retrieval-eval`. Profiles gate which tracks run: **ci** = pytest `tests/retrieval` + graph_v1 `retrieval-eval` + fixture weak-label; **local** = those plus codingwork accuracy (skip if vault/dataset missing) + timing fixture wall-clock. Weak-label scores **only** via `HeuristicEvidenceJudge.score`. No new `src/eval/` package.

**Tech Stack:** Python 3.11+, argparse CLI (`app.cli`), pytest, existing `run_retrieval_evaluation` / `run_query_v2` / `HeuristicEvidenceJudge` / `RetrievalIndexStore`, SQLite FTS5 fixtures under `tests/fixtures/retrieval/graph_v1/`. Test with `uv run pytest` (`pythonpath=src`).

## Global Constraints

- Spec (source of truth): `docs/specs/2026-11-09-bench-suite-design.md` (commit `e3b299c` on `v1.0.0`).
- Package version remains **1.0.0** (no bump unless user asks).
- **Same CLI entry** for local and CI: `uv run llm-wiki-mcp bench --profile local|ci --output-dir <dir>`.
- **Four tracks only** in v1: regression, accuracy, latency, weak-label. No ablation matrix, no answer EM, no BEIR/RAGAS, no vector default path, no hosted/outbound LLM judge, no `LocalLlmEvidenceJudge` on the weak-label track.
- **CI never** runs codingwork accuracy or real-vault latency; missing codingwork paths must **skip + green**, not invent pass.
- **Regression failure → red** (non-zero exit). Weak-label metrics are **report_only** (never red on score). Latency has **no hard gate**. Local accuracy default gate **R@10 ≥ 0.75** (`--accuracy-r10-min`), FAIL_LOUD on miss when the track actually ran.
- Reuse `retrieval-eval` as the only formal ranking metrics engine for accuracy + graph_v1 regression reports.
- Fast track must align with MCP/`run_query_v2` engine path (lexical, Query V2); CI does not require MCP transport.
- Timing fixture: exactly **5** queries in `tests/fixtures/bench/timing_queries.json` (same questions as the 2026-11-09 v0.9.36 A/B); **no** vault body text in the fixture.
- Default local accuracy paths (overridable, **not** committed): `/tmp/sag2-eval/codingwork-vault`, `/tmp/sag2-eval/codingwork-gold.jsonl`, `/tmp/sag2-eval/codingwork-gold.manifest.json`.
- `docs/*`, `tests/fixtures`, and `.github` are gitignored → use `git add -f` for plan/spec/fixture/workflow as needed.
- Add `reports/bench/` to `.gitignore`; never commit real eval reports or vault bodies.
- TDD each task: failing test → fail → implement → pass → commit.
- **Do not push** unless the user asks.

## File map

| File | Role |
|------|------|
| `tests/fixtures/bench/timing_queries.json` | **Create.** Frozen 5-query latency set (id/query/scope/optional tags; no bodies). `git add -f`. |
| `src/retrieval/bench_summary.py` | **Create.** Track status literals, summary dict builder, JSON+Markdown writers. |
| `src/retrieval/bench_weak_label.py` | **Create.** Heuristic-only top-k scoring, gold-in-topk weak hit, citation coverage. |
| `src/retrieval/bench_latency.py` | **Create.** Load timing fixture; wall-clock `run_query_v2` loop; mean/p50/p95; optional baseline Δ. |
| `src/retrieval/bench_runner.py` | **Create.** Profile orchestration: regression → (local accuracy/latency) → weak-label → summary; exit codes. |
| `src/app/cli.py` | **Modify.** Register `bench` parser + `_run_bench`; dispatch in `main`. |
| `tests/retrieval/test_bench_summary.py` | **Create.** Summary schema / writer unit tests. |
| `tests/retrieval/test_bench_weak_label.py` | **Create.** Judge-only scoring + citation/gold helpers. |
| `tests/retrieval/test_bench_latency.py` | **Create.** Fixture load + monkeypatched timing loop. |
| `tests/retrieval/test_bench_runner.py` | **Create.** Profile skip rules, regression failure exit, ci does not touch codingwork paths. |
| `tests/retrieval/test_bench_ci_workflow.py` | **Create.** Assert CI workflow file calls `--profile ci`. |
| `tests/app/test_cli.py` | **Modify.** CLI smoke for `bench --profile ci` + parser flags. |
| `.github/workflows/bench-ci.yml` | **Create.** CI job calling `bench --profile ci`. `git add -f`. |
| `.gitignore` | **Modify.** Ignore `reports/bench/`. |
| `README.md` | **Modify.** Short “Bench 套件” subsection (local four tracks / ci two tracks). |
| `docs/specs/2026-11-09-bench-suite-design.md` | Spec (read-only during impl). |
| `docs/plans/2026-11-09-bench-suite.md` | This plan. |

### Current anchors (do not invent)

CLI registration today (`src/app/cli.py`):

```python
evaluation_parser = subparsers.add_parser("retrieval-eval", ...)
# ...
if args.command == "retrieval-eval":
    return _run_retrieval_eval(args)
```

Entry script: `llm-wiki-mcp = "app.cli:main"` in `pyproject.toml`.

Eval in-process API: `load_retrieval_dataset`, `run_retrieval_evaluation`, `write_retrieval_eval_report` from `retrieval.retrieval_eval`.

Engine path: `run_query_v2(root, question, retrieval_mode="lexical", top_k=..., scope=...)`.

Judge: `HeuristicEvidenceJudge().score(question, passage_text, title=..., page_path=...)` → float in `[0,1]`.

Public result always includes `citation_trace` (body-free) with `hits[].citation` like `"[1]"`.

graph_v1: `tests/fixtures/retrieval/graph_v1/{vault,cases.jsonl,cases.manifest.json}` — copy vault before `RetrievalIndexStore.build` (never dirty the fixture tree).

Metrics key for R@10: prefer `metrics["recall_at_k_macro"]` when eval `top_k==10` (existing CLI tests assert this name); also accept nested `metrics["ranking"]["recall_at_10"]`. Helper: `extract_recall_at_10(report) -> float | None`.

---

### Task 1: Timing fixture + summary schema

**Files:**
- Create: `tests/fixtures/bench/timing_queries.json`
- Create: `src/retrieval/bench_summary.py`
- Test: `tests/retrieval/test_bench_summary.py`

**Interfaces:**
- Consumes: none.
- Produces:
  ```python
  TrackStatus = Literal["pass", "fail", "skipped", "report_only"]

  def build_bench_summary(
      *,
      profile: Literal["local", "ci"],
      started_at: str,
      finished_at: str,
      tracks: Mapping[str, Mapping[str, Any]],
  ) -> dict[str, Any]:
      """Return JSON-serializable summary with profile, timestamps, tracks."""

  def write_bench_summary(summary: Mapping[str, Any], output_dir: str | Path) -> dict[str, str]:
      """Write bench-summary.json + bench-summary.md; return {'json': ..., 'markdown': ...}."""
  ```
  Fixture shape is validated by reading the JSON file in tests. `load_timing_queries` arrives in Task 3 (`bench_latency.py`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_bench_summary.py
from __future__ import annotations

import json
from pathlib import Path

from retrieval.bench_summary import build_bench_summary, write_bench_summary

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMING_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bench" / "timing_queries.json"


def test_timing_queries_fixture_has_five_frozen_queries() -> None:
    assert TIMING_FIXTURE.is_file(), "timing_queries.json must be committed (git add -f)"
    payload = json.loads(TIMING_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["dataset_id"] == "bench-timing-v1"
    queries = payload["queries"]
    assert len(queries) == 5
    ids = [q["id"] for q in queries]
    assert ids == [
        "codingwork-0015",
        "codingwork-0001",
        "codingwork-0004",
        "codingwork-0027",
        "codingwork-0020",
    ]
    for q in queries:
        assert isinstance(q["query"], str) and q["query"].strip()
        assert "body" not in q and "content" not in q and "text" not in q
        assert q.get("answerable", True) is True


def test_build_and_write_bench_summary(tmp_path: Path) -> None:
    summary = build_bench_summary(
        profile="ci",
        started_at="2026-11-09T00:00:00+08:00",
        finished_at="2026-11-09T00:01:00+08:00",
        tracks={
            "regression": {
                "status": "pass",
                "pytest_exit_code": 0,
                "reports": {"retrieval_eval": "regression/retrieval-eval.json"},
            },
            "accuracy": {"status": "skipped", "reason": "ci_profile"},
            "latency": {"status": "skipped", "reason": "ci_profile"},
            "weak_label": {
                "status": "report_only",
                "mean_top1_evidence": 0.4,
                "reports": {"json": "weak_label/weak-label.json"},
            },
        },
    )
    assert summary["profile"] == "ci"
    assert summary["tracks"]["accuracy"]["status"] == "skipped"
    assert summary["tracks"]["regression"]["status"] == "pass"
    paths = write_bench_summary(summary, tmp_path)
    assert Path(paths["json"]).name == "bench-summary.json"
    assert Path(paths["markdown"]).name == "bench-summary.md"
    loaded = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert loaded["tracks"]["weak_label"]["status"] == "report_only"
    md = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "regression" in md and "pass" in md
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_bench_summary.py -v
```

Expected: FAIL — `ModuleNotFoundError: retrieval.bench_summary` and/or missing fixture file.

- [ ] **Step 3: Write minimal implementation**

Create `tests/fixtures/bench/timing_queries.json`:

```json
{
  "schema_version": 1,
  "dataset_id": "bench-timing-v1",
  "revision": "2026-11-09.1",
  "notes": "Frozen wall-clock set matching 2026-11-09 v0.9.36 vs v1.0.0 A/B (seed 20261109). Queries only — no vault bodies.",
  "queries": [
    {
      "id": "codingwork-0015",
      "query": "unauthenticated client-side contexts N/search module",
      "scope": "auto",
      "language": "en",
      "answerable": true,
      "tags": ["latin", "identifier", "slow-hop"]
    },
    {
      "id": "codingwork-0001",
      "query": "Suitelet N/llm ChatBot Tooling 代码示例",
      "scope": "knowledge",
      "language": "mixed",
      "answerable": true,
      "tags": ["cjk-mixed", "identifier"]
    },
    {
      "id": "codingwork-0004",
      "query": "N/workflow 模块 方法 列表",
      "scope": "auto",
      "language": "mixed",
      "answerable": true,
      "tags": ["cjk-mixed", "identifier", "slow-hop"]
    },
    {
      "id": "codingwork-0027",
      "query": "serverWidget createForm addField Suitelet",
      "scope": "all",
      "language": "en",
      "answerable": true,
      "tags": ["latin", "identifier", "camelcase"]
    },
    {
      "id": "codingwork-0020",
      "query": "MapReduce 脚本各阶段执行限制 getInputData map reduce summarize 治理单位 限制",
      "scope": "auto",
      "language": "mixed",
      "answerable": true,
      "tags": ["cjk-mixed", "short-cache-or-fast"]
    }
  ]
}
```

Create `src/retrieval/bench_summary.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_bench_summary.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -f tests/fixtures/bench/timing_queries.json
git add src/retrieval/bench_summary.py tests/retrieval/test_bench_summary.py
git commit -m "$(cat <<'EOF'
feat(bench): add timing fixture and bench-summary writers

EOF
)"
```

---

### Task 2: Weak-label module (HeuristicEvidenceJudge only)

**Files:**
- Create: `src/retrieval/bench_weak_label.py`
- Test: `tests/retrieval/test_bench_weak_label.py`

**Interfaces:**
- Consumes: `HeuristicEvidenceJudge` from `retrieval.sag2_evidence`; `run_query_v2` for the track runner.
- Produces:
  ```python
  def score_topk_evidence(
      question: str,
      hits: Sequence[Mapping[str, Any]],
      *,
      judge: HeuristicEvidenceJudge | None = None,
      top_k: int = 10,
  ) -> dict[str, Any]: ...

  def gold_page_in_topk(
      hit_paths: Sequence[str],
      relevant_paths: Sequence[str],
      *,
      top_k: int = 10,
  ) -> bool: ...

  def citation_coverage(result: Mapping[str, Any]) -> float | None: ...

  def run_weak_label_track(
      vault: str | Path,
      dataset_path: str | Path,
      *,
      output_dir: str | Path,
      top_k: int = 10,
      manifest_path: str | Path | None = None,
      retrieval_mode: str = "lexical",
  ) -> dict[str, Any]: ...
  ```

**Lock decisions:**
- Instantiate **only** `HeuristicEvidenceJudge()`; never construct `LocalLlmEvidenceJudge` here. Reject non-heuristic judge with `TypeError`.
- Passage text: `snippet` / `text` / `content` / `passage_text` / `""`; title from `title`; path from `path` or `page_path`.
- `citation_coverage`: missing `citation_trace` → `None` (caller stores `"unavailable"`). Else `(# hits with non-empty citation) / max(len(results), 1)`.
- Gold weak hit: any relevant path in the first `top_k` **primary** `results` paths only (not `additional_results`).
- Track status in summary is always `report_only` when the track completes; runner crash still fails the process.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_bench_weak_label.py
from __future__ import annotations

from retrieval.bench_weak_label import (
    citation_coverage,
    gold_page_in_topk,
    score_topk_evidence,
)
from retrieval.sag2_evidence import HeuristicEvidenceJudge


def test_score_topk_evidence_uses_heuristic_judge_only() -> None:
    hits = [
        {
            "path": "wiki/concepts/invoice.md",
            "title": "Invoice",
            "snippet": "invoice approval workflow documented here",
        },
        {
            "path": "wiki/concepts/other.md",
            "title": "Other",
            "snippet": "unrelated gardening tips",
        },
    ]
    out = score_topk_evidence("invoice approval", hits, top_k=2)
    assert 0.0 <= out["top1_evidence"] <= 1.0
    assert 0.0 <= out["mean_evidence"] <= 1.0
    assert 0.0 <= out["max_evidence"] <= 1.0
    assert out["scorer"] == "HeuristicEvidenceJudge"
    out2 = score_topk_evidence(
        "invoice approval", hits, judge=HeuristicEvidenceJudge(), top_k=2
    )
    assert out2["scorer"] == "HeuristicEvidenceJudge"


def test_gold_page_in_topk_and_citation_coverage() -> None:
    assert gold_page_in_topk(
        ["wiki/a.md", "wiki/b.md"], ["wiki/b.md"], top_k=2
    )
    assert not gold_page_in_topk(
        ["wiki/a.md", "wiki/b.md"], ["wiki/c.md"], top_k=2
    )
    assert not gold_page_in_topk(
        ["wiki/a.md", "wiki/b.md"], ["wiki/b.md"], top_k=1
    )
    covered = citation_coverage(
        {
            "results": [{"path": "wiki/a.md"}, {"path": "wiki/b.md"}],
            "citation_trace": {
                "hits": [
                    {"citation": "[1]", "path": "wiki/a.md"},
                    {"citation": "[2]", "path": "wiki/b.md"},
                ]
            },
        }
    )
    assert covered == 1.0
    assert citation_coverage({"results": [{"path": "wiki/a.md"}]}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_bench_weak_label.py -v
```

Expected: FAIL — missing module.

- [ ] **Step 3: Write minimal implementation**

```python
# src/retrieval/bench_weak_label.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_bench_weak_label.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/bench_weak_label.py tests/retrieval/test_bench_weak_label.py
git commit -m "$(cat <<'EOF'
feat(bench): add heuristic-only weak-label scoring

EOF
)"
```

---

### Task 3: Latency track (timing_queries.json wall-clock)

**Files:**
- Create: `src/retrieval/bench_latency.py`
- Test: `tests/retrieval/test_bench_latency.py`

**Interfaces:**
- Consumes: timing fixture from Task 1; `run_query_v2`.
- Produces:
  ```python
  def load_timing_queries(path: str | Path) -> list[dict[str, Any]]: ...

  def percentile_ms(values_ms: Sequence[float], pct: float) -> float:
      """Nearest-rank percentile; pct in 0..100."""

  def run_latency_track(
      vault: str | Path,
      *,
      queries_path: str | Path,
      output_dir: str | Path,
      top_k: int = 10,
      retrieval_mode: str = "lexical",
      baseline_report: str | Path | None = None,
      warmup: bool = True,
  ) -> dict[str, Any]: ...
  ```

**Lock:**
- Wall clock via `time.perf_counter()` around `run_query_v2`.
- Per query record: `id`, `query`, `latency_ms`, `ok`, `top_path`, telemetry subset `hops_run` / `recovery_reasons` / `raw_fallback_mode` / `raw_single_shot` from `pipeline.sag2_hop` when present.
- Aggregates: `mean_ms`, `p50_ms`, `p95_ms`.
- Baseline JSON may use `rows` / `timed` / `queries` with `latency_ms` or `new_ms`; compute per-id Δ. **Never** fail due to slowdown.
- Optional warmup of the first query when `warmup=True` (not counted).

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_bench_latency.py
from __future__ import annotations

from pathlib import Path

import pytest

from retrieval.bench_latency import load_timing_queries, percentile_ms, run_latency_track

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMING_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "bench" / "timing_queries.json"


def test_load_timing_queries_and_percentiles() -> None:
    queries = load_timing_queries(TIMING_FIXTURE)
    assert len(queries) == 5
    assert queries[0]["id"] == "codingwork-0015"
    assert percentile_ms([10.0, 20.0, 30.0, 40.0, 50.0], 50) == 30.0
    assert percentile_ms([10.0, 20.0, 30.0, 40.0, 50.0], 95) >= 50.0


def test_run_latency_track_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_latency as latency_mod

    calls: list[str] = []

    def fake_run_query_v2(root, question, **kwargs):  # noqa: ANN001
        calls.append(question)
        return {
            "ok": True,
            "results": [{"path": "wiki/x.md"}],
            "pipeline": {
                "sag2_hop": {
                    "hops_run": 1,
                    "recovery_reasons": [],
                    "raw_fallback_mode": "none",
                }
            },
        }

    monkeypatch.setattr(latency_mod, "run_query_v2", fake_run_query_v2)
    fixture = tmp_path / "queries.json"
    fixture.write_text(TIMING_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    report = run_latency_track(
        tmp_path / "vault",
        queries_path=fixture,
        output_dir=tmp_path / "latency",
        warmup=True,
    )
    assert report["query_count"] == 5
    assert "mean_ms" in report and "p50_ms" in report and "p95_ms" in report
    assert Path(report["reports"]["json"]).is_file()
    assert len(calls) == 6  # warmup + 5 timed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_bench_latency.py -v
```

Expected: FAIL — missing `bench_latency`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/retrieval/bench_latency.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_bench_latency.py tests/retrieval/test_bench_summary.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/bench_latency.py tests/retrieval/test_bench_latency.py
git commit -m "$(cat <<'EOF'
feat(bench): add latency track over frozen timing queries

EOF
)"
```

---

### Task 4: Bench runner — profiles, regression, accuracy, weak-label wiring

**Files:**
- Create: `src/retrieval/bench_runner.py`
- Test: `tests/retrieval/test_bench_runner.py`

**Interfaces:**
- Consumes: Tasks 1–3 modules; `run_retrieval_evaluation` / `write_retrieval_eval_report` / `load_retrieval_dataset`; `RetrievalIndexStore`; `run_weak_label_track`; `run_latency_track`.
- Produces:
  ```python
  DEFAULT_ACCURACY_VAULT = Path("/tmp/sag2-eval/codingwork-vault")
  DEFAULT_ACCURACY_DATASET = Path("/tmp/sag2-eval/codingwork-gold.jsonl")
  DEFAULT_ACCURACY_MANIFEST = Path("/tmp/sag2-eval/codingwork-gold.manifest.json")
  DEFAULT_ACCURACY_R10_MIN = 0.75

  def repo_root() -> Path: ...
  def graph_v1_paths() -> dict[str, Path]: ...
  def extract_recall_at_10(report: Mapping[str, Any]) -> float | None: ...
  def run_regression_track(output_dir: Path, *, run_pytest: Callable[..., int] | None = None) -> dict[str, Any]: ...
  def run_accuracy_track(...) -> dict[str, Any]: ...
  def run_bench(*, profile: Literal["local", "ci"], output_dir: str | Path, ...) -> tuple[int, dict[str, Any]]: ...
  ```

**Lock — profile scheduling:**

| profile | regression | accuracy | latency | weak_label |
|---------|------------|----------|---------|------------|
| `ci` | yes | skipped (`ci_profile`) | skipped (`ci_profile`) | graph_v1 work-vault + cases |
| `local` | yes | codingwork if paths exist else skipped | timing fixture; vault missing → skipped `vault_missing`; timing file missing → **fail** | weak-label vault/dataset override or accuracy vault / else graph_v1 |

**Regression:**
1. `sys.executable -m pytest tests/retrieval -q` (injectable `run_pytest`).
2. Copy graph_v1 vault → `output_dir/regression/work-vault`, build index, `run_retrieval_evaluation(..., measure_context_budget=False)`, write reports under `output_dir/regression/`.
3. pytest ≠ 0 → track `fail`, bench exit 2.
4. No frozen production baseline required in v1.

**Accuracy:** `top_k=10`; `extract_recall_at_10` reads `recall_at_k_macro` first; compare to `accuracy_r10_min` (default 0.75); FAIL_LOUD reason string on miss.

**Exit codes:** `0` ok; `2` if regression fail **or** (local and accuracy status `fail`). Weak-label/latency metrics never flip exit code.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_bench_runner.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval.bench_runner import extract_recall_at_10, run_bench


def test_extract_recall_at_10_prefers_macro_at_k() -> None:
    assert extract_recall_at_10({"metrics": {"recall_at_k_macro": 0.84}}) == pytest.approx(0.84)
    assert extract_recall_at_10({"metrics": {"ranking": {"recall_at_10": 0.5}}}) == pytest.approx(0.5)
    assert extract_recall_at_10({"metrics": {}}) is None


def test_ci_profile_skips_accuracy_and_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_runner as runner

    work = tmp_path / "work-vault"
    work.mkdir()
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "cases.manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        runner,
        "run_regression_track",
        lambda output_dir, **kwargs: {
            "status": "pass",
            "pytest_exit_code": 0,
            "work_vault": work,
            "dataset": dataset,
            "manifest": manifest,
            "reports": {},
        },
    )

    def fake_weak(**kwargs):  # noqa: ANN001
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        path = out / "weak-label.json"
        path.write_text("{}\n", encoding="utf-8")
        return {
            "mean_top1_evidence": 0.25,
            "mean_topk_evidence": 0.2,
            "gold_in_topk_rate": 0.0,
            "citation_coverage_mean": "unavailable",
            "reports": {"json": str(path)},
        }

    monkeypatch.setattr(runner, "run_weak_label_track", fake_weak)

    codingwork = Path("/tmp/sag2-eval/codingwork-vault-should-not-be-touched-by-ci")
    exit_code, summary = run_bench(
        profile="ci",
        output_dir=tmp_path / "out",
        vault=codingwork,
    )
    assert exit_code == 0
    assert summary["tracks"]["accuracy"]["status"] == "skipped"
    assert summary["tracks"]["latency"]["status"] == "skipped"
    assert summary["tracks"]["weak_label"]["status"] == "report_only"
    assert summary["tracks"]["regression"]["status"] == "pass"
    assert not codingwork.exists()


def test_regression_failure_sets_exit_code_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_runner as runner

    monkeypatch.setattr(
        runner,
        "run_regression_track",
        lambda output_dir, **kwargs: {
            "status": "fail",
            "pytest_exit_code": 1,
            "work_vault": tmp_path,
            "dataset": tmp_path / "c.jsonl",
            "manifest": tmp_path / "c.manifest.json",
            "reports": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "run_weak_label_track",
        lambda **kwargs: {
            "mean_top1_evidence": 0.0,
            "reports": {"json": str(tmp_path / "w.json")},
        },
    )
    (tmp_path / "w.json").write_text("{}\n", encoding="utf-8")
    exit_code, summary = run_bench(profile="ci", output_dir=tmp_path / "out2")
    assert exit_code == 2
    assert summary["tracks"]["regression"]["status"] == "fail"


def test_local_accuracy_gate_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import retrieval.bench_runner as runner

    monkeypatch.setattr(
        runner,
        "run_regression_track",
        lambda output_dir, **kwargs: {
            "status": "pass",
            "pytest_exit_code": 0,
            "work_vault": tmp_path,
            "dataset": tmp_path / "c.jsonl",
            "manifest": tmp_path / "m.json",
            "reports": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "run_accuracy_track",
        lambda **kwargs: {
            "status": "fail",
            "recall_at_10": 0.5,
            "r10_min": 0.75,
            "reason": "FAIL_LOUD: R@10=0.5 < 0.75",
            "reports": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "run_latency_track",
        lambda **kwargs: {
            "mean_ms": 1.0,
            "p50_ms": 1.0,
            "p95_ms": 1.0,
            "query_count": 5,
            "reports": {"json": str(tmp_path / "lat.json")},
        },
    )
    (tmp_path / "lat.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "run_weak_label_track",
        lambda **kwargs: {
            "mean_top1_evidence": 0.1,
            "reports": {"json": str(tmp_path / "w.json")},
        },
    )
    (tmp_path / "w.json").write_text("{}\n", encoding="utf-8")
    # Pretend vault+dataset exist so local does not skip before calling accuracy.
    vault = tmp_path / "vault"
    vault.mkdir()
    dataset = tmp_path / "gold.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    exit_code, summary = run_bench(
        profile="local",
        output_dir=tmp_path / "out3",
        vault=vault,
        dataset=dataset,
        skip_latency=True,
    )
    assert exit_code == 2
    assert summary["tracks"]["accuracy"]["status"] == "fail"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_bench_runner.py -v
```

Expected: FAIL — missing `bench_runner`.

- [ ] **Step 3: Implement runner**

Create `src/retrieval/bench_runner.py` with the following structure (implement fully; do not leave stubs):

```python
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
    for key in ("recall_at_10", "macro_recall_at_10", "recall_at_k_macro", "R@10"):
        if key in metrics and metrics[key] is not None:
            return float(metrics[key])
    ranking = metrics.get("ranking") if isinstance(metrics.get("ranking"), Mapping) else {}
    for key in ("recall_at_10", "macro_recall_at_10"):
        if key in ranking and ranking[key] is not None:
            return float(ranking[key])
    return None


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

    if skip_regression:
        tracks["regression"] = {"status": "skipped", "reason": "skip_regression"}
        regress = {
            "status": "skipped",
            "work_vault": None,
            "dataset": graph_v1_paths()["dataset"],
            "manifest": graph_v1_paths()["manifest"],
        }
    else:
        regress = run_regression_track(out, run_pytest=run_pytest)
        tracks["regression"] = {
            "status": regress["status"],
            "pytest_exit_code": regress.get("pytest_exit_code"),
            "reports": {
                key: str(Path(value).relative_to(out)) if Path(value).is_absolute() and str(value).startswith(str(out)) else value
                for key, value in (regress.get("reports") or {}).items()
            }
            if regress.get("reports")
            else {},
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
        tracks["accuracy"] = run_accuracy_track(
            vault=accuracy_vault,
            dataset=accuracy_dataset,
            manifest=accuracy_manifest if accuracy_manifest.is_file() else None,
            output_dir=out,
            r10_min=accuracy_r10_min,
        )

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
        latency_report = run_latency_track(
            accuracy_vault,
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
            "reports": latency_report.get("reports"),
        }

    if skip_weak_label:
        tracks["weak_label"] = {"status": "skipped", "reason": "skip_weak_label"}
    else:
        if profile == "ci":
            wl_vault = Path(regress["work_vault"]) if regress.get("work_vault") else graph_v1_paths()["vault"]
            wl_dataset = Path(regress["dataset"])
            wl_manifest = Path(regress["manifest"])
        else:
            wl_vault = Path(weak_label_vault) if weak_label_vault else (
                accuracy_vault if accuracy_vault.is_dir() else Path(regress.get("work_vault") or graph_v1_paths()["vault"])
            )
            wl_dataset = Path(weak_label_dataset) if weak_label_dataset else (
                accuracy_dataset if accuracy_dataset.is_file() else graph_v1_paths()["dataset"]
            )
            wl_manifest = (
                accuracy_manifest
                if accuracy_manifest.is_file() and wl_dataset == accuracy_dataset
                else graph_v1_paths()["manifest"]
            )
        weak = run_weak_label_track(
            wl_vault,
            wl_dataset,
            output_dir=out / "weak_label",
            manifest_path=wl_manifest,
        )
        tracks["weak_label"] = {
            "status": "report_only",
            "mean_top1_evidence": weak.get("mean_top1_evidence"),
            "mean_topk_evidence": weak.get("mean_topk_evidence"),
            "gold_in_topk_rate": weak.get("gold_in_topk_rate"),
            "citation_coverage_mean": weak.get("citation_coverage_mean"),
            "reports": weak.get("reports"),
        }

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
    return exit_code, summary
```

Simplify relative report paths if the `relative_to` logic is brittle — storing absolute paths from `write_retrieval_eval_report` is fine for v1; keep whatever the helpers return.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_bench_runner.py tests/retrieval/test_bench_summary.py tests/retrieval/test_bench_weak_label.py tests/retrieval/test_bench_latency.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/bench_runner.py tests/retrieval/test_bench_runner.py
git commit -m "$(cat <<'EOF'
feat(bench): orchestrate local/ci profiles and regression track

EOF
)"
```

---

### Task 5: CLI `bench` + flag wiring

**Files:**
- Modify: `src/app/cli.py`
- Modify: `tests/app/test_cli.py`

**Interfaces:**
- Consumes: `run_bench` from `retrieval.bench_runner` (module-level import so tests can monkeypatch `app.cli.run_bench`).
- Produces CLI:

```text
llm-wiki-mcp bench
  --profile {local,ci}           (required)
  --output-dir PATH              (required)
  --vault PATH
  --dataset PATH
  --manifest PATH
  --accuracy-r10-min FLOAT       (default 0.75)
  --timing-queries PATH
  --latency-baseline-report PATH
  --weak-label-vault PATH
  --weak-label-dataset PATH
  --skip-accuracy
  --skip-latency
  --skip-regression
  --skip-weak-label
```

Print JSON `{ok, profile, summary, output_dir}`; return `run_bench` exit code.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/app/test_cli.py
def test_bench_cli_profile_ci_invokes_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import app.cli as cli_mod

    calls: dict[str, object] = {}

    def fake_run_bench(**kwargs):  # noqa: ANN001
        calls.update(kwargs)
        summary = {
            "schema_version": 1,
            "profile": kwargs["profile"],
            "started_at": "t0",
            "finished_at": "t1",
            "tracks": {
                "regression": {"status": "pass", "pytest_exit_code": 0},
                "accuracy": {"status": "skipped", "reason": "ci_profile"},
                "latency": {"status": "skipped", "reason": "ci_profile"},
                "weak_label": {"status": "report_only", "mean_top1_evidence": 0.1},
            },
        }
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "bench-summary.json").write_text("{}\n", encoding="utf-8")
        return 0, summary

    monkeypatch.setattr(cli_mod, "run_bench", fake_run_bench)
    exit_code = main(
        [
            "bench",
            "--profile",
            "ci",
            "--output-dir",
            str(tmp_path / "bench-out"),
        ]
    )
    assert exit_code == 0
    assert calls["profile"] == "ci"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["profile"] == "ci"


def test_bench_parser_requires_profile_and_output_dir() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bench"])
    args = parser.parse_args(["bench", "--profile", "local", "--output-dir", "/tmp/x"])
    assert args.command == "bench"
    assert args.profile == "local"
    assert args.accuracy_r10_min == 0.75
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest \
  tests/app/test_cli.py::test_bench_cli_profile_ci_invokes_runner \
  tests/app/test_cli.py::test_bench_parser_requires_profile_and_output_dir \
  -v
```

Expected: FAIL — unknown `bench` command / missing attribute.

- [ ] **Step 3: Implement CLI**

Import near other retrieval imports in `src/app/cli.py`:

```python
from retrieval.bench_runner import run_bench
```

In `_build_parser`, after the `retrieval-eval` block:

```python
    bench_parser = subparsers.add_parser(
        "bench",
        help="Run the fixed local/CI retrieval bench suite (regression, accuracy, latency, weak-label).",
    )
    bench_parser.add_argument("--profile", required=True, choices=("local", "ci"))
    bench_parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for bench-summary and per-track reports.",
    )
    bench_parser.add_argument("--vault", help="Local accuracy/latency vault root override.")
    bench_parser.add_argument("--dataset", help="Local accuracy JSONL dataset override.")
    bench_parser.add_argument("--manifest", help="Local accuracy manifest override.")
    bench_parser.add_argument("--accuracy-r10-min", type=float, default=0.75)
    bench_parser.add_argument(
        "--timing-queries",
        help="Latency queries JSON (default: tests/fixtures/bench/timing_queries.json).",
    )
    bench_parser.add_argument(
        "--latency-baseline-report",
        help="Optional prior latency JSON for delta reporting.",
    )
    bench_parser.add_argument("--weak-label-vault", help="Weak-label vault override.")
    bench_parser.add_argument("--weak-label-dataset", help="Weak-label dataset override.")
    bench_parser.add_argument("--skip-accuracy", action="store_true")
    bench_parser.add_argument("--skip-latency", action="store_true")
    bench_parser.add_argument("--skip-regression", action="store_true")
    bench_parser.add_argument("--skip-weak-label", action="store_true")
```

Add runner:

```python
def _run_bench(args: argparse.Namespace) -> int:
    exit_code, summary = run_bench(
        profile=args.profile,
        output_dir=args.output_dir,
        vault=args.vault,
        dataset=args.dataset,
        manifest=args.manifest,
        accuracy_r10_min=args.accuracy_r10_min,
        timing_queries=args.timing_queries,
        latency_baseline_report=args.latency_baseline_report,
        weak_label_vault=args.weak_label_vault,
        weak_label_dataset=args.weak_label_dataset,
        skip_accuracy=args.skip_accuracy,
        skip_latency=args.skip_latency,
        skip_regression=args.skip_regression,
        skip_weak_label=args.skip_weak_label,
    )
    _print_json(
        {
            "ok": exit_code == 0,
            "profile": args.profile,
            "summary": summary,
            "output_dir": str(Path(args.output_dir)),
        }
    )
    return exit_code
```

In `main` try-dispatch:

```python
        if args.command == "bench":
            return _run_bench(args)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest \
  tests/app/test_cli.py::test_bench_cli_profile_ci_invokes_runner \
  tests/app/test_cli.py::test_bench_parser_requires_profile_and_output_dir \
  tests/retrieval/test_bench_runner.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/app/cli.py tests/app/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): add bench --profile local|ci entrypoint

EOF
)"
```

---

### Task 6: README, CI workflow, gitignore

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Create: `.github/workflows/bench-ci.yml` (`git add -f`)
- Create: `tests/retrieval/test_bench_ci_workflow.py`

**Interfaces:** none new in Python.

- [ ] **Step 1: Write the failing workflow presence test**

```python
# tests/retrieval/test_bench_ci_workflow.py
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_bench_ci_workflow_calls_profile_ci() -> None:
    path = REPO / ".github" / "workflows" / "bench-ci.yml"
    assert path.is_file(), "bench-ci.yml must be force-added (git add -f)"
    text = path.read_text(encoding="utf-8")
    assert "--profile ci" in text
    assert "uv run" in text
    assert "llm-wiki-mcp bench" in text


def test_readme_documents_bench_suite() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "bench --profile" in readme
    assert "HeuristicEvidenceJudge" in readme or "弱标签" in readme
    assert "R@10" in readme or "0.75" in readme
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/retrieval/test_bench_ci_workflow.py -v
```

Expected: FAIL — missing workflow and/or README section.

- [ ] **Step 3: Add workflow, gitignore, README**

Append to `.gitignore`:

```gitignore
reports/bench/
```

Create `.github/workflows/bench-ci.yml`:

```yaml
name: bench-ci

on:
  push:
  pull_request:

jobs:
  bench:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with:
          enable-cache: true
      - name: Sync dev deps
        run: uv sync --extra dev
      - name: Fixed bench suite (ci profile)
        run: |
          mkdir -p reports/bench/ci
          uv run llm-wiki-mcp bench --profile ci --output-dir reports/bench/ci
      - name: Upload bench summary
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: bench-summary-ci
          path: reports/bench/ci
```

In README CLI list (near `retrieval-eval`), add:

```bash
uv run llm-wiki-mcp bench --profile local|ci --output-dir <reports-dir>
```

Under `## 开发`, after the pytest examples, add:

```markdown
### Bench 套件

同一入口覆盖回归 / 准 / 快 / 弱标签（弱标签仅 `HeuristicEvidenceJudge`，无外呼）：

```bash
# 本地四轨（codingwork vault/dataset 缺省路径不存在时准轨 skipped，不假装达标）
uv run llm-wiki-mcp bench --profile local --output-dir reports/bench/local

# CI 两轨：pytest tests/retrieval + graph_v1 retrieval-eval + fixture 弱标签
uv run llm-wiki-mcp bench --profile ci --output-dir reports/bench/ci
```

- 准轨默认门禁 **R@10 ≥ 0.75**（`--accuracy-r10-min` 可改）；仅 local 未达标时非零退出。
- 快轨读取 `tests/fixtures/bench/timing_queries.json`（5 题），无硬门禁。
- 弱标签只出报告（`report_only`），不因伪分异红。
- 报告目录建议 `reports/bench/<run>/`（已 gitignore）；不要提交真实 vault 正文。
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest \
  tests/retrieval/test_bench_ci_workflow.py \
  tests/retrieval/test_bench_summary.py \
  tests/retrieval/test_bench_weak_label.py \
  tests/retrieval/test_bench_latency.py \
  tests/retrieval/test_bench_runner.py \
  tests/app/test_cli.py::test_bench_cli_profile_ci_invokes_runner \
  tests/app/test_cli.py::test_bench_parser_requires_profile_and_output_dir \
  -v
```

Expected: PASS.

Manual smoke (informational; do not fail Task 6 if codingwork vault absent):

```bash
mkdir -p /tmp/bench-smoke-ci
uv run llm-wiki-mcp bench --profile ci --output-dir /tmp/bench-smoke-ci
# optional local:
# mkdir -p /tmp/bench-smoke-local
# uv run llm-wiki-mcp bench --profile local --output-dir /tmp/bench-smoke-local --skip-latency
```

- [ ] **Step 5: Commit**

```bash
git add -f .github/workflows/bench-ci.yml
git add -f tests/retrieval/test_bench_ci_workflow.py
git add .gitignore README.md
git commit -m "$(cat <<'EOF'
docs(ci): wire bench --profile ci workflow and README

EOF
)"
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| CLI `bench --profile local\|ci` + `--output-dir` + overrides | Task 5 (+ runner Task 4) |
| Regression: pytest `tests/retrieval` + graph_v1 `retrieval-eval` | Task 4 |
| Accuracy: codingwork local-only, R@10≥0.75, skip if missing | Task 4 |
| Latency: `timing_queries.json` 5 queries, mean/p50/p95, no hard gate | Tasks 1 + 3 |
| Weak-label: HeuristicEvidenceJudge only; report_only; fixture on ci | Task 2 + 4 |
| `bench-summary.json` + `.md` | Task 1 + 4 |
| CI workflow same entry `--profile ci` | Task 6 |
| README subsection | Task 6 |
| No BEIR/RAGAS/vector default/outbound judge/answer EM | Global Constraints |
| No real vault bodies committed; reports gitignored | Task 1 fixture + Task 6 gitignore |
| Package stays 1.0.0 | Global Constraints |

**Ambiguities resolved from code/spec:**
1. R@10 metric name = `recall_at_k_macro` when eval `top_k=10` (existing CLI test); helper also accepts nested keys.
2. Latency vault missing on local → **skipped** (`vault_missing`); missing timing fixture file → **fail**.
3. CI short-circuits accuracy/latency even if `--vault` is passed.
4. `.github` and `tests/fixtures` are gitignored → always `git add -f`.
5. Weak-label gold hit uses primary `results` only (not `additional_results`).
6. No `src/eval/` package — modules live under `src/retrieval/bench_*.py`.
7. CLI registration mirrors `retrieval-eval` argparse + `main` dispatch in `src/app/cli.py`.

**Placeholder scan:** No TBD/TODO. Paths, thresholds (`0.75`, 5 queries), pytest commands, workflow YAML, and reason strings are concrete.

**Type consistency:** `TrackStatus` = `pass|fail|skipped|report_only`; `run_bench` → `tuple[int, dict]`; weak-label scorer name always `"HeuristicEvidenceJudge"`; timing fixture `schema_version=1`, `dataset_id=bench-timing-v1`.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-11-09-bench-suite.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (`superpowers:subagent-driven-development`).
2. **Inline Execution** — execute tasks in this session with `superpowers:executing-plans` and checkpoints.

Which approach?

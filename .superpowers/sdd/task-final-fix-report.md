# Final whole-branch Important fixes — bench suite

**Date:** 2026-11-09 (Etc/GMT-8 / UTC+8)  
**Branch:** `v1.0.0`  
**BASE:** `450ad2f`  
**Scope:** Fix all four Important findings from final whole-branch review. No version bump. No push.

## Findings fixed

1. **Summary stores absolute report paths** → `_relativize_reports` in `bench_runner.py`; track dicts store POSIX paths relative to `--output-dir`.
2. **`gold_page_in_topk` exact-string only** → `_normalize_page_path` (`\\` → `/`, strip leading `/`) for hits and relevant paths.
3. **CI never runs `tests/app`** → workflow step `uv run pytest tests/app/test_cli.py -k bench -q` before bench suite.
4. **Uncaught track errors skip summary** → each track wrapped in `except Exception`; fail status + truncated reason; always `write_bench_summary`; non-zero exit. Weak-label fail also gates exit.

## Files changed

- `src/retrieval/bench_runner.py`
- `src/retrieval/bench_weak_label.py`
- `.github/workflows/bench-ci.yml`
- `tests/retrieval/test_bench_runner.py`
- `tests/retrieval/test_bench_weak_label.py`
- `tests/retrieval/test_bench_ci_workflow.py`

## TDD

RED (new tests only, before impl):

```
uv run pytest \
  tests/retrieval/test_bench_weak_label.py::test_gold_page_in_topk_normalizes_slashes_and_leading_slash \
  tests/retrieval/test_bench_runner.py::test_summary_reports_are_relative_under_output_dir \
  tests/retrieval/test_bench_runner.py::test_track_exception_still_writes_summary_and_fails \
  tests/retrieval/test_bench_ci_workflow.py::test_bench_ci_workflow_runs_cli_bench_tests -v
```

→ 4 failed (expected): absolute paths, no normalization, uncaught RuntimeError, missing workflow CLI step.

GREEN after impl: same 4 passed.

## Covering suite

```
uv run pytest tests/retrieval/test_bench_runner.py \
  tests/retrieval/test_bench_weak_label.py \
  tests/retrieval/test_bench_summary.py \
  tests/retrieval/test_bench_latency.py \
  tests/retrieval/test_bench_ci_workflow.py \
  tests/app/test_cli.py -k bench -v
```

```
====================== 18 passed, 15 deselected in 0.19s =======================
```

Selected:

- test_extract_recall_at_10_prefers_macro_at_k
- test_ci_profile_skips_accuracy_and_latency
- test_regression_failure_sets_exit_code_2
- test_local_accuracy_gate_fail_loud
- test_summary_reports_are_relative_under_output_dir
- test_track_exception_still_writes_summary_and_fails
- test_score_topk_evidence_uses_heuristic_judge_only
- test_gold_page_in_topk_and_citation_coverage
- test_gold_page_in_topk_normalizes_slashes_and_leading_slash
- test_timing_queries_fixture_has_five_frozen_queries
- test_build_and_write_bench_summary
- test_load_timing_queries_and_percentiles
- test_run_latency_track_writes_report
- test_bench_ci_workflow_calls_profile_ci
- test_readme_documents_bench_suite
- test_bench_ci_workflow_runs_cli_bench_tests
- test_bench_cli_profile_ci_invokes_runner
- test_bench_parser_requires_profile_and_output_dir

## Concerns / notes

- Writers (`bench_weak_label`, `bench_latency`, `write_retrieval_eval_report`) still return absolute Path/str for local use; only summary track assembly relativizes.
- Paths outside `--output-dir` that cannot relativize fall back to basename (no absolute leak into summary).
- `KeyboardInterrupt` / `SystemExit` not caught (only `Exception`).
- Weak-label track `status: fail` now also forces exit ≠ 0 (runner crash path); metric-only weak-label remains `report_only` and non-gating.

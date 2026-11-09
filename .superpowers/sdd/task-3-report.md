# Task 3 Report — Knife 2 wiki_relaxed single-shot

**Status:** DONE  
**Commit:** `5f7f579e865953978036e885dd3e7941be73a453`  
**Base:** `42e7102` (Task 2 HEAD)

## What changed

- Added `_run_wiki_relaxed_single_shot` on `QueryExecutionContext`: appends reason on entry, calls `relaxed_recovery_items` with `limit=WIKI_RELAXED_SINGLE_SHOT_LIMIT` (40), scores via `compose_score`+`step_bonus`, `adaptive_expand` then hard-bound, abstains via `relaxed_fill_is_strong` without clearing prior selected, replaces + sets `lexical_mode="relaxed"` only on strong fill.
- Wired into `_run_sag2_default_recovery` after knife 1: recompute `pool_kind` → `plan_wiki_relaxed_single_shot` → knife 2 → existing `if not sag2_raw_single_shot: _run_raw_single_shot`.
- Imported `plan_wiki_relaxed_single_shot`.
- Tests: `test_cjk_empty_pool_fires_one_wiki_relaxed`, `test_cjk_relaxed_still_empty_falls_back_to_raw_replace`, `test_default_path_never_runs_full_p2_ladder_chain`.

## TDD evidence

### RED (before implement)

```
FAILED test_cjk_empty_pool_fires_one_wiki_relaxed
  AssertionError: assert ('wiki_relaxed_empty' in [] or 'wiki_relaxed_cjk' in [])
FAILED test_cjk_relaxed_still_empty_falls_back_to_raw_replace
  AssertionError: assert 'wiki_relaxed_empty' in []
PASSED test_default_path_never_runs_full_p2_ladder_chain
2 failed, 1 passed in 0.22s
```

Failure reason: knife 2 not wired / recovery_reasons empty — expected.

### GREEN (after implement)

Focused: `3 passed in 0.23s`  
File: `17 passed in 0.55s`  
Suite: `uv run pytest tests/retrieval -q --tb=line` → `465 passed, 1 skipped in 29.15s`

No `relaxed_fts_hits == 0` updates needed (Latin-only fixtures still 0).

## Self-review

- Order matches brief: coverage → recompute pool → wiki_relaxed → gated raw.
- Reason appended on branch entry even if fill abstains.
- Strong fill replaces selected (wiki-side); weak abstain leaves prior (empty → raw replace).
- Coverage still consumes `sag2_raw_single_shot`; orchestrator guard prevents second raw.
- Did not restore P2 ladder; did not touch CHANGELOG/eval.
- Did not add no-op-at-top on `_run_raw_single_shot` (brief optional; relied on orchestrator guard).

## Concerns

None blocking. `_run_raw_single_shot` still resets flags on entry — safe only via `if not self.sag2_raw_single_shot` (Task 2 review carry). Weak+CJK that strongly fills via relaxed will skip raw (pool no longer empty/weak) — intended.

---

## Review fix (evidence_score stamp)

**Status:** FIXED  
**Fix commit:** `a5d0c95b1697597e0430dbfca6336826ebfa1fdf`  
**Base:** `5f7f579`

### Critical
Strong wiki_relaxed fill left `evidence_score` unset; `_run_raw_single_shot` defaulted to 0.0 → `wiki_hop_is_weak` True → raw merge could overwrite `lexical_mode` to `strict`.

**Fix:** On strong fill admission in `_run_wiki_relaxed_single_shot`, stamp each selected row with `HeuristicEvidenceJudge().score(...)` floored at `0.15` (wiki_hop strong threshold). Matches hop/judge scoring patterns; fill already passed `relaxed_fill_is_strong`.

### Important
1. Hardened `test_cjk_empty_pool_fires_one_wiki_relaxed`: assert `raw_fallback_mode == "none"`, `raw_single_shot is False`, `lexical_mode in {relaxed, wiki_relaxed}`, `relaxed_fts_hits >= 1`, wiki result required.
2. Added `test_cjk_empty_pool_strong_relaxed_skips_raw` (raw competitor present).
3. Added `test_wiki_relaxed_cjk_weak_pool_skips_raw` (reason `wiki_relaxed_cjk` + strong fill skips raw).

### TDD
**RED:** hardened + new tests failed with `raw_fallback_mode == 'raw_merge'`.  
**GREEN:** focused 4 passed; file `19 passed`; `test_query_recovery.py` `28 passed`.

No CHANGELOG / eval.

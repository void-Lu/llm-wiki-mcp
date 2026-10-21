# Retrieval P1 Implementation Plan (Raw Ladder Only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen `raw_zero` so **weak primary** wiki hits (no qualified / identifier / phrase / title strong signals) no longer permanently block capped raw recovery — without aliases or vector work.

**Architecture:** Introduce `wiki_strong_answered` as the raw-zero gate (strong relaxed **or** strong primary **or** coverage already merged raw). Keep `has_primary_recall` for coverage / wiki_relaxed ordering. Reuse `relaxed_fill_is_strong` for primary strength; pass `seed_qualified_fts_hits` so any qualified stage-one hit counts strong. Weak primary + non-empty raw → existing `raw_only` replace; raw empty → fail-open keep weak primary (`run_raw_branch` already does this).

**Tech Stack:** Python 3, pytest, SQLite FTS5, `uv run`, existing `run_query_v2` / `retrieval-eval` CLI.

## Global Constraints

- **No aliases** / CamelCase→`N/…` / `Nui server widget` hardcodes (user cancelled)
- No vector / hybrid / embedding
- No absolute BM25 score threshold
- Do not change MCP public field names
- Do **not** change coverage / all_coverage merge formulas
- TDD: failing test → fail → implement → pass → commit (per task)
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push** unless asked
- No gold re-label; no reindex required (query-time only)
- Out of scope: unanswered strong-noise gate (0003/0011), sorting fixes for 0008/0015 residuals

## File map

| File | Role |
|------|------|
| `src/retrieval/query_recovery.py` | `FallbackState.wiki_strong_answered`; `plan_fallback` raw_zero gate + `wiki_weak_primary` reason |
| `src/retrieval/query_recall_policy.py` | `primary_fill_is_strong(...)` wrapping qualified count + `relaxed_fill_is_strong` |
| `src/retrieval/query_execution_context.py` | Compute strong/weak after primary+relaxed; gate raw_zero; preserve coverage_fallback |
| `src/retrieval/query_pipeline.py` | Pass `seed_qualified_fts_hits` into `QueryExecutionContext` |
| `tests/retrieval/test_query_recovery.py` | Decision table: weak primary → raw_zero; strong → block; matrix update |
| `tests/retrieval/test_query_recall_policy.py` | Integration: weak primary probes raw; strong relaxed still blocks; P0 weak abstain still green |
| `CHANGELOG.md` | User-visible note |
| `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md` | Spec (already committed) |
| `docs/plans/2026-10-19-retrieval-p1-raw-ladder.md` | This plan |

### Current anchors (do not invent)

`plan_fallback` raw_zero today (`query_recovery.py`):

```python
if (
    not state.has_primary_recall
    and not state.wiki_relaxed_answered
    and state.effective_scope in {"knowledge", "all"}
    and state.raw_available in {"unknown", "yes"}
):
    return FallbackPlan(
        "raw_zero",
        RecoveryCondition("raw", ("wiki_zero_results",), "item"),
        "raw",
        "raw_only",
    )
```

`FallbackState` today — **no** `wiki_strong_answered`:

```python
@dataclass(frozen=True)
class FallbackState:
    has_primary_recall: bool
    effective_scope: str
    uncovered_latin_terms: tuple[str, ...]
    wiki_relaxed_answered: bool
    raw_available: Literal["unknown", "yes", "no"]
    relaxed_available: bool
```

Raw-zero assembly today (`query_execution_context.py` ~592): always passes `has_primary_recall=self.seed_has_primary_recall`, so **any** primary hit blocks `raw_zero`. `run_raw_branch` already fail-opens when raw candidates empty (keeps `selected`).

Strong-signal helper already exists: `relaxed_fill_is_strong` in `query_recall_policy.py` (P0).

P0 regression anchors (must stay green):

- `test_v2_wiki_relaxed_answer_blocks_raw_fallback`
- `test_v2_weak_wiki_relaxed_abstains_and_allows_raw_zero`
- `test_v2_raw_fallback_respects_project_and_type_filters`

Target gold ids (post-P0 baseline `reports-p0`): **0018** (primary noise blocks raw — main win); **0008/0015** may already have raw via coverage/qualified — residuals OK to document; **0030** out of scope (aliases).

---

### Task 0: Docs (this plan)

- [x] Spec on branch: `docs/specs/2026-10-19-retrieval-p1-raw-ladder-design.md`
- [ ] Write this plan; `git add -f`; commit docs only (no implement)

---

### Task 1: `wiki_strong_answered` + `plan_fallback` decision table

**Files:**
- Modify: `src/retrieval/query_recovery.py`
- Test: `tests/retrieval/test_query_recovery.py`

**Interfaces:**

| Field / behavior | Spec |
|------------------|------|
| `FallbackState.wiki_strong_answered: bool` | New; default call sites must set explicitly |
| `raw_zero` when | `not wiki_strong_answered` **and** `effective_scope in {knowledge, all}` **and** `raw_available in {unknown, yes}` |
| `raw_zero` reason | `wiki_weak_primary` if `has_primary_recall` else `wiki_zero_results` |
| coverage / wiki_relaxed / all_coverage | **Unchanged** predicates (still use `has_primary_recall` / `wiki_relaxed_answered`) |
| history / archive / raw scopes | Still never get this `raw_zero` |

**Produces for later tasks:** `FallbackState(..., wiki_strong_answered=...)`; `plan.condition.reasons` may include `wiki_weak_primary`.

- [ ] **Step 1: Write failing tests** in `test_query_recovery.py`

Extend `_state` helper with `wiki_strong_answered: bool = False`. Add cases (names may vary):

```python
def test_plan_fallback_weak_primary_schedules_raw_zero() -> None:
    plan = plan_fallback(
        _state(
            has_primary_recall=True,
            effective_scope="knowledge",
            uncovered_latin_terms=(),
            wiki_relaxed_answered=False,
            wiki_strong_answered=False,
            raw_available="yes",
            relaxed_available=False,
        )
    )
    assert plan is not None and plan.branch == "raw_zero"
    assert plan.candidates_kind == "raw_only"
    assert "wiki_weak_primary" in plan.condition.reasons


def test_plan_fallback_strong_wiki_blocks_raw_zero() -> None:
    for kwargs in (
        dict(has_primary_recall=True, wiki_strong_answered=True),
        dict(has_primary_recall=False, wiki_relaxed_answered=True, wiki_strong_answered=True),
    ):
        plan = plan_fallback(
            _state(
                effective_scope="knowledge",
                uncovered_latin_terms=(),
                raw_available="yes",
                relaxed_available=False,
                wiki_relaxed_answered=False,
                **kwargs,
            )
        )
        assert plan is None or plan.branch != "raw_zero"
```

Update `test_plan_fallback_exhausts_the_decision_matrix` (and any progression tests) to include the new field: raw_zero expects `not wiki_strong_answered` (replace the old `not has_primary_recall and not wiki_relaxed_answered` clause for that branch only). Keep coverage / wiki_relaxed / all_coverage expectations identical.

Also assert zero-results path still uses `wiki_zero_results`:

```python
plan = plan_fallback(_state(has_primary_recall=False, wiki_strong_answered=False, ...))
assert "wiki_zero_results" in plan.condition.reasons
```

- [ ] **Step 2: Run — expect fail** (missing field / old gate)

```powershell
uv run pytest tests/retrieval/test_query_recovery.py -k "plan_fallback" -q
```

- [ ] **Step 3: Implement minimal `FallbackState` + `plan_fallback`**

```python
@dataclass(frozen=True)
class FallbackState:
    has_primary_recall: bool
    effective_scope: str
    uncovered_latin_terms: tuple[str, ...]
    wiki_relaxed_answered: bool
    raw_available: Literal["unknown", "yes", "no"]
    relaxed_available: bool
    wiki_strong_answered: bool = False  # or required positional — prefer required to force call-site updates


# raw_zero block — replace has_primary_recall / wiki_relaxed_answered gate:
if (
    not state.wiki_strong_answered
    and state.effective_scope in {"knowledge", "all"}
    and state.raw_available in {"unknown", "yes"}
):
    reason = "wiki_weak_primary" if state.has_primary_recall else "wiki_zero_results"
    return FallbackPlan(
        "raw_zero",
        RecoveryCondition("raw", (reason,), "item"),
        "raw",
        "raw_only",
    )
```

Leave coverage / wiki_relaxed / all_coverage blocks untouched. Fix **all** `FallbackState(...)` construction sites in src + tests so they compile (execution_context temporarily pass `wiki_strong_answered=wiki_relaxed_answered or has_primary_recall` to preserve old behavior until Task 2 — **or** fail those imports and fix in Task 2 only if Task 1 tests are pure-recovery). Prefer: Task 1 updates recovery tests + recovery.py; temporarily set execution_context `wiki_strong_answered=self.seed_has_primary_recall or wiki_relaxed_answered` so pipeline still green (old semantics) until Task 2 flips the computation.

- [ ] **Step 4: Run — expect pass**

```powershell
uv run pytest tests/retrieval/test_query_recovery.py -q
```

- [ ] **Step 5: Commit**

```text
feat(retrieval): gate raw_zero on wiki_strong_answered
```

---

### Task 2: `primary_fill_is_strong` + wire weak primary → capped `raw_zero`

**Files:**
- Modify: `src/retrieval/query_recall_policy.py`
- Modify: `src/retrieval/query_execution_context.py`
- Modify: `src/retrieval/query_pipeline.py` (pass qualified hit count)
- Test: `tests/retrieval/test_query_recall_policy.py` (and unit on helper if useful)

**Interfaces:**

```python
def primary_fill_is_strong(
    question: str,
    items: Sequence[Mapping[str, Any]],
    *,
    qualified_fts_hits: int = 0,
    extra_terms: Sequence[str] = (),
) -> bool:
    """True when stage-one primary should block raw_zero."""
```

Frozen strength rules (no BM25 cutoff):

1. `qualified_fts_hits > 0` and `items` non-empty → **strong**
2. Else `relaxed_fill_is_strong(question, items, extra_terms=extra_terms)`

`wiki_strong_answered` at raw_zero planning time:

```text
wiki_strong_answered =
    wiki_relaxed_answered
    OR (seed_has_primary_recall AND primary_fill_is_strong(...))
    OR coverage_fallback   # coverage already merged raw — do not raw_only-replace
```

Weak-primary selection (already mostly in `run_raw_branch`):

1. Schedule `raw_zero` when `not wiki_strong_answered`
2. If raw non-empty → `raw_only` replace + reason from plan (`wiki_weak_primary`)
3. If raw empty → keep weak primary (existing early return)

- [ ] **Step 1: Write failing integration test** (weak primary probes raw)

Mirror P0 fixture style (`create_wiki_root`, `_write`, `refresh_indexes`). Need **strict primary hits** that are weak (CJK/OR noise in title/body with **no** shared title token / identifier / phrase), plus a raw doc that clearly matches:

```python
def test_v2_weak_primary_allows_capped_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Active pages that AND/OR-match query tokens but lack title/identifier/phrase overlap
    _write(
        root,
        "wiki/concepts/noise-primary.md",
        "杂谈笔记",
        "部署 清单 权限 勾选 说明 guide 模块",
        type="concept",
    )
    raw = root / "raw/sources/references/mcp-install-checklist.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# MCP Standard Tools 安装清单\n\n"
        "Server SuiteScript 与 REST Web Services 勾选后安装 MCP Standard Tools。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "MCP Standard Tools 安装清单 Server SuiteScript",
        scope="knowledge",
        retrieval_mode="lexical",
    )
    paths = [item["path"] for item in result["results"]]
    # Must not be stuck on noise-only wiki when raw has the checklist
    assert paths, "expected results"
    assert paths[0].startswith("raw/"), paths
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert "wiki_weak_primary" in result["pipeline"]["fallback"]["reasons"]
```

Tune fixture until **before** the code change: either returns wiki-only (blocked) or fails the raw assert — that is the fail to fix. If the query never gets `fts_hits>0` on wiki, tighten wiki body tokens so strict hits exist but `primary_fill_is_strong` is false.

Add companion: **strong primary still blocks** — wiki page whose title overlaps a Latin identifier in the question (or inject `N/record` qualified path) must keep wiki and `raw_fts_hits == 0` or fallback level ≠ raw from `raw_zero`. Prefer reusing / lightly extending an existing qualified or title-strong knowledge fixture rather than inventing a huge vault.

Keep running:

```python
# existing — must stay green
test_v2_wiki_relaxed_answer_blocks_raw_fallback
test_v2_weak_wiki_relaxed_abstains_and_allows_raw_zero
```

- [ ] **Step 2: Run — expect fail** on new weak-primary test

```powershell
uv run pytest tests/retrieval/test_query_recall_policy.py -k "weak_primary or wiki_relaxed_answer_blocks or weak_wiki_relaxed" -q
```

- [ ] **Step 3: Implement helper + wire**

In `query_recall_policy.py`:

```python
def primary_fill_is_strong(
    question: str,
    items: Sequence[Mapping[str, Any]],
    *,
    qualified_fts_hits: int = 0,
    extra_terms: Sequence[str] = (),
) -> bool:
    if not items:
        return False
    if qualified_fts_hits > 0:
        return True
    return relaxed_fill_is_strong(question, items, extra_terms=extra_terms)
```

In `QueryExecutionContext`: add `seed_qualified_fts_hits: int = 0`.

In `query_pipeline.py` construction of `QueryExecutionContext`, pass `seed_qualified_fts_hits=qualified_fts_hits`.

In `_run_fallback_recovery`, when building `raw_zero_state`:

```python
primary_strong = (
    self.seed_has_primary_recall
    and recall_policy.primary_fill_is_strong(
        request_view.question,
        self.seed_selected,  # strength of *primary* seed, not post-coverage mix
        qualified_fts_hits=self.seed_qualified_fts_hits,
    )
)
wiki_strong_answered = (
    wiki_relaxed_answered
    or primary_strong
    or self.coverage_fallback
)
raw_zero_state = FallbackState(
    has_primary_recall=self.seed_has_primary_recall,
    effective_scope=request_view.effective_scope,
    uncovered_latin_terms=tuple(self.uncovered_latin_terms),
    wiki_relaxed_answered=wiki_relaxed_answered,
    raw_available="unknown",
    relaxed_available=wiki_relaxed_answered,
    wiki_strong_answered=wiki_strong_answered,
)
```

Update **every** other `FallbackState(...)` in this file (coverage / relaxed / all_coverage) to pass `wiki_strong_answered=` consistently:

- coverage planning: `wiki_strong_answered=False` (irrelevant; coverage uses `has_primary_recall`)
- relaxed planning: `False`
- all_coverage: `wiki_strong_answered=True` when strong relaxed committed

Do not widen raw ladder limits; keep `RAW_FALLBACK_LIMIT` / `RAW_FALLBACK_CANDIDATE_LIMIT`.

- [ ] **Step 4: Run — expect pass**

```powershell
uv run pytest tests/retrieval/test_query_recall_policy.py tests/retrieval/test_query_recovery.py tests/retrieval/test_query_execution_context.py -q
```

- [ ] **Step 5: Commit**

```text
feat(retrieval): probe raw_zero on weak primary wiki hits
```

---

### Task 3: CHANGELOG + gold eval (`reports-p1`)

**Files:**
- Modify: `CHANGELOG.md`
- Output: `.eval/gold-sample-2026-10-19/reports-p1/` (local artifacts; do not force-add secrets)

**Steps:**

- [ ] **Step 1: CHANGELOG** (Chinese ok), under `## 2026-10-19 — …`:

  - Weak primary (no qualified / title / identifier / phrase strong signal) may probe capped `raw_zero`; reason `wiki_weak_primary`
  - Strong relaxed / strong primary / successful coverage merge still block raw-only replace
  - No aliases; no reindex required

- [ ] **Step 2: Targeted regression**

```powershell
uv run pytest tests/retrieval/test_query_recovery.py tests/retrieval/test_query_recall_policy.py tests/retrieval/test_query_execution_context.py tests/retrieval/test_query_pipeline.py -q
```

- [ ] **Step 3: Gold eval** (same frozen set; **no** reindex)

```powershell
uv run llm-wiki-mcp retrieval-eval `
  --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" `
  --dataset "C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl" `
  --manifest "C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.manifest.json" `
  --output-dir "C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\reports-p1"
```

- [ ] **Step 4: Compare to `reports-p0`**

  - Focus hit@10 / miss reasons for **0018** (expect improve), **0008 / 0015** (may flat — document residual), **0030** (expect still miss)
  - `filter_correctness` ≈ 1.0
  - Note p95 if reported; no hard SLA, flag large regressions

- [ ] **Step 5: Commit changelog**

```text
docs: changelog for retrieval P1 weak-primary raw ladder
```

Report metrics + commit hashes to parent; **do not push** unless asked.

---

## Self-review vs spec

| Spec requirement | Plan coverage |
|------------------|---------------|
| `wiki_strong_answered` gates raw_zero | Task 1 |
| Strong relaxed still blocks | Task 1 + Task 2 regression |
| Weak primary → capped raw_zero; raw empty fail-open | Task 2 + existing `run_raw_branch` |
| New reason `wiki_weak_primary` | Task 1 |
| coverage formula unchanged; don't wipe coverage with raw_only | `coverage_fallback` ⇒ strong |
| No aliases / vector / unanswered gate | Global Constraints |
| Success: gold eval vs reports-p0; honest 0008/0015/0030 | Task 3 |
| TDD + commits per bite | Tasks 1–2 steps |
| Files listed in spec | File map matches |

No TBD: strength definition, fail-open, coverage interaction, and reason string are frozen.

## Execution handoff

Start at **Task 1**. After Task 2 green, run Task 3 eval before calling the work done.

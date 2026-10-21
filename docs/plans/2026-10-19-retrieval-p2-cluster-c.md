# Retrieval P2 Cluster-C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix cluster-C residual misses (0018 / 0008 / 0015) after P1: tighten body-only primary “strong”, and make coverage-merge surface Help Docs raw into top-k when Latin terms are uncovered.

**Architecture:** Three approved knives map onto two code levers. Knife 1 adds `body_evidence=False` on the primary strong path so phrase/identifier matches title/path only. Knives 2+3 share `merge_coverage_items`: today `coverage` already schedules whenever `has_primary_recall` and uncovered Latin exist **before** `wiki_strong_answered` is considered (`triggered` in eval means “raw survived into selected”, not “plan ran”). So the latin-probe intent is satisfied by boosting raw that covers uncovered terms inside coverage fusion — not by a second `plan_fallback` branch that would double-run the raw ladder.

**Tech Stack:** Python 3, pytest, SQLite FTS5, `uv run`, existing `run_query_v2` / `retrieval-eval` CLI.

## Global Constraints

- No aliases / CamelCase→`N/…` hardcodes (cluster D abandoned)
- No vector / hybrid / embedding
- No absolute BM25 score threshold
- Do not change MCP public field names
- Do **not** re-open unanswered strong-noise gate (0003 / 0011)
- TDD: failing test → fail → implement → pass → commit (per task)
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push** unless asked
- No gold re-label; no reindex required (query-time only)
- Spec: `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`

## File map

| File | Role |
|------|------|
| `src/retrieval/query_recall_policy.py` | Knife 1: `relaxed_fill_is_strong(..., body_evidence=)`; `primary_fill_is_strong` passes `False`. Knives 2+3: `merge_coverage_items` raw coverage authority bump |
| `src/retrieval/query_recovery.py` | Knife 3 regression only: coverage still wins over strong (no new branch unless a test proves a gap) |
| `src/retrieval/query_execution_context.py` | Pass-through only if primary strong call site needs updating |
| `tests/retrieval/test_query_recall_policy.py` | Unit + vault fixtures for body-only primary, coverage merge rank |
| `tests/retrieval/test_query_recovery.py` | Decision table: coverage ∥ strong; no duplicate raw ladder |
| `CHANGELOG.md` | User-visible note |
| `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md` | Spec (committed `db274a9`) |
| `docs/plans/2026-10-19-retrieval-p2-cluster-c.md` | This plan |

### Current anchors (do not invent)

`relaxed_fill_is_strong` phrase/identifier use full `blob = title\\npath\\nbody` (`query_recall_policy.py` ~206–212). Expansion aliases already title/path-only.

`primary_fill_is_strong` today:

```python
if not items:
    return False
if qualified_fts_hits > 0:
    return True
return relaxed_fill_is_strong(question, items, extra_terms=extra_terms)
```

`merge_coverage_items` fusion today:

```python
authority = 0.0 if is_raw else min(max(float(item.get("score", 0.0)) / 100.0, 0.0), 0.35)
fused_score = round(coverage_ratio + source_rrf + authority, 12)
# raw with no uncovered-term overlap is dropped
```

Coverage plan is built with `wiki_strong_answered=False` always (`query_execution_context.py` ~476–494), so strong primary **does not** block coverage scheduling.

Eval `pipeline.coverage.triggered` == `coverage_fallback` == “any selected hit is raw” (`query_pipeline.py` ~777).

P1-fix snapshots:

| id | query gist | residual |
|----|------------|----------|
| 0018 | quoted “Custom Record Type Object Custom Fields” | fts=3, qualified=0, raw=0, fallback none — body phrase likely marks primary strong |
| 0008 | email capture plug-in | coverage raw in pool (triggered true) but gold not top-10 |
| 0015 | unauthenticated … N/search | uncovered_latin=3, raw_fts=80, triggered false — merge dropped/buried raw |

P0/P1 regression anchors (must stay green):

- `test_v2_wiki_relaxed_answer_blocks_raw_fallback`
- `test_v2_weak_wiki_relaxed_abstains_and_allows_raw_zero`
- `test_v2_weak_primary_allows_capped_raw_zero`
- `test_v2_raw_fallback_respects_project_and_type_filters`
- `test_primary_fill_is_strong_uses_qualified_hits_and_relaxed_signals`

---

### Task 0: Docs (this plan)

- [x] Spec on branch: `docs/specs/2026-10-19-retrieval-p2-cluster-c-design.md`
- [ ] Write this plan; `git add -f`; commit docs only (no implement)

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
git add -f docs/plans/2026-10-19-retrieval-p2-cluster-c.md
git commit -m "docs(plans): retrieval P2 cluster-C implementation plan"
```

---

### Task 1: Knife 1 — primary strong ignores body-only phrase/identifier

**Files:**
- Modify: `src/retrieval/query_recall_policy.py` (`relaxed_fill_is_strong`, `primary_fill_is_strong`)
- Test: `tests/retrieval/test_query_recall_policy.py`

**Interfaces:**

| Symbol | Spec |
|--------|------|
| `relaxed_fill_is_strong(..., body_evidence: bool = True)` | Default `True` preserves P0 relaxed abstain semantics |
| When `body_evidence=False` | phrase + qualified-id alias match **title/path only**; exact title↔question and title-token / edit-distance-1 unchanged |
| `primary_fill_is_strong(...)` | After qualified>0 short-circuit, call `relaxed_fill_is_strong(..., body_evidence=False)` |

**Produces for later tasks:** Body-only phrase primary is weak → existing `wiki_weak_primary` / `raw_zero` path can fire for 0018-shaped queries.

- [ ] **Step 1: Write failing tests**

Extend `test_query_recall_policy.py`:

```python
def test_primary_fill_is_strong_ignores_body_only_phrase() -> None:
    body_phrase_hit = PassageHit(
        passage_id="p-body",
        page_path="wiki/concepts/noise.md",
        title="杂谈笔记",
        heading_path=(),
        text="see Custom Record Type Object Custom Fields in passing",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    title_phrase_hit = PassageHit(
        passage_id="p-title",
        page_path="wiki/concepts/custom-fields.md",
        title="Custom Record Type Object Custom Fields",
        heading_path=(),
        text="unrelated",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    q = "Custom Record Type Object Custom Fields"
    assert not primary_fill_is_strong(q, [{"hit": body_phrase_hit}])
    assert primary_fill_is_strong(q, [{"hit": title_phrase_hit}])
    # relaxed path still treats body phrase as strong (P0 default)
    assert relaxed_fill_is_strong(q, [{"hit": body_phrase_hit}])


def test_v2_body_only_primary_phrase_allows_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/noise-primary.md",
        "杂谈笔记",
        "see Custom Record Type Object Custom Fields mentioned once",
        type="concept",
    )
    raw = (
        root
        / "raw/sources/file/NetSuite Help Docs"
        / "Custom Record Type Object Custom Fields.md"
    )
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# Custom Record Type Object Custom Fields\n\n"
        "SDF customrecordcustomfields XML example and role limits.\n",
        encoding="utf-8",
    )
    refresh_indexes(root)
    result = run_query_v2(
        root,
        '"Custom Record Type Object Custom Fields"',
        scope="knowledge",
        retrieval_mode="lexical",
    )
    paths = [item["path"] for item in result["results"]]
    assert any(path.startswith("raw/") for path in paths), paths
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert "wiki_weak_primary" in result["pipeline"]["fallback"]["reasons"]
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_ignores_body_only_phrase tests/retrieval/test_query_recall_policy.py::test_v2_body_only_primary_phrase_allows_raw_zero -v
```

Expected: FAIL (body-only still strong / no raw_zero).

- [ ] **Step 3: Minimal implementation**

In `relaxed_fill_is_strong`, add `body_evidence: bool = True`. Build:

```python
title_path = f"{title}\n{page_path}"
phrase_blob = blob if body_evidence else title_path
ident_blob = blob if body_evidence else title_path
```

Use `phrase_blob` / `ident_blob` for the phrase and qualified-id loops; keep exact + title-token logic unchanged.

In `primary_fill_is_strong`:

```python
return relaxed_fill_is_strong(
    question, items, extra_terms=extra_terms, body_evidence=False
)
```

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command +:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "relaxed_fill_is_strong or primary_fill_is_strong or weak_primary or weak_wiki_relaxed" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/retrieval/query_recall_policy.py tests/retrieval/test_query_recall_policy.py
git commit -m "fix(retrieval): primary strong ignores body-only phrase/identifier"
```

---

### Task 2: Knives 2+3 — coverage merge lifts raw that covers uncovered Latin

**Files:**
- Modify: `src/retrieval/query_recall_policy.py` (`merge_coverage_items`)
- Test: `tests/retrieval/test_query_recall_policy.py`
- Test: `tests/retrieval/test_query_recovery.py` (coverage independent of strong)

**Interfaces:**

| Behavior | Spec |
|----------|------|
| Drop rule | Unchanged: raw with zero uncovered-term overlap still skipped |
| Wiki authority | Unchanged cap `min(score/100, 0.35)` |
| Raw authority | **New:** `0.35 * coverage_ratio` (same max band as wiki cap) so a raw page covering all uncovered terms can outrank a wiki page with `coverage_ratio=0` + full authority |
| Formula | `fused_score = coverage_ratio + source_rrf + authority` (structure unchanged) |
| Knife 3 mapping | No new `plan_fallback` branch; add regression that `has_primary_recall + uncovered_latin` still plans `coverage` even when `wiki_strong_answered=True` would be set at raw_zero time. If a future gap appears (coverage not scheduled), only then add `wiki_strong_latin_probe` — YAGNI until proven |

**Produces:** 0008/0015-shaped coverage merges keep Help Docs raw competitive in top-k.

- [ ] **Step 1: Write failing unit test for merge rank**

```python
def test_merge_coverage_items_prefers_high_coverage_raw_over_padding_wiki() -> None:
    wiki = candidate_item(
        PassageHit(
            passage_id="w1",
            page_path="wiki/concepts/n-search.md",
            title="N/search",
            heading_path=(),
            text="module search APIs only",
            score=80.0,
            corpus="active",
            authority="formal",
            source_kind="wiki",
            ordinal=0,
        ),
        score=80.0,
    )
    raw = candidate_item(
        PassageHit(
            passage_id="r1",
            page_path="raw/sources/file/Help/Nsearch Module.md",
            title="N/search Module",
            heading_path=(),
            text="unauthenticated client-side contexts are not supported",
            score=5.0,
            corpus="raw",
            authority="",
            source_kind="raw",
            ordinal=0,
        ),
        score=5.0,
    )
    merged = merge_coverage_items(
        [wiki],
        [raw],
        uncovered_terms=["unauthenticated", "client", "contexts"],
        rrf_k=10,
    )
    assert merged, "expected fused rows"
    assert merged[0]["hit"].source_kind == "raw"
    assert merged[0]["coverage_ratio"] == 1.0
```

Import `candidate_item` / `merge_coverage_items` as the file already does for neighboring tests (add imports if missing).

- [ ] **Step 2: Write failing recovery regression (knife 3 intent)**

In `test_query_recovery.py`:

```python
def test_plan_fallback_coverage_not_blocked_by_wiki_strong_flag() -> None:
    """Coverage is planned from primary+uncovered; strong flag must not steal the branch."""
    plan = plan_fallback(
        _state(
            has_primary_recall=True,
            effective_scope="knowledge",
            uncovered_latin_terms=("unauthenticated",),
            wiki_relaxed_answered=False,
            wiki_strong_answered=True,  # as if primary were strong
            raw_available="yes",
            relaxed_available=False,
        )
    )
    assert plan is not None
    assert plan.branch == "coverage"
```

(Today this should already pass — run it; if it fails, fix `plan_fallback` order before merge work. Treat as characterization test locked in this task.)

- [ ] **Step 3: Run merge test to verify fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py::test_merge_coverage_items_prefers_high_coverage_raw_over_padding_wiki -v
```

Expected: FAIL (`merged[0]` still wiki because authority 0.35 beats raw).

- [ ] **Step 4: Minimal implementation in `merge_coverage_items`**

Replace authority assignment:

```python
if is_raw:
    # Offset wiki formal authority so full-coverage raw can surface in top-k.
    authority = 0.35 * coverage_ratio
else:
    authority = min(max(float(item.get("score", 0.0)) / 100.0, 0.0), 0.35)
fused_score = round(coverage_ratio + source_rrf + authority, 12)
```

Do **not** change `merge_weak_primary_raw` in this task.

- [ ] **Step 5: Integration fixture (optional but preferred)**

Add `test_v2_coverage_merge_surfaces_raw_help_doc(tmp_path)` mirroring 0015: wiki page covers `search`/`module` only; question includes `unauthenticated client-side contexts N/search module`; raw Help page contains the three uncovered tokens; assert a `raw/` path appears in `result["results"]` and `pipeline.coverage.triggered` is True.

- [ ] **Step 6: Run tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "merge_coverage or coverage_merge or body_only_primary or weak_primary" -v
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recovery.py::test_plan_fallback_coverage_not_blocked_by_wiki_strong_flag -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/retrieval/query_recall_policy.py tests/retrieval/test_query_recall_policy.py tests/retrieval/test_query_recovery.py
git commit -m "fix(retrieval): lift coverage-merge raw when it covers uncovered Latin"
```

---

### Task 3: CHANGELOG + gold eval `reports-p2-c`

**Files:**
- Modify: `CHANGELOG.md`
- Output: `.eval/gold-sample-2026-10-19/reports-p2-c/` (local artifact; do not force-add unless user asks)

- [ ] **Step 1: CHANGELOG entry** under Unreleased / Fixed (match repo style):

```markdown
- Retrieval: primary “strong” no longer treats body-only phrase/identifier hits as answers; coverage merge gives raw pages that cover uncovered Latin terms a proportional authority offset so Help Docs can enter top-k (cluster C / P2).
```

- [ ] **Step 2: Run targeted retrieval tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py tests/retrieval/test_query_recovery.py -q --tb=line
```

Expected: PASS.

- [ ] **Step 3: Gold eval**

```powershell
.\.venv\Scripts\python.exe -m llm_wiki_mcp retrieval-eval --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" --dataset .eval/gold-sample-2026-10-19/gold.jsonl --manifest .eval/gold-sample-2026-10-19/gold.manifest.json --output-dir .eval/gold-sample-2026-10-19/reports-p2-c
```

(Adjust module invocation to whatever the repo’s CLI entry already uses in prior P0/P1 notes — prefer the same `uv run llm-wiki-mcp retrieval-eval ...` form if that is the project norm.)

Compare to `reports-p1-fix`:

- 0008 / 0015 / 0018 hit@10 or recall
- `filter_correctness` ≈ 1.0
- If a case still misses, write one-line residual in the eval md or commit message (honest, no alias fix)

- [ ] **Step 4: Commit changelog (+ plan already committed)**

```powershell
git add CHANGELOG.md
git commit -m "docs: changelog for retrieval P2 cluster-C"
```

Do **not** push unless asked.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| 刀1 body-only not strong for primary | Task 1 |
| 刀2 coverage rank / raw visibility | Task 2 (`merge_coverage_items` authority) |
| 刀3 latin uncovered → probe/merge raw without replacing wiki | Task 2 (coverage path already merges; boost + characterization test). **No duplicate `raw_zero` branch** — matches code reality that coverage is not gated by strong |
| No unanswered gate / no aliases / no vectors | Global constraints |
| Eval `reports-p2-c` | Task 3 |
| Fail-open weak primary when raw empty | Unchanged P1 `run_raw_branch` |

Placeholder scan: none. Types: `body_evidence: bool`, authority float formula locked above.

---

## Execution handoff

Plan complete when saved to `docs/plans/2026-10-19-retrieval-p2-cluster-c.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — this session with executing-plans checkpoints  

Which approach?

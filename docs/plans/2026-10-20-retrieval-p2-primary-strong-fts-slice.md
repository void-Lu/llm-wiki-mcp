# Primary Strong Evidence = FTS Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the title-token denylist knife, codingwork-0018 still marks primary strong because `primary_fill_is_strong` walks the full post-graph `seed_selected`, which includes title-candidate `Custom Tool Script` (`ov={custom}`). Restrict primary-strong evidence to FTS-ranked items (`fts_rank is not None`) so title-only / graph-only pages cannot alone block `raw_zero`, aligning with `has_primary_recall = bool(ranked)`.

**Architecture:** One call-site filter in `QueryExecutionContext._run_fallback_recovery` before `primary_fill_is_strong`. Prefer a tiny pure helper (same module or `query_recall_policy`) so unit tests can lock the slice without mocking the whole execute path. **Do not** change denylist / `GENERIC_TITLE_TOKENS` / `_title_token_overlap_is_strong` / relaxed / knife1 body gate / coverage / qualified short-circuit.

**Tech Stack:** Python / existing retrieval tests / `.\.venv\Scripts\python.exe -m pytest` and `uv run llm-wiki-mcp retrieval-eval` (as in prior P2 plans).

## Global Constraints

- **Evidence rule (frozen):** `fts_evidence = [item for item in seed_selected if item.get("fts_rank") is not None]` — use **`is not None`**, not truthiness (do not treat `0` as missing if it ever appears)
- **Call site:** only the `primary_fill_is_strong(...)` argument in `_run_fallback_recovery`; pass `fts_evidence` instead of `self.seed_selected`
- **Denylist / GENERIC / formula:** **unchanged** this knife — do **not** add `custom` / `script` / `tool`
- **Do not change:** `relaxed_fill_is_strong` defaults; exact title↔question; phrase/ident knife1 body gate; `qualified_fts_hits > 0` short-circuit; coverage merge / raw_zero orchestration (beyond evidence slice); result display (title/graph pages may still appear in final hits)
- No aliases / vectors / BM25 absolute cutoff / 0003–0011 unanswered gate
- **0008 / 0015 are not success criteria** for this knife
- TDD: failing tests first → fail → implement → pass → commit (per task)
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push / open PR** unless asked
- Spec (source of truth): `docs/specs/2026-10-20-retrieval-p2-primary-strong-fts-slice-design.md` (commit `81df76a`)
- Brief (diagnosis): `/workspace/sdd-p2/brief-0018-custom-seed-design.md` (context only)
- CHANGELOG entry required when implementing (Task 3); **Chinese**, match prior P2 style

## File map

| File | Role |
|------|------|
| `src/retrieval/query_execution_context.py` | Filter `seed_selected` → FTS slice before `primary_fill_is_strong` in `_run_fallback_recovery` |
| `src/retrieval/query_recall_policy.py` | **Optional:** export tiny `fts_ranked_evidence(items)` helper only if it keeps tests DRY; **do not** change denylist / GENERIC / strong formula |
| `tests/retrieval/test_query_recall_policy.py` and/or `tests/retrieval/test_query_execution_context.py` | New FTS-slice tests; keep denylist / SuiteScript / knife1 / Location fixtures green |
| `CHANGELOG.md` | User-visible note (Chinese, match prior P2 style) |
| `docs/specs/2026-10-20-retrieval-p2-primary-strong-fts-slice-design.md` | Approved spec (already committed `81df76a`) |
| `docs/plans/2026-10-20-retrieval-p2-primary-strong-fts-slice.md` | This plan |

**Do not modify for this knife:** `GENERIC_TITLE_TOKENS`, `_title_token_overlap_is_strong`, knife1 body phrase gate, `lexical_analyzer` stopwords, MCP tool signatures, coverage/merge ranking for 0008/0015.

### Current anchors (do not invent — as of denylist on `v0.9.36` @ `25e5fc6` / `72d3f45`)

`_run_fallback_recovery` — `src/retrieval/query_execution_context.py` ~L615–620:

```python
primary_strong = (
    self.seed_has_primary_recall
    and recall_policy.primary_fill_is_strong(
        request_view.question,
        self.seed_selected,  # ← full post-graph seed (includes title_rank-only)
        qualified_fts_hits=self.seed_qualified_fts_hits,
    )
)
```

`candidate_item` — `src/retrieval/candidate_items.py`: core keys include `"fts_rank"` / `"title_rank"` defaulting to `None`. FTS path sets `fts_rank=rank`; title-candidate path sets `title_rank=rank` and leaves `fts_rank=None`.

Live 0018 residual (brief): FTS `N/record 模块` alone → primary **False** (denylist OK); title-candidate `Custom Tool Script` with `ov={custom}` alone → **True**; full seed → **True**; `fts_rank is not None` only → **False**.

Existing fixtures to **keep green**:

| Test | Expectation that must survive |
|------|-------------------------------|
| `test_primary_fill_is_strong_rejects_single_generic_title_token` | `{record}` primary **False**; relaxed still **True** |
| `test_primary_fill_is_strong_accepts_multi_generic_title_token_overlap` | `{record,type}` primary **True** |
| `test_primary_fill_is_strong_uses_qualified_hits_and_relaxed_signals` | SuiteScript primary **True**; `qualified_fts_hits=1` True |
| `test_primary_fill_is_strong_ignores_body_only_phrase` | body-only phrase primary **False** |
| `test_relaxed_fill_is_strong_detects_title_phrase_and_identifier` | Location relaxed single-token **True** |
| `test_v2_body_only_primary_phrase_allows_raw_zero` | knife1 integration still allows raw |

---

### Task 0: Docs (this plan)

- [x] Spec on branch: `docs/specs/2026-10-20-retrieval-p2-primary-strong-fts-slice-design.md` (`81df76a`)
- [ ] Write this plan; `git add -f`; commit **plan file only** (no `src/` / `tests/` yet)

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
git add -f docs/plans/2026-10-20-retrieval-p2-primary-strong-fts-slice.md
git commit -m "docs: plan primary strong FTS evidence slice (0018)"
```

Do **not** push.

---

### Task 1: Failing unit tests (TDD — no production code yet)

**Files:**
- Prefer: `tests/retrieval/test_query_recall_policy.py` (policy-level slice + `primary_fill_is_strong` on filtered lists)
- And/or: `tests/retrieval/test_query_execution_context.py` if you want a call-site assertion — only if a pure helper is exported; do **not** require a heavy end-to-end execute harness for this knife

**Suggested symbols / names:**

1. Helper under test (implement in Task 2): `fts_ranked_evidence(items: Sequence[Mapping[str, Any]]) -> list[...]` — keep items whose `fts_rank is not None`. Place in `query_recall_policy` (importable next to `primary_fill_is_strong`) **or** as a module-private `_fts_ranked_seed_items` in `query_execution_context` re-exported for tests — pick one; prefer `query_recall_policy.fts_ranked_evidence` for easy imports.
2. `test_fts_ranked_evidence_keeps_only_fts_rank` — pure filter
3. `test_primary_strong_evidence_ignores_title_only_custom_token` — lock 0018 residual
4. Re-assert SuiteScript / denylist still pass when evidence is FTS-tagged

- [ ] **Step 1: Write failing tests**

```python
from retrieval.candidate_items import candidate_item
from retrieval.query_recall_policy import (
    fts_ranked_evidence,  # Task 2 adds this (or import from execution_context if placed there)
    primary_fill_is_strong,
    relaxed_fill_is_strong,
)
from retrieval.retrieval_index import PassageHit


def _hit(path: str, title: str, text: str = "unrelated body noise only") -> PassageHit:
    return PassageHit(
        passage_id=f"p-{path}",
        page_path=path,
        title=title,
        heading_path=(),
        text=text,
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )


def test_fts_ranked_evidence_keeps_only_fts_rank() -> None:
    fts = candidate_item(
        _hit("wiki/concepts/netsuite-ss-modules/n-record.md", "N/record 模块"),
        score=1.0,
        fts_rank=2,
    )
    title_only = candidate_item(
        _hit(
            "wiki/concepts/script-types/Custom-Tool-Script.md",
            "SuiteScript 2.1 脚本类型：Custom Tool Script",
        ),
        score=0.5,
        title_rank=9,
    )
    graphish = candidate_item(
        _hit("wiki/other.md", "Other"),
        score=0.1,
        # fts_rank left None — not FTS evidence
    )
    out = fts_ranked_evidence([fts, title_only, graphish])
    assert out == [fts]
    assert fts_ranked_evidence([]) == []


def test_primary_strong_evidence_ignores_title_only_custom_token() -> None:
    """0018 residual: title-candidate {custom} must not make primary strong
    when FTS slice is only weak {record}.
    """
    fts_weak = candidate_item(
        _hit("wiki/concepts/netsuite-ss-modules/n-record.md", "N/record 模块"),
        score=1.0,
        fts_rank=2,
    )
    title_custom = candidate_item(
        _hit(
            "wiki/concepts/script-types/Custom-Tool-Script.md",
            "SuiteScript 2.1 脚本类型：Custom Tool Script",
        ),
        score=0.5,
        title_rank=9,
    )
    q = "Custom Record Type Object Custom Fields"
    full = [fts_weak, title_custom]
    # Full seed still strong today (title-only {custom}) — documents residual
    assert primary_fill_is_strong(q, full)
    # Spec behavior: strong evidence = FTS slice only → denylist weak {record}
    assert not primary_fill_is_strong(q, fts_ranked_evidence(full))
    # FTS non-generic still strong
    suitescript = candidate_item(
        _hit("wiki/guides/suitescript.md", "SuiteScript Guide"),
        score=1.0,
        fts_rank=1,
    )
    assert primary_fill_is_strong(
        "How does SuiteScript work?", fts_ranked_evidence([suitescript, title_custom])
    )
```

**Notes for implementers:**

- If `fts_ranked_evidence` does not exist yet, Step 2 FAIL may be `ImportError` — that is an acceptable RED; prefer a real import of the missing helper so RED is an ImportError (clearer than a stub).
- Do **not** change denylist tests; they remain the contract for formula.
- The assert `primary_fill_is_strong(q, full)` documents today’s pollution; after Task 2 the **call site** stops passing `full`. Keep this assert so nobody “fixes” 0018 by weakening denylist accidentally — or rename to a comment + `assert primary_fill_is_strong(q, [title_custom])` which is the alone-strong title page (clearer, still True with current denylist because `custom ∉ GENERIC`).

Preferred alone-strong lock (clearer than depending on full-seed True):

```python
assert primary_fill_is_strong(q, [title_custom])  # title-only still "strong" as a page
assert not primary_fill_is_strong(q, fts_ranked_evidence([fts_weak, title_custom]))
```

- [ ] **Step 2: Run tests — expect FAIL**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_fts_ranked_evidence_keeps_only_fts_rank `
  tests/retrieval/test_query_recall_policy.py::test_primary_strong_evidence_ignores_title_only_custom_token `
  -v
```

Expected: **FAIL** — missing `fts_ranked_evidence` and/or FTS slice not yet used (if you inlined filter expectations differently).

- [ ] **Step 3: Commit tests only** (optional local checkpoint; or fold into Task 2 commit)

```powershell
git add tests/retrieval/test_query_recall_policy.py
# if you also touched test_query_execution_context.py, add it too
git commit -m "test(retrieval): primary strong evidence FTS slice (0018)"
```

---

### Task 2: Filter FTS evidence at primary_strong call site

**Files:**
- Modify: `src/retrieval/query_execution_context.py`
- Optionally modify: `src/retrieval/query_recall_policy.py` (add `fts_ranked_evidence` only)

**Interfaces:**

| Symbol | Spec |
|--------|------|
| `fts_ranked_evidence(items)` | Returns list/tuple of items where `item.get("fts_rank") is not None`. Missing key → exclude. Does **not** mutate inputs. |
| `_run_fallback_recovery` | `primary_fill_is_strong(question, fts_ranked_evidence(self.seed_selected), qualified_fts_hits=...)` |
| Denylist / GENERIC | **No edits** |

- [ ] **Step 1: Minimal implementation**

In `query_recall_policy.py` (recommended):

```python
def fts_ranked_evidence(
    items: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Primary-strong evidence: stage-one FTS hits only (fts_rank is not None)."""
    return [item for item in items if item.get("fts_rank") is not None]
```

(Add `Sequence` / `Mapping` / `Any` imports if not already present.)

In `query_execution_context.py` ~L615–620:

```python
primary_strong = (
    self.seed_has_primary_recall
    and recall_policy.primary_fill_is_strong(
        request_view.question,
        recall_policy.fts_ranked_evidence(self.seed_selected),
        qualified_fts_hits=self.seed_qualified_fts_hits,
    )
)
```

**Do not** filter `self.seed_selected` in place for ranking/display — only the strong-check argument.

**Do not** change `seed_has_primary_recall` computation (already FTS-era).

- [ ] **Step 2: Run new + regression tests — expect PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_fts_ranked_evidence_keeps_only_fts_rank `
  tests/retrieval/test_query_recall_policy.py::test_primary_strong_evidence_ignores_title_only_custom_token `
  tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_rejects_single_generic_title_token `
  tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_accepts_multi_generic_title_token_overlap `
  tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_uses_qualified_hits_and_relaxed_signals `
  tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_ignores_body_only_phrase `
  tests/retrieval/test_query_recall_policy.py::test_relaxed_fill_is_strong_detects_title_phrase_and_identifier `
  tests/retrieval/test_query_recall_policy.py::test_v2_body_only_primary_phrase_allows_raw_zero `
  -v
```

Then broader focused suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "relaxed_fill_is_strong or primary_fill_is_strong or fts_ranked or weak_primary or body_only" -v
```

Expected: **PASS**. Title-only `{custom}` excluded from strong evidence; FTS `{record}` still weak via denylist; SuiteScript FTS still strong; denylist multi-generic exception untouched.

- [ ] **Step 3: Commit**

```powershell
git add src/retrieval/query_execution_context.py src/retrieval/query_recall_policy.py tests/retrieval/test_query_recall_policy.py
git commit -m "fix(retrieval): primary strong evidence uses FTS slice (0018)"
```

---

### Task 3: CHANGELOG + focused pytest

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: CHANGELOG** — new dated section at top (match Chinese P2 style):

```markdown
## 2026-10-20 — 检索 P2：primary 强证据仅 FTS 切片（0018）

- `primary_fill_is_strong` 只看 `fts_rank is not None` 的 seed；title-candidate / 仅 graph 页不能单独把 `wiki_strong` 拉真挡住 `raw_zero`。
- 与 `has_primary_recall = bool(ranked)`（title merge 前）对齐；denylist / GENERIC / relaxed / 刀1 body 门不变。
- 结果里仍可保留 title/graph 页；本刀不加 `custom` 等泛化词。
```

- [ ] **Step 2: Re-run focused pytest**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "relaxed_fill_is_strong or primary_fill_is_strong or fts_ranked or weak_primary or body_only" -q --tb=line
```

Expected: PASS.

- [ ] **Step 3: Commit changelog**

```powershell
git add CHANGELOG.md
git commit -m "docs: changelog for primary strong FTS evidence slice"
```

---

### Task 4: Gold eval → `reports-p2-fts-strong-slice`

**Output:** `.eval/gold-sample-2026-10-19/reports-p2-fts-strong-slice/` (local artifact; do **not** force-add unless asked)

- [ ] **Step 1: Run eval**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
uv run llm-wiki-mcp retrieval-eval `
  --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" `
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl `
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json `
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-fts-strong-slice
```

(If `uv run` is unavailable on LU-C, use the same `.\.venv\Scripts\python.exe -m ...` form used in prior P0/P1/P2 notes.)

- [ ] **Step 2: Check 0018 only as this knife’s gate**

| Check | Expectation |
|-------|-------------|
| codingwork-0018 | Should schedule `raw_zero` / `wiki_weak_primary` (no longer `fallback:none` solely because title-candidate `{custom}` marked primary strong). Prefer raw Help Doc path or at least weak-primary + raw probe |
| `filter_correctness` | Must not regress vs `reports-p2-title-token-bar` / `reports-p2-c` |
| **0008 / 0015** | **Do not require** hit@10 improvement; note neutrally if unchanged |

If 0018 still misses for a *different* reason (empty raw, merge rank, etc.), write one honest residual line — do **not** add `custom` to GENERIC or reopen 0008/0015 in this knife.

- [ ] **Step 3: Do not push** unless the user asks. No PR unless asked.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Evidence = `fts_rank is not None` | Task 2 helper + call site |
| Filter in `_run_fallback_recovery` before `primary_fill_is_strong` | Task 2 |
| Denylist / GENERIC / relaxed / knife1 / qualified / coverage unchanged | Global + Task 2 “no edits” |
| Title/graph still allowed in results | Global (filter argument only) |
| 0018 gate; not 0008/0015; eval `reports-p2-fts-strong-slice` | Task 4 |
| TDD failing tests first | Task 1 before Task 2 |
| CHANGELOG Chinese P2 style | Task 3 |
| No push/PR unless asked | Global + Task 4 |
| Do not add `custom` | Global |

Placeholder scan: none. Types: `fts_rank is not None`; helper returns list of mappings; call site passes filtered sequence only.

---

## Execution handoff

Plan complete when saved to `docs/plans/2026-10-20-retrieval-p2-primary-strong-fts-slice.md` and committed (Task 0).

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — this session with executing-plans checkpoints  

Which approach?

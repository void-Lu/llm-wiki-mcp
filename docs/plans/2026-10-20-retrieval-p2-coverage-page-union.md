# Coverage Page-Union for Uncovered Latin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** codingwork-0015 runs the coverage branch and already has gold raw `Nsearch Module.md` at page_rank #1 in the recovery pool, but `merge_coverage_items` admits raw only when the **page-best passage** covers uncovered latin. High-BM25 URL/table junk wins `select_best_per_page`, sibling ordinals that contain `unauthenticated…` are never consulted → `n_covering=0` → early exit → `triggered=false` / `fallback=none`. Fix admission to use a **page-level token union** (all passages sharing `page_path` in the raw candidate list).

**Architecture:** Keep `select_best_per_page` as the **display / fusion carrier**. Change raw uncovered admission so `covered = page_token_union ∩ uncovered_terms` (or equivalent any-passage hit). Ensure the list passed into `merge_coverage_items` still contains **multi-ordinal** FTS hits for the same page (today `raw_recovery_candidates` collapses too early). Knife2 `authority = 0.35 × coverage_ratio` stays; `coverage_ratio` naturally uses the page-level covered set. **No 0008 pool work.**

**Tech Stack:** Python / existing retrieval tests / `.\.venv\Scripts\python.exe -m pytest` and `uv run llm-wiki-mcp retrieval-eval` (as in prior P2 plans).

## Global Constraints

- **Admission rule (frozen):** For each raw page, `page_tokens = ∪ tokens(title + " " + text)` over **all** candidate items with that `page_path` in the `raw_items` argument; `covered = {t ∈ uncovered_terms | t ∈ page_tokens}`; admit raw page iff `covered` non-empty (same gate as today, stronger evidence).
- **Display:** Still one row per page via `select_best_per_page` (BM25 / score carrier unchanged).
- **Data source (frozen for this knife):** Union from passages **present in `raw_items`** (pre-collapse FTS hits). **Do not** add a new index round-trip / `passages_at_ordinals` fetch in this knife. If a page has only one ordinal in `raw_items`, behavior equals today’s passage-local check (fail-safe).
- **Active/wiki side:** Leave covered semantics as today (selected block only) unless a tiny shared helper is reused without changing wiki admission.
- **Do not change:** denylist / `GENERIC_TITLE_TOKENS` / FTS strong-slice / `qualified_fts_hits` short-circuit / knife1 body gate / knife2 formula `0.35 × coverage_ratio` / `raw_recovery_candidates` ladder scoring / vector DB / `side` hyphen tokenization / 刀3 latin-probe.
- **0008 is not a success criterion** (pool membership residual is a later knife).
- TDD: failing tests first → fail → implement → pass → commit (per task).
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push / open PR** unless asked.
- Spec (source of truth): `docs/specs/2026-10-20-retrieval-p2-coverage-page-union-design.md` (commit `4830d70`).
- Diag (context only): `/workspace/sdd-p2/diag-0008-0015.md`.
- CHANGELOG entry required when implementing (Task 3); **Chinese**, match prior P2 style.

## File map

| File | Role |
|------|------|
| `src/retrieval/query_recall_policy.py` | Page-union helper + `merge_coverage_items` raw admission; **defer** `select_best_per_page` in `raw_recovery_candidates` so multi-ordinal hits reach merge (see Task 2) |
| `tests/retrieval/test_query_recall_policy.py` | New multi-ordinal / URL-junk fixtures; keep existing coverage fusion / knife2 / 0015-shaped v2 tests green |
| `CHANGELOG.md` | User-visible note (Chinese) |
| `docs/specs/2026-10-20-retrieval-p2-coverage-page-union-design.md` | Approved spec (`4830d70`) |
| `docs/plans/2026-10-20-retrieval-p2-coverage-page-union.md` | This plan |

**Do not modify for this knife:** `query_execution_context.py` coverage call shape (still `merge_coverage_items(selected, raw_candidate_items, …)`); denylist / FTS-slice helpers; `lexical_analyzer` tokenization of `client-side` → `side`; MCP signatures; 0008 candidate-pool generation.

### Current anchors (do not invent — as of FTS strong-slice on `v0.9.36` @ `1700979` / `4830d70`)

`_coverage_terms` + `merge_coverage_items` — `src/retrieval/query_recall_policy.py` ~L632–684:

```python
def _coverage_terms(item: dict[str, Any], terms: Sequence[str]) -> set[str]:
    hit = item["hit"]
    text = f"{hit.title} {hit.text}"
    available = set(tokens(text))
    return {term for term in terms if term in available}


def merge_coverage_items(...):
    active_pages = select_best_per_page(list(active_items))
    raw_pages = select_best_per_page(list(raw_items))
    ...
    for local_rank, item in enumerate([*active_pages, *raw_pages], 1):
        is_raw = item["hit"].source_kind == "raw"
        ...
        covered = _coverage_terms(item, uncovered_terms)  # ← page-best only today
        if is_raw and not covered:
            continue
        coverage_ratio = len(covered) / total_terms
        ...
        if is_raw:
            authority = 0.35 * coverage_ratio  # knife2 — keep formula
```

**Pipeline trap (must address in Task 2):** `raw_recovery_candidates` ~L593 already does:

```python
raw_candidate_items = select_best_per_page(raw_items)
```

before return. Call site (`query_execution_context` ~L293) therefore passes **one passage per page** into `merge_coverage_items`. Page-union over that list is a no-op. **Defer collapse:** keep multi-ordinal `raw_items` through return (apply `step_bonus` on unique `page_path`s as today, but do not drop sibling ordinals). Callers that need one-per-page already re-run `select_best_per_page` (`merge_coverage_items`, `merge_weak_primary_raw`, raw_only branch).

Existing fixtures to **keep green**:

| Test | Expectation that must survive |
|------|-------------------------------|
| `test_coverage_fusion_uses_raw_source_rank_not_bm25_absolute_value` | Rank/RRF local, not absolute BM25 |
| `test_coverage_fusion_uses_effective_rrf_k` | `rrf_k` still moves scores |
| `test_merge_coverage_items_prefers_high_coverage_raw_over_padding_wiki` | Full-cov raw beats partial wiki (knife2) |
| `test_v2_coverage_merge_surfaces_raw_help_doc` | 0015-shaped happy path still surfaces raw |
| `test_v2_coverage_keeps_primary_results_when_raw_does_not_cover_gap` | Non-covering raw still filtered |
| `test_v2_coverage_recovery_merges_raw_evidence_when_primary_misses_latin_term` | Coverage merge still fires |
| denylist / FTS-slice / SuiteScript / knife1 / Location | Unrelated; still green |

Live 0015 residual (diag): uncovered=`[unauthenticated,side,contexts]`; gold page #1; best snip URL junk `cov=[]`; `n_covering_before_merge=0`.

---

### Task 0: Docs (this plan)

- [x] Spec on branch: `docs/specs/2026-10-20-retrieval-p2-coverage-page-union-design.md` (`4830d70`)
- [ ] Write this plan; `git add -f`; commit **plan file only** (no `src/` / `tests/` yet)

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
git add -f docs/plans/2026-10-20-retrieval-p2-coverage-page-union.md
git commit -m "docs: plan coverage page-union (0015)"
```

Do **not** push.

---

### Task 1: Failing unit tests (TDD — no production code yet)

**Files:**
- Modify: `tests/retrieval/test_query_recall_policy.py`

**Suggested symbols / names:**

1. Helper under test (implement in Task 2): `page_coverage_terms(page_items, uncovered_terms) -> set[str]` — union tokens across items, then intersect with uncovered. Prefer export from `query_recall_policy` next to `_coverage_terms` (public or testable name).
2. `test_merge_coverage_admits_raw_when_sibling_passage_covers_uncovered` — multi-ordinal lock
3. `test_merge_coverage_admits_raw_when_best_passage_is_url_junk` — 0015-shaped fixture
4. Keep single-passage non-cover → still filtered

- [ ] **Step 1: Write failing tests**

```python
from retrieval.candidate_items import candidate_item
from retrieval.query_recall_policy import merge_coverage_items, page_coverage_terms
from retrieval.retrieval_index import PassageHit


def _raw_hit(
    path: str,
    *,
    passage_id: str,
    title: str,
    text: str,
    ordinal: int,
    score: float,
) -> dict:
    return candidate_item(
        PassageHit(
            passage_id=passage_id,
            page_path=path,
            title=title,
            heading_path=(),
            text=text,
            score=score,
            corpus="raw",
            authority="",
            source_kind="raw",
            ordinal=ordinal,
        ),
        score=score,
        fts_rank=ordinal + 1,
    )


PAGE = "raw/sources/file/NetSuite Help Docs/41_Nsearch Module/Nsearch Module.md"


def test_page_coverage_terms_unions_sibling_passages() -> None:
    best_junk = _raw_hit(
        PAGE,
        passage_id="p-junk",
        title="N/search Module",
        text="/ docs . oracle . com / en / cloud / table junk",
        ordinal=0,
        score=50.0,
    )
    sibling = _raw_hit(
        PAGE,
        passage_id="p-limit",
        title="N/search Module",
        text="unsupported in unauthenticated client-side contexts",
        ordinal=3,
        score=5.0,
    )
    uncovered = ["unauthenticated", "side", "contexts"]
    assert page_coverage_terms([best_junk], uncovered) == set()
    assert page_coverage_terms([best_junk, sibling], uncovered) == {
        "unauthenticated",
        "side",
        "contexts",
    }


def test_merge_coverage_admits_raw_when_sibling_passage_covers_uncovered() -> None:
    """Best ordinal misses uncovered; sibling on same page hits → page admitted."""
    best_junk = _raw_hit(
        PAGE,
        passage_id="p-junk",
        title="N/search Module",
        text="/ docs . oracle . com / en / cloud / table junk",
        ordinal=0,
        score=50.0,
    )
    sibling = _raw_hit(
        PAGE,
        passage_id="p-limit",
        title="N/search Module",
        text="unsupported in unauthenticated client-side contexts",
        ordinal=3,
        score=5.0,
    )
    uncovered = ["unauthenticated", "side", "contexts"]
    merged = merge_coverage_items([], [best_junk, sibling], uncovered, rrf_k=10)
    assert len(merged) == 1
    assert merged[0]["hit"].page_path == PAGE
    # Display carrier remains page-best (higher score), not the sibling
    assert merged[0]["hit"].passage_id == "p-junk"
    assert merged[0]["coverage_ratio"] == 1.0
    assert set(merged[0]["coverage_terms"]) == set(uncovered)


def test_merge_coverage_still_filters_raw_when_no_passage_covers() -> None:
    only_junk = _raw_hit(
        PAGE,
        passage_id="p-junk",
        title="N/search Module",
        text="/ docs . oracle . com / en / cloud / table junk",
        ordinal=0,
        score=50.0,
    )
    merged = merge_coverage_items(
        [], [only_junk], ["unauthenticated", "contexts"], rrf_k=10
    )
    assert merged == []
```

**Notes for implementers:**

- If `page_coverage_terms` is missing, Step 2 FAIL may be `ImportError` — acceptable RED.
- Tokenization: `tokens("client-side")` already yields `side` (and related); fixture text should include the same surface forms as live Help so uncovered keys match.
- Do **not** require a full `run_query_v2` vault harness for the new gate; unit-level `merge_coverage_items` is enough. Existing `test_v2_coverage_merge_surfaces_raw_help_doc` remains the integration regression.

- [ ] **Step 2: Run tests — expect FAIL**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_page_coverage_terms_unions_sibling_passages `
  tests/retrieval/test_query_recall_policy.py::test_merge_coverage_admits_raw_when_sibling_passage_covers_uncovered `
  tests/retrieval/test_query_recall_policy.py::test_merge_coverage_still_filters_raw_when_no_passage_covers `
  -v
```

Expected: **FAIL** — missing `page_coverage_terms` and/or merge still passage-local on page-best only (sibling ignored → empty merge).

- [ ] **Step 3: Commit tests only** (optional checkpoint; or fold into Task 2)

```powershell
git add tests/retrieval/test_query_recall_policy.py
git commit -m "test(retrieval): coverage page-union for uncovered latin (0015)"
```

---

### Task 2: Page-union admission + keep multi-ordinal raw candidates

**Files:**
- Modify: `src/retrieval/query_recall_policy.py`

**Interfaces:**

| Symbol | Spec |
|--------|------|
| `page_coverage_terms(page_items, terms) -> set[str]` | Union `tokens(title + " " + text)` over items; return `{t in terms if t in union}`. Empty `page_items` → empty set. Does not mutate inputs. |
| `merge_coverage_items` | Build `page_path → [items]` from **full** `raw_items` (before / in parallel with `select_best_per_page`). For each raw **page-best** carrier, `covered = page_coverage_terms(group, uncovered_terms)` instead of `_coverage_terms(item, …)`. Active/wiki rows may keep `_coverage_terms(item, …)`. |
| `raw_recovery_candidates` | **Stop** assigning `raw_candidate_items = select_best_per_page(raw_items)` as the returned list. Keep all eligible FTS ordinals. Still compute `step_bonus` keyed by unique `page_path` (apply bonus to every item of that page, or to the best then copy — pick one; prefer apply to all items sharing the path so relative order within page stays by raw score). Sort as today. |
| `_coverage_terms` | May remain as single-item helper used by wiki/active and by `page_coverage_terms` internally. |
| Knife2 | `authority = 0.35 * coverage_ratio` unchanged; `coverage_ratio = len(covered) / total_terms` with page-level `covered`. |

- [ ] **Step 1: Minimal implementation**

Suggested helper:

```python
def page_coverage_terms(
    page_items: Sequence[Mapping[str, Any]],
    terms: Sequence[str],
) -> set[str]:
    """Uncovered-latin hits using the union of all passages on one page."""
    if not page_items or not terms:
        return set()
    available: set[str] = set()
    for item in page_items:
        hit = item["hit"]
        available.update(tokens(f"{hit.title} {hit.text}"))
    return {term for term in terms if term in available}
```

In `merge_coverage_items`:

```python
from collections import defaultdict

raw_by_page: dict[str, list[dict[str, Any]]] = defaultdict(list)
for item in raw_items:
    raw_by_page[item["hit"].page_path].append(dict(item) if not isinstance(item, dict) else item)

active_pages = select_best_per_page(list(active_items))
raw_pages = select_best_per_page(list(raw_items))
...
covered = (
    page_coverage_terms(raw_by_page.get(item["hit"].page_path, [item]), uncovered_terms)
    if is_raw
    else _coverage_terms(item, uncovered_terms)
)
```

(Use whatever typing/`dict(item)` pattern the file already uses for `select_best_per_page` inputs.)

In `raw_recovery_candidates`, replace the collapse-before-return with multi-ordinal keep, e.g.:

```python
# BEFORE (today):
# raw_candidate_items = select_best_per_page(raw_items)

# AFTER: keep all ordinals; step_bonus still page-keyed
raw_candidate_items = list(raw_items)
if raw_candidate_items and raw_lexical_mode != "qualified_code":
    page_paths = sorted({item["hit"].page_path for item in raw_candidate_items})
    step_counts = step_counts_for_pages(page_paths, raw_store, raw_store)
    for item in raw_candidate_items:
        count = step_counts.get(item["hit"].page_path, 0)
        item["score"] = round(item["score"] + step_bonus(count), 12)
raw_candidate_items.sort(
    key=lambda item: (-item["score"], item["hit"].page_path, item["hit"].passage_id)
)
return raw_store, raw_candidate_items, raw_fts_hits, raw_index_warning, raw_lexical_mode
```

**Call-site note:** `query_execution_context` filters `raw_candidate_items` by `covered_ids` from merge (page-best `passage_id`s). After deferring collapse, that filter would drop sibling ordinals that helped admission but are not the best id — **OK** for fusion (merge already emits one carrier per page). If a later branch expects only page-best in `raw_candidate_items`, re-apply `select_best_per_page` **after** the coverage id filter, or filter to covered **page_paths** then `select_best_per_page`. Prefer: after coverage pre-filter, keep items whose `page_path` is in the admitted set (not only matching best `passage_id`), then `select_best_per_page` before the second merge / pooling if duplicates would confuse ranking. Minimal patch sketch:

```python
covered_paths = {item["hit"].page_path for item in covered_raw_items}
raw_candidate_items = [
    item for item in raw_candidate_items
    if item["hit"].page_path in covered_paths
]
# optional: raw_candidate_items = select_best_per_page(raw_candidate_items)
```

Only touch `query_execution_context.py` if the `passage_id`-only filter breaks multi-ordinal → empty second merge. Prefer fixing the filter to **page_path** membership; do not otherwise refactor execution context.

- [ ] **Step 2: Run new + regression tests — expect PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_page_coverage_terms_unions_sibling_passages `
  tests/retrieval/test_query_recall_policy.py::test_merge_coverage_admits_raw_when_sibling_passage_covers_uncovered `
  tests/retrieval/test_query_recall_policy.py::test_merge_coverage_still_filters_raw_when_no_passage_covers `
  tests/retrieval/test_query_recall_policy.py::test_merge_coverage_items_prefers_high_coverage_raw_over_padding_wiki `
  tests/retrieval/test_query_recall_policy.py::test_coverage_fusion_uses_raw_source_rank_not_bm25_absolute_value `
  tests/retrieval/test_query_recall_policy.py::test_coverage_fusion_uses_effective_rrf_k `
  tests/retrieval/test_query_recall_policy.py::test_v2_coverage_merge_surfaces_raw_help_doc `
  tests/retrieval/test_query_recall_policy.py::test_v2_coverage_keeps_primary_results_when_raw_does_not_cover_gap `
  tests/retrieval/test_query_recall_policy.py::test_v2_coverage_recovery_merges_raw_evidence_when_primary_misses_latin_term `
  -v
```

Then a slightly broader net:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "coverage or page_coverage or merge_coverage or fts_ranked or primary_fill_is_strong or body_only" -v
```

Expected: **PASS**. Sibling evidence admits page; best passage remains carrier; single junk still filtered; knife2 / RRF / denylist / FTS-slice regressions green.

- [ ] **Step 3: Commit**

```powershell
git add src/retrieval/query_recall_policy.py tests/retrieval/test_query_recall_policy.py
# include query_execution_context.py only if page_path filter patch was required
git commit -m "fix(retrieval): coverage page-union for uncovered latin (0015)"
```

---

### Task 3: CHANGELOG + focused pytest

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: CHANGELOG** — new dated section at top (Chinese P2 style):

```markdown
## 2026-10-20 — 检索 P2：coverage 未覆盖拉丁词改为页级并集（0015）

- `merge_coverage_items` 对 raw 是否盖住 uncovered latin：按同 `page_path` 全部候选 passage 的 token **并集**判定，不再只看 `select_best_per_page` 选中块。
- 展示/融合载体仍用 page-best；刀2 `authority = 0.35 × coverage_ratio` 公式不变（ratio 用页级 covered）。
- `raw_recovery_candidates` 向 coverage 传递多 ordinal FTS 命中（不再提前压成一页一条）；缺兄弟时行为与旧 passage 门一致。
- 本刀不修 0008 池召回、不做刀3、不动 denylist / FTS strong-slice。
```

- [ ] **Step 2: Re-run focused pytest**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "coverage or page_coverage or merge_coverage or fts_ranked or primary_fill_is_strong or body_only" -q --tb=line
```

Expected: PASS.

- [ ] **Step 3: Commit changelog**

```powershell
git add CHANGELOG.md
git commit -m "docs: changelog for coverage page-union (0015)"
```

---

### Task 4: Gold eval → `reports-p2-coverage-page-union`

**Output:** `.eval/gold-sample-2026-10-19/reports-p2-coverage-page-union/` (local artifact; do **not** force-add unless asked)

- [ ] **Step 1: Run eval**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
uv run llm-wiki-mcp retrieval-eval `
  --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" `
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl `
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json `
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-coverage-page-union
```

(If `uv run` is unavailable on LU-C, use the same `.\.venv\Scripts\python.exe -m ...` form used in prior P0/P1/P2 notes.)

- [ ] **Step 2: Check gates**

| Check | Expectation |
|-------|-------------|
| codingwork-**0015** | `coverage.triggered=true` (or equivalent raw visible in pipeline); gold raw Help path in results / recall@10 **> 0** vs `reports-p2-fts-strong-slice` (was 0) |
| codingwork-**0018** | Still green (`raw_zero` / `wiki_weak_primary` path; recall not regressed to 0) |
| `filter_correctness` | Must not regress vs `reports-p2-fts-strong-slice` / `reports-p2-c` |
| codingwork-**0008** | **Do not require** improvement; note neutrally if still miss (pool residual) |

If 0015 still misses because the limiting ordinal was **never** in the FTS hit list (not merely dropped by `select_best_per_page`), write one honest residual line — do **not** add index re-fetch or 0008 pool hacks in this knife.

- [ ] **Step 3: Do not push** unless the user asks. No PR unless asked.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Raw uncovered = page_path passage union (or any-passage equiv) | Task 2 `page_coverage_terms` + merge |
| Landing `merge_coverage_items` / `_coverage_terms` (+ helper) | Task 2 |
| Display still `select_best_per_page` | Task 2 |
| Fail-safe when no siblings in list | Global + unit `still_filters` |
| Defer / supply multi-ordinal into merge | Task 2 `raw_recovery_candidates` |
| Knife2 formula unchanged | Global + Task 2 |
| Denylist / FTS-slice / 刀3 / 0008 / vectors / `side` tokenization unchanged | Global |
| 0015 success; 0008 not success; 0018 regression; eval `reports-p2-coverage-page-union` | Task 4 |
| TDD failing tests first | Task 1 before Task 2 |
| CHANGELOG Chinese P2 style | Task 3 |
| No push/PR unless asked | Global + Task 4 |

Placeholder scan: none. Types: `page_coverage_terms` returns `set[str]`; merge still returns fused candidate dicts; multi-ordinal list remains `list[dict]`.

---

## Execution handoff

Plan complete when saved to `docs/plans/2026-10-20-retrieval-p2-coverage-page-union.md` and committed (Task 0).

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks
2. **Inline Execution** — this session with executing-plans checkpoints

Which approach?

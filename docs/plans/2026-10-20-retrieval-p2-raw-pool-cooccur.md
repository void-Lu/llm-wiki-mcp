# Raw Pool Co-occurrence / Phrase Recall Implementation Plan (0008)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** codingwork-0008 (`email capture plug-in implementation`) already runs coverage (`triggered=true`), but gold Help pages

- `Developing an Email Capture Plug-in Implementation.md`
- `Email Capture Plug-in Interface Description.md`

never enter the raw coverage candidate pool. Live ladder settles on **`qualified_code`** because hyphenated **`plug-in`** is parsed as `QualifiedIdentifier(plug/in)`. That FTS (`"plug" AND "in" OR "plugin"…`) **replaces** strict hits and, with `identifier_phrase.run_if_empty=True`, **skips** the AND phrase step that would require `email`+`capture`(+`plug`+`implementation`). Pool fills with Admin **Enable…** + generic `* Plug-in Implementation` pages → recall@10 = 0. Knife2 cannot help pages outside the pool.

**Architecture:** Keep the existing raw search ladder. When the question has a multi-word Latin entity (`identifier_phrase_tokens` length ≥ 2), **always** run a supplementary `identifier_phrase` FTS and **union-merge** those hits into the ladder result by `passage_id` (higher score wins). Fail-open if the secondary search is empty or errors. Apply existing identifier-phrase score bonuses whenever phrases exist — **not only** when `raw_lexical_mode == "identifier_phrase"`. Do **not** rewrite `has_qualified_identifier` / stop treating `plug-in` as a QI in this knife. Do **not** change 0015 page-union, 0018 denylist / FTS-slice, or knife2 formula.

**Tech Stack:** Python / existing retrieval tests / `.\.venv\Scripts\python.exe -m pytest` and `uv run llm-wiki-mcp retrieval-eval` (as in prior P2 plans).

## Global Constraints

- **Co-occur rule (frozen):** If `len(identifier_phrase_tokens(question)) >= 2`, after `search_ladder(...)` (or as an explicit extra search against the same `raw_store`), run `search_fts(..., mode="identifier_phrase", limit=candidate_limit, …)` and **union** into ladder hits by `passage_id`. Never replace-only with phrase hits; never clear the ladder set when phrase returns empty.
- **Bonus rule (frozen):** When `identifier_phrases(question)` is non-empty, compute phrase bonuses (`IDENTIFIER_PHRASE_BONUS` / `_raw_recovery_bonus`) for scoring **regardless of** `raw_lexical_mode` (today bonuses are gated on `mode == "identifier_phrase"`, which 0008 never reaches).
- **Reported mode:** Keep `raw_lexical_mode` from the ladder (e.g. still `qualified_code` when QI fired). Do **not** invent a new public lexical mode name in this knife.
- **Cap / limit:** Keep `candidate_limit` / `RAW_FALLBACK_LIMIT` semantics; phrase union must land gold **inside** the FTS hit set that feeds `merge_coverage_items` (coverage path does not re-cap with `raw_cap` the way weak-primary does).
- **Do not change:** `merge_coverage_items` / `page_coverage_terms` (0015); denylist / `GENERIC_TITLE_TOKENS` / `fts_ranked_evidence` / `primary_fill_is_strong` (0018); knife2 `0.35 × coverage_ratio`; fork B diversify/quota; fork C authority-only; vector DB; `has_qualified_identifier` extraction rules; module-name aliases.
- **Success gates:** gold ∈ coverage raw pool; codingwork-**0008** recall@10 **> 0**; **0015 / 0018** still green; `filter_correctness` not regress.
- TDD: failing tests first → fail → implement → pass → commit (per task).
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push / open PR** unless asked.
- Spec (source of truth): `docs/specs/2026-10-20-retrieval-p2-raw-pool-cooccur-design.md` (commit `389a1ad`).
- Diag (context only): `/workspace/sdd-p2/diag-0008-0015.md`.
- CHANGELOG entry required when implementing (Task 3); **Chinese**, match prior P2 style.

## File map

| File | Role |
|------|------|
| `src/retrieval/query_recall_policy.py` | Phrase co-occur union + bonus ungating inside / beside `raw_recovery_candidates`; optional tiny pure helpers |
| `tests/retrieval/test_query_recall_policy.py` | New co-occur / plug-in-QI residual fixtures; keep 0015 page-union + 0018 denylist/FTS-slice green |
| `CHANGELOG.md` | User-visible note (Chinese) |
| `docs/specs/2026-10-20-retrieval-p2-raw-pool-cooccur-design.md` | Approved spec (`389a1ad`) |
| `docs/plans/2026-10-20-retrieval-p2-raw-pool-cooccur.md` | This plan |

**Do not modify for this knife:** `query_execution_context.py` coverage call shape (unless a one-line import is somehow required — prefer all logic in recall policy); `lexical_analyzer.has_qualified_identifier` / `extract_qualified_identifiers`; coverage page-union helpers; denylist / FTS-slice.

### Current anchors (do not invent — as of page-union on `v0.9.36` @ `7a677b2` / spec `389a1ad`)

Live 0008 probe (diag + LU-C):

```text
question = "email capture plug-in implementation"
has_qualified_identifier → True   # plug-in → QualifiedIdentifier plug/in
qualified_code FTS → ("plug" AND "in" OR "plug in" OR "plugin")
identifier_phrase_tokens → ["email","capture","plug","implementation"]
identifier_phrase_fts_query → "email" AND "capture" AND "plug" AND "implementation"
ladder merge=replace_if_nonempty; identifier_phrase.run_if_empty=True
→ mode=qualified_code; phrase step skipped; gold ∉ pool (~42 pages)
```

`raw_recovery_candidates` — `src/retrieval/query_recall_policy.py` ~L481–610:

```python
ladder_steps = [LadderStep("strict", "strict", request.question, common_kwargs)]
if has_qualified_identifier(request.question):
    ladder_steps.append(
        LadderStep("qualified_code", "qualified_code", request.question, common_kwargs)
    )
ladder_steps.extend([
    LadderStep(
        "identifier_phrase", "identifier_phrase", request.question,
        {**common_kwargs, "term_variants": term_variants or {}},
        run_if_empty=True,   # ← skipped when qualified_code filled
        ignore_errors=True,
    ),
    # raw_prefix, relaxed …
])
raw_hits, raw_lexical_mode = search_ladder(
    raw_store, steps=ladder_steps, merge="replace_if_nonempty",
    limit=candidate_limit, swallow_index_errors=True,
)
...
query_identifier_phrases = (
    identifier_phrases(request.question)
    if raw_lexical_mode == "identifier_phrase"   # ← 0008 never True
    else []
)
```

Existing fixtures to **keep green**:

| Test | Expectation that must survive |
|------|-------------------------------|
| `test_page_coverage_terms_unions_sibling_passages` / merge sibling / still_filters | 0015 page-union |
| `test_v2_coverage_merge_surfaces_raw_help_doc` | 0015-shaped coverage still surfaces raw |
| `test_merge_coverage_items_prefers_high_coverage_raw_over_padding_wiki` | knife2 |
| `test_fts_ranked_evidence_*` / denylist primary tests | 0018 |
| `test_v2_raw_fallback_combines_identifier_phrase_docs_above_bigram_noise` | genuine identifier_phrase path |
| `test_v2_raw_fallback_recovers_qualified_module_names_from_chinese_questions` | real `qualified_code` still works |
| `test_v2_identifier_phrase_fills_procedural_sections_of_selected_guide_pages` | phrase guide fill |

---

### Task 0: Docs (this plan)

- [x] Spec on branch: `docs/specs/2026-10-20-retrieval-p2-raw-pool-cooccur-design.md` (`389a1ad`)
- [ ] Write this plan; `git add -f`; commit **plan file only** (no `src/` / `tests/` yet)

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
git add -f docs/plans/2026-10-20-retrieval-p2-raw-pool-cooccur.md
git commit -m "docs: plan raw pool co-occurrence (0008)"
```

Do **not** push.

---

### Task 1: Failing unit tests (TDD — no production code yet)

**Files:**
- Modify: `tests/retrieval/test_query_recall_policy.py`

**Suggested symbols / names:**

1. Helpers under test (implement in Task 2), prefer export from `query_recall_policy` next to `raw_recovery_candidates`:
   - `should_augment_raw_phrase_cooccur(question: str) -> bool` — `len(identifier_phrase_tokens(question)) >= 2`
   - `union_passage_hits_by_id(primary, secondary) -> list[PassageHit]` — by `passage_id`, keep higher `score`, stable tie-break `(page_path, passage_id)`
2. `test_should_augment_raw_phrase_cooccur_for_multiword_entity` — True for 0008 query; False for single-token / empty
3. `test_union_passage_hits_by_id_keeps_higher_score_and_adds_missing` — pure merge
4. `test_raw_recovery_merges_phrase_cooccur_when_qualified_code_filled` — 0008-shaped lock (tmp vault or fake store)
5. `test_raw_recovery_phrase_cooccur_fail_open_when_phrase_empty` — ladder set preserved
6. Single-token / no multiword run: no forced empty pool / behavior unchanged

- [ ] **Step 1: Write failing tests**

Pure helpers (must fail until Task 2 exports them):

```python
from retrieval.lexical_analyzer import identifier_phrase_tokens
from retrieval.query_recall_policy import (
    should_augment_raw_phrase_cooccur,
    union_passage_hits_by_id,
)
from retrieval.retrieval_index import PassageHit


def _hit(pid: str, path: str, title: str, text: str, score: float) -> PassageHit:
    return PassageHit(
        passage_id=pid,
        page_path=path,
        title=title,
        heading_path=(),
        text=text,
        score=score,
        corpus="raw",
        authority="",
        source_kind="raw",
        ordinal=0,
    )


def test_should_augment_raw_phrase_cooccur_for_multiword_entity() -> None:
    q = "email capture plug-in implementation"
    assert len(identifier_phrase_tokens(q)) >= 2
    assert should_augment_raw_phrase_cooccur(q) is True
    assert should_augment_raw_phrase_cooccur("N/record") is False
    assert should_augment_raw_phrase_cooccur("plugin") is False


def test_union_passage_hits_by_id_keeps_higher_score_and_adds_missing() -> None:
    primary = [
        _hit("a", "raw/a.md", "Enable Plug-in", "plugin enable", 10.0),
        _hit("b", "raw/b.md", "Custom Plug-in", "generic", 8.0),
    ]
    secondary = [
        _hit("b", "raw/b.md", "Custom Plug-in", "generic", 9.5),  # higher → win
        _hit(
            "g",
            "raw/Developing an Email Capture Plug-in Implementation.md",
            "Developing an Email Capture Plug-in Implementation",
            "process(email) capture",
            7.0,
        ),
    ]
    merged = union_passage_hits_by_id(primary, secondary)
    by_id = {h.passage_id: h for h in merged}
    assert set(by_id) == {"a", "b", "g"}
    assert by_id["b"].score == 9.5
    assert "Email Capture" in by_id["g"].title
```

0008-shaped integration (prefer a small tmp raw index like existing `test_v2_raw_fallback_*` helpers — reuse whatever vault/index fixture the file already uses). Intent:

```python
def test_raw_recovery_merges_phrase_cooccur_when_qualified_code_filled(
    tmp_path: Path,
) -> None:
    """plug-in QI fills qualified_code; gold only matches email+capture AND phrase.

    After co-occur union, Developing… path must appear in raw_recovery_candidates.
    """
    # Arrange: raw pages including generic "X Plug-in Implementation" noise
    # plus Developing an Email Capture Plug-in Implementation (body has email+capture).
    # Act: raw_recovery_candidates(...) or run_query_v2 coverage path.
    # Assert: gold page_path ∈ {item["hit"].page_path for item in raw_candidate_items}
    ...
```

Fail-open:

```python
def test_union_passage_hits_by_id_fail_open_on_empty_secondary() -> None:
    primary = [_hit("a", "raw/a.md", "A", "a", 1.0)]
    assert [h.passage_id for h in union_passage_hits_by_id(primary, [])] == ["a"]
```

**Notes for implementers:**

- Do **not** hardcode vault path prefix `03_ECP` in production code; fixtures may use realistic Help titles.
- Avoid asserting Admin Enable must lose ranking — success is **membership + recall@10>0**.
- If `ImportError` on missing helpers → acceptable RED for Step 2.

- [ ] **Step 2: Run tests — expect FAIL**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_should_augment_raw_phrase_cooccur_for_multiword_entity `
  tests/retrieval/test_query_recall_policy.py::test_union_passage_hits_by_id_keeps_higher_score_and_adds_missing `
  tests/retrieval/test_query_recall_policy.py::test_union_passage_hits_by_id_fail_open_on_empty_secondary `
  tests/retrieval/test_query_recall_policy.py::test_raw_recovery_merges_phrase_cooccur_when_qualified_code_filled `
  -v
```

Expected: **FAIL** — missing helpers and/or gold still absent when qualified_code dominates.

- [ ] **Step 3: Commit tests only** (optional checkpoint; or fold into Task 2)

```powershell
git add tests/retrieval/test_query_recall_policy.py
git commit -m "test(retrieval): raw pool phrase co-occurrence (0008)"
```

---

### Task 2: Phrase co-occur union + ungated bonuses

**Files:**
- Modify: `src/retrieval/query_recall_policy.py`
- Possibly import already-present: `identifier_phrase_tokens` from `lexical_analyzer` (today only `identifier_phrases` is imported — add the tokens helper to the import list)

**Interfaces:**

| Symbol | Spec |
|--------|------|
| `should_augment_raw_phrase_cooccur(question) -> bool` | `len(identifier_phrase_tokens(question)) >= 2` |
| `union_passage_hits_by_id(primary, secondary) -> list[PassageHit]` | Merge by `passage_id`; keep higher `score`; if equal keep primary; sort `(-score, page_path, passage_id)` |
| `raw_recovery_candidates` | After successful `search_ladder`, if `should_augment_raw_phrase_cooccur(question)`: `search_fts(..., mode="identifier_phrase", limit=candidate_limit, term_variants=…)` inside try/except (`RetrievalIndexError` → ignore secondary); `raw_hits = union_passage_hits_by_id(raw_hits, phrase_hits)`. Do **not** change `raw_lexical_mode` from ladder. |
| Phrase bonuses | `query_identifier_phrases = identifier_phrases(request.question)` whenever non-empty (drop the `raw_lexical_mode == "identifier_phrase"` gate). Keep bonus magnitudes. |
| Ladder steps | **Optional micro-tweak only if needed:** leave `run_if_empty` as-is; prefer **post-ladder union** so qualified_code telemetry stays accurate and we do not silently skip QI hits. |

- [ ] **Step 1: Minimal implementation**

Suggested helpers:

```python
from retrieval.lexical_analyzer import (
    identifier_phrases,
    identifier_phrase_tokens,  # add
    has_qualified_identifier,
    ...
)

def should_augment_raw_phrase_cooccur(question: str) -> bool:
    return len(identifier_phrase_tokens(question)) >= 2


def union_passage_hits_by_id(
    primary: Sequence[PassageHit],
    secondary: Sequence[PassageHit],
) -> list[PassageHit]:
    by_id: dict[str, PassageHit] = {}
    for hit in primary:
        by_id[hit.passage_id] = hit
    for hit in secondary:
        cur = by_id.get(hit.passage_id)
        if cur is None or hit.score > cur.score:
            by_id[hit.passage_id] = hit
    return sorted(
        by_id.values(),
        key=lambda h: (-h.score, h.page_path, h.passage_id),
    )
```

In `raw_recovery_candidates`, after ladder (and after error early-return), before building `raw_items`:

```python
if should_augment_raw_phrase_cooccur(request.question):
    try:
        phrase_hits = raw_store.search_fts(
            request.question,
            limit=candidate_limit,
            mode="identifier_phrase",
            term_variants=term_variants or {},
            **common_kwargs,
        )
    except RetrievalIndexError:
        phrase_hits = []
    if phrase_hits:
        raw_hits = union_passage_hits_by_id(raw_hits, phrase_hits)
```

Ungate bonuses:

```python
query_identifier_phrases = identifier_phrases(request.question)
# was: only when raw_lexical_mode == "identifier_phrase"
```

Keep multi-ordinal return + `step_bonus` logic from 0015 unchanged.

Export new helpers in `__all__` only if the module already lists public helpers that way; otherwise tests import by name (file already exports many names in a tuple ~L104+ — add if present).

- [ ] **Step 2: Run new + regression tests — expect PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_should_augment_raw_phrase_cooccur_for_multiword_entity `
  tests/retrieval/test_query_recall_policy.py::test_union_passage_hits_by_id_keeps_higher_score_and_adds_missing `
  tests/retrieval/test_query_recall_policy.py::test_union_passage_hits_by_id_fail_open_on_empty_secondary `
  tests/retrieval/test_query_recall_policy.py::test_raw_recovery_merges_phrase_cooccur_when_qualified_code_filled `
  tests/retrieval/test_query_recall_policy.py::test_v2_raw_fallback_combines_identifier_phrase_docs_above_bigram_noise `
  tests/retrieval/test_query_recall_policy.py::test_v2_raw_fallback_recovers_qualified_module_names_from_chinese_questions `
  tests/retrieval/test_query_recall_policy.py::test_page_coverage_terms_unions_sibling_passages `
  tests/retrieval/test_query_recall_policy.py::test_merge_coverage_admits_raw_when_sibling_passage_covers_uncovered `
  tests/retrieval/test_query_recall_policy.py::test_fts_ranked_evidence_keeps_only_fts_rank `
  -v
```

Broader net:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "cooccur or phrase or coverage or page_coverage or fts_ranked or primary_fill_is_strong or qualified_code or identifier_phrase or raw_fallback" -v
```

Expected: **PASS**. Gold path ∈ candidates under plug-in QI; empty phrase fail-open; 0015/0018/qualified/phrase regressions green.

- [ ] **Step 3: Commit**

```powershell
git add src/retrieval/query_recall_policy.py tests/retrieval/test_query_recall_policy.py
git commit -m "fix(retrieval): raw pool phrase co-occurrence (0008)"
```

---

### Task 3: CHANGELOG + focused pytest

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: CHANGELOG** — new dated section at top (Chinese P2 style):

```markdown
## 2026-10-20 — 检索 P2：raw 池多词实体共现 / 短语召回（0008）

- `raw_recovery_candidates`：问句含多词拉丁实体（`identifier_phrase_tokens` ≥ 2）时，在 ladder 结果上**并入**一次 `identifier_phrase` FTS（按 `passage_id` 取高分），避免 `plug-in` 等误触发的 `qualified_code` 挤掉 `email`+`capture` 共现页。
- 短语加分不再仅限 `raw_lexical_mode == identifier_phrase`；ladder 上报 mode 不变。
- 二次检索空/失败 fail-open；不动 0015 page-union、0018 denylist / FTS-slice、刀2 公式、池内去重配额。
```

- [ ] **Step 2: Re-run focused pytest**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "cooccur or phrase or coverage or page_coverage or fts_ranked or primary_fill_is_strong or qualified_code or identifier_phrase or raw_fallback" -q --tb=line
```

Expected: PASS.

- [ ] **Step 3: Commit changelog**

```powershell
git add CHANGELOG.md
git commit -m "docs: changelog for raw pool co-occurrence (0008)"
```

---

### Task 4: Gold eval → `reports-p2-raw-pool-cooccur`

**Output:** `.eval/gold-sample-2026-10-19/reports-p2-raw-pool-cooccur/` (local artifact; do **not** force-add unless asked)

- [ ] **Step 1: Run eval**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
uv run llm-wiki-mcp retrieval-eval `
  --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" `
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl `
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json `
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-raw-pool-cooccur
```

(If `uv run` is unavailable on LU-C, use the same `.\.venv\Scripts\python.exe -m ...` form used in prior P0/P1/P2 notes.)

- [ ] **Step 2: Check gates**

| Check | Expectation |
|-------|-------------|
| codingwork-**0008** | Gold Developing and/or Interface **∈** coverage raw pool; recall@10 **> 0** vs `reports-p2-coverage-page-union` / `reports-p2-fts-strong-slice` (was 0). Admin Enable may still rank high — OK if gold also hits. |
| codingwork-**0015** | Still green (page-union; recall not back to 0) |
| codingwork-**0018** | Still green (`raw_zero` / `wiki_weak_primary`; recall not regressed to 0) |
| `filter_correctness` | Must not regress vs prior P2 report |

If 0008 still misses because gold never matches `identifier_phrase` AND (index/tokenization), write one honest residual — do **not** add fork B diversify or fork C authority hacks in this knife.

- [ ] **Step 3: Do not push** unless the user asks. No PR unless asked (may later stack onto PR #8).

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Raise pool recall via co-occur / phrase (fork A) | Task 2 post-ladder `identifier_phrase` union |
| Landing `raw_recovery_candidates` (or equivalent) | Task 2 |
| Union / fail-open with loose ladder set | Task 2 + fail-open test |
| Phrase/AND not only when mode==identifier_phrase | Task 2 bonus ungating |
| No diversify quota (fork B); no knife2-only (fork C) | Global |
| 0015 page-union / 0018 denylist+FTS-slice unchanged | Global + Task 4 |
| Knife2 formula unchanged | Global |
| Success: gold∈pool + 0008 recall>0; filter; 0015/0018 green | Task 4 |
| TDD failing tests first | Task 1 before Task 2 |
| CHANGELOG Chinese P2 style | Task 3 |
| Eval `reports-p2-raw-pool-cooccur` | Task 4 |
| No push/PR unless asked | Global + Task 4 |
| No hardcode single path prefix | Global + Task 1 notes |

Placeholder scan: none intentional. Types: helpers return `bool` / `list[PassageHit]`; `raw_recovery_candidates` return tuple unchanged.

**Causal note (for implementers):** Do not “fix” 0008 by deleting `plug-in` from qualified-id extraction in this knife — that is a broader lexical change. Spec freezes **pool co-occurrence union**.

---

## Execution handoff

Plan complete when saved to `docs/plans/2026-10-20-retrieval-p2-raw-pool-cooccur.md` and committed (Task 0).

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks
2. **Inline Execution** — this session with executing-plans checkpoints

Which approach?
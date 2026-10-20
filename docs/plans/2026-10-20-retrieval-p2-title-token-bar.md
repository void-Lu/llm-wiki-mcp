# Primary Title-Token Denylist Bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After P2 knife1, codingwork-0018 still marks primary strong via a single generic title-token overlap (`N/record 模块` ∩ query → `{record}`), which blocks `raw_zero`. Raise the **primary-only** title-token / ED-1 bar with a GENERIC denylist so `{record}` is weak, multi-generic `{record,type}` stays strong, and distinctive single tokens like `suitescript` stay strong — without changing relaxed.

**Architecture:** One lever in `relaxed_fill_is_strong` when `body_evidence=False` (the primary path via `primary_fill_is_strong`). Exact title-token arm and ED-1 arm share one formula on the overlap set; `body_evidence=True` (relaxed) keeps “any non-empty intersection → strong”. No changes to coverage merge, `qualified_fts_hits` short-circuit, phrase/ident body gate, or ladder orchestration.

**Tech Stack:** Python / existing retrieval tests / uv (`uv run` or `.\.venv\Scripts\python.exe -m pytest` as in prior P2 plans).

## Global Constraints

- **Primary only:** gate applies when `body_evidence=False`; relaxed (`True`) unchanged
- **Formula (frozen):** `ov = title_tokens ∩ evidence_tokens`; `content = ov − GENERIC_TITLE_TOKENS`; **strong iff** `len(content) ≥ 1` **OR** `len(ov) ≥ 2`
- **GENERIC list (from spec, casefold):** `record`, `type`, `object`, `field`, `fields`, `module`, `guide`, `overview`, `helper`, `helpers`, `api`, `doc`, `docs`
- ED-1 on primary uses the **same** filter / formula (no single generic fuzzy hit alone)
- Do **not** change: exact title↔question, phrase/ident (knife1 body gate), `qualified_fts_hits > 0`, coverage / merge / raw_zero orchestration, relaxed title-token
- No aliases / vectors / BM25 absolute cutoff / 0003–0011 unanswered gate
- **0008 / 0015 are not success criteria** for this knife
- TDD: failing tests first → fail → implement → pass → commit (per task)
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push / open PR** unless asked
- Spec (source of truth): `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md` (commit `87709af`)
- CHANGELOG entry required when implementing (Task 3)

## File map

| File | Role |
|------|------|
| `src/retrieval/query_recall_policy.py` | Add `GENERIC_TITLE_TOKENS`; gate title-token + ED-1 arms when `body_evidence=False` |
| `tests/retrieval/test_query_recall_policy.py` | New primary False / multi-generic True; keep SuiteScript / Location / knife1 fixtures |
| `CHANGELOG.md` | User-visible note (Chinese, match prior P2 style) |
| `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md` | Approved spec (already committed) |
| `docs/plans/2026-10-20-retrieval-p2-title-token-bar.md` | This plan |

**Do not modify for this knife:** `query_execution_context.py` call sites (already `body_evidence=False` for primary), `query_recovery.py`, `lexical_analyzer.py` stopwords, MCP tool signatures.

### Current anchors (do not invent — as of knife1 on `v0.9.36`)

`primary_fill_is_strong` — `src/retrieval/query_recall_policy.py:147`:

```python
if not items:
    return False
if qualified_fts_hits > 0:
    return True
return relaxed_fill_is_strong(
    question, items, extra_terms=extra_terms, body_evidence=False
)
```

`relaxed_fill_is_strong` — `:171` (`body_evidence: bool = True`). Title-token loop today `:227–241`:

```python
title_tokens = {tok.casefold() for tok in tokens(title) if tok}
if title_tokens & evidence_tokens:          # ANY non-empty → strong
    return True
for title_tok in title_tokens:              # ED-1, both sides ≥ 4
    if len(title_tok) < 4:
        continue
    for term in question_tokens:
        if (
            term != title_tok
            and len(term) >= 4
            and abs(len(term) - len(title_tok)) <= 1
            and edit_distance(term, title_tok) <= 1
        ):
            return True
```

`body_evidence` already gates phrase/ident blobs (`:210–211`); it does **not** yet gate title-token / ED-1.

Existing fixtures to **keep green**:

| Test | Expectation that must survive |
|------|-------------------------------|
| `test_primary_fill_is_strong_uses_qualified_hits_and_relaxed_signals` | `SuiteScript Guide` + q with `SuiteScript` → primary **True**; empty False; weak False; `qualified_fts_hits=1` True |
| `test_primary_fill_is_strong_ignores_body_only_phrase` | body-only phrase primary **False**; full title phrase **True**; relaxed body phrase **True** |
| `test_relaxed_fill_is_strong_detects_title_phrase_and_identifier` | `Location overview` single-token **True** (relaxed) |
| `test_v2_body_only_primary_phrase_allows_raw_zero` | knife1 integration still allows raw |

P2-c residual (0018): primary still strong via `{record}` → `fallback:none`, `raw_fts=0`. Gold is raw Help Doc title exact match.

---

### Task 0: Docs (this plan)

- [x] Spec on branch: `docs/specs/2026-10-20-retrieval-p2-title-token-bar-design.md` (`87709af`)
- [ ] Write this plan; `git add -f`; commit **plan file only** (no `src/` / `tests/` yet)

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
git add -f docs/plans/2026-10-20-retrieval-p2-title-token-bar.md
git commit -m "docs: plan primary title-token denylist bar (0018)"
```

Do **not** push.

---

### Task 1: Failing unit tests (TDD — no production code yet)

**Files:**
- Test: `tests/retrieval/test_query_recall_policy.py`

**Suggested new names (place near existing primary tests ~L847+):**

1. `test_primary_fill_is_strong_rejects_single_generic_title_token` — lock 0018
2. `test_primary_fill_is_strong_accepts_multi_generic_title_token_overlap` — lock `len(ov)≥2` exception

- [ ] **Step 1: Write failing tests**

```python
def test_primary_fill_is_strong_rejects_single_generic_title_token() -> None:
    """0018: N/record 模块 ∩ Custom Record… → {record} must not be primary-strong."""
    hit = PassageHit(
        passage_id="p-n-record",
        page_path="wiki/concepts/netsuite-ss-modules/n-record.md",
        title="N/record 模块",
        heading_path=(),
        text="unrelated body noise only",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    q = "Custom Record Type Object Custom Fields"
    assert not primary_fill_is_strong(q, [{"hit": hit}])
    # relaxed path unchanged: single title-token overlap still strong
    assert relaxed_fill_is_strong(q, [{"hit": hit}])


def test_primary_fill_is_strong_accepts_multi_generic_title_token_overlap() -> None:
    """All-generic overlap with |ov|≥2 stays primary-strong (approved exception)."""
    hit = PassageHit(
        passage_id="p-record-type",
        page_path="wiki/concepts/record-type.md",
        title="Record Type helpers",
        heading_path=(),
        text="unrelated body noise only",
        score=1.0,
        corpus="active",
        authority="",
        source_kind="wiki",
        ordinal=0,
    )
    # tokens(title) ⊇ {record, type, helpers}; ∩ query → {record, type} (|ov|=2)
    q = "Custom Record Type Object Custom Fields"
    assert primary_fill_is_strong(q, [{"hit": hit}])
```

Also **re-assert** (inline in the new test file section or as comments pointing at existing tests — do not delete):

- SuiteScript single non-generic token → primary True (`test_primary_fill_is_strong_uses_qualified_hits_and_relaxed_signals`)
- Location single token → relaxed True
- knife1 body-only phrase → primary False

Optional tiny helper test (only if you extract a pure function in Task 2):

```python
def test_title_token_overlap_strong_formula() -> None:
    # if _title_token_overlap_is_strong(ov, *, body_evidence=) is extracted
    assert not _title_token_overlap_is_strong({"record"}, body_evidence=False)
    assert _title_token_overlap_is_strong({"record", "type"}, body_evidence=False)
    assert _title_token_overlap_is_strong({"suitescript"}, body_evidence=False)
    assert _title_token_overlap_is_strong({"record"}, body_evidence=True)
```

- [ ] **Step 2: Run tests — expect FAIL**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
.\.venv\Scripts\python.exe -m pytest `
  tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_rejects_single_generic_title_token `
  tests/retrieval/test_query_recall_policy.py::test_primary_fill_is_strong_accepts_multi_generic_title_token_overlap `
  -v
```

Expected: **FAIL** — today `{record}` still returns True from primary (any non-empty intersection).

- [ ] **Step 3: Commit tests only** (optional local checkpoint; or fold into Task 2 commit if preferred)

```powershell
git add tests/retrieval/test_query_recall_policy.py
git commit -m "test(retrieval): primary rejects single generic title-token (0018)"
```

---

### Task 2: Implement GENERIC gate in title-token + ED-1 arms

**Files:**
- Modify: `src/retrieval/query_recall_policy.py`

**Interfaces:**

| Symbol | Spec |
|--------|------|
| `GENERIC_TITLE_TOKENS: frozenset[str]` | Module-level constant near other policy constants (~L37+); English lowercase, aligned with `tokens()` casefold |
| Exact title-token arm | When `body_evidence=False`: `ov = title_tokens & evidence_tokens`; `content = ov - GENERIC_TITLE_TOKENS`; strong iff `len(content) ≥ 1 or len(ov) ≥ 2`. When `True`: any non-empty `ov` → strong (unchanged) |
| ED-1 arm | When `body_evidence=True`: immediate True on first ED-1 hit (unchanged). When `False`: do **not** return True on a lone generic fuzzy hit; accumulate ED-1 matched title tokens into `ed_hits`, then evaluate `(ov \| ed_hits)` with the **same** formula (shared helper preferred) |
| Suggested helper (optional but recommended) | `_title_token_overlap_is_strong(ov: set[str], *, body_evidence: bool) -> bool` — single place for the formula so exact / ED-1 cannot drift |

**Formula (copy into code comment if helpful):**

```text
ov      = title_tokens ∩ evidence_tokens
content = ov − GENERIC_TITLE_TOKENS
strong  iff  len(content) ≥ 1  OR  len(ov) ≥ 2   # primary only
```

- [ ] **Step 1: Minimal implementation**

Add constant:

```python
GENERIC_TITLE_TOKENS: frozenset[str] = frozenset(
    {
        "record",
        "type",
        "object",
        "field",
        "fields",
        "module",
        "guide",
        "overview",
        "helper",
        "helpers",
        "api",
        "doc",
        "docs",
    }
)
```

Replace title-token block (~L227–241) with logic equivalent to:

```python
title_tokens = {tok.casefold() for tok in tokens(title) if tok}
ov = title_tokens & evidence_tokens
if _title_token_overlap_is_strong(ov, body_evidence=body_evidence):
    return True
ed_hits: set[str] = set()
for title_tok in title_tokens:
    if len(title_tok) < 4:
        continue
    for term in question_tokens:
        if (
            term != title_tok
            and len(term) >= 4
            and abs(len(term) - len(title_tok)) <= 1
            and edit_distance(term, title_tok) <= 1
        ):
            if body_evidence:
                return True
            ed_hits.add(title_tok)
if ed_hits and _title_token_overlap_is_strong(
    ov | ed_hits, body_evidence=False
):
    return True
```

Helper:

```python
def _title_token_overlap_is_strong(
    ov: set[str], *, body_evidence: bool
) -> bool:
    if not ov:
        return False
    if body_evidence:
        return True
    content = ov - GENERIC_TITLE_TOKENS
    return len(content) >= 1 or len(ov) >= 2
```

Do **not** change `primary_fill_is_strong` call signature or `query_execution_context` callers.

- [ ] **Step 2: Run new + regression tests — expect PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest `
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
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "relaxed_fill_is_strong or primary_fill_is_strong or weak_primary or body_only" -v
```

Expected: **PASS**. SuiteScript still True (`suitescript` ∉ GENERIC → `len(content)≥1`). Location relaxed still True. `{record}` primary False. `{record,type}` primary True.

- [ ] **Step 3: Commit**

```powershell
git add src/retrieval/query_recall_policy.py tests/retrieval/test_query_recall_policy.py
git commit -m "fix(retrieval): primary title-token denylist bar (0018)"
```

---

### Task 3: CHANGELOG + focused pytest

**Files:**
- Modify: `CHANGELOG.md`
- (optional) tiny docstring on `relaxed_fill_is_strong` noting primary title-token denylist — only if needed for clarity

- [ ] **Step 1: CHANGELOG** — new dated section at top (match Chinese P2 style):

```markdown
## 2026-10-20 — 检索 P2：primary title-token 泛化 denylist（0018）

- primary「强」的 title-token / ED-1 臂：去掉泛化词后至少 1 个实词，或重叠 ≥2 个 token（含全泛化多词例外）；单点 `{record}` 不再挡住 `raw_zero`。
- **仅** `body_evidence=False`（primary）；relaxed 任意 title-token 重叠仍强。
- 名单不全时加词、不改公式；不动 coverage / qualified 短路 / 刀1 body 门。
```

- [ ] **Step 2: Re-run focused pytest**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/retrieval/test_query_recall_policy.py -k "relaxed_fill_is_strong or primary_fill_is_strong or weak_primary or body_only" -q --tb=line
```

Expected: PASS.

- [ ] **Step 3: Commit changelog**

```powershell
git add CHANGELOG.md
git commit -m "docs: changelog for primary title-token denylist bar"
```

---

### Task 4: Gold eval → `reports-p2-title-token-bar`

**Output:** `.eval/gold-sample-2026-10-19/reports-p2-title-token-bar/` (local artifact; do **not** force-add unless asked)

- [ ] **Step 1: Run eval**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
uv run llm-wiki-mcp retrieval-eval `
  --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" `
  --dataset .eval/gold-sample-2026-10-19/gold.jsonl `
  --manifest .eval/gold-sample-2026-10-19/gold.manifest.json `
  --output-dir .eval/gold-sample-2026-10-19/reports-p2-title-token-bar
```

(If `uv run` is unavailable on LU-C, use the same `.\.venv\Scripts\python.exe -m llm_wiki_mcp ...` form used in prior P0/P1/P2-c notes.)

- [ ] **Step 2: Check 0018 only as this knife’s gate**

| Check | Expectation |
|-------|-------------|
| codingwork-0018 | Should schedule `raw_zero` / weak primary path (no longer `fallback:none` solely because title-token `{record}` marked strong). Prefer seeing raw Help Doc path or at least `wiki_weak_primary` + raw probe |
| `filter_correctness` | Must not regress vs `reports-p2-c` |
| **0008 / 0015** | **Do not require** hit@10 improvement; note neutrally if unchanged |

If 0018 still misses for a *different* reason (empty raw, merge rank, etc.), write one honest residual line — do **not** expand GENERIC formula or reopen 0008/0015 in this knife.

- [ ] **Step 3: Do not push** unless the user asks. No PR unless asked.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Primary-only (`body_evidence=False`) | Task 2 |
| Formula `len(content)≥1 OR len(ov)≥2` | Task 2 helper |
| `{record}` weak / `{record,type}` strong / `{suitescript}` strong | Task 1 tests + Task 2 |
| ED-1 same filter | Task 2 (`ed_hits` + shared helper) |
| GENERIC initial list | Task 2 constant |
| Relaxed unchanged | Task 1 assert + Task 2 `body_evidence=True` branch |
| No coverage / qualified / knife1 body / aliases / vectors | Global constraints |
| Eval `reports-p2-title-token-bar`; 0018 gate; not 0008/0015 | Task 4 |
| TDD failing tests first | Task 1 before Task 2 |
| CHANGELOG | Task 3 |
| No push/PR unless asked | Global + Task 4 |

Placeholder scan: none. Types: `frozenset[str]`, `body_evidence: bool`, formula locked above.

---

## Execution handoff

Plan complete when saved to `docs/plans/2026-10-20-retrieval-p2-title-token-bar.md` and committed (Task 0).

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — this session with executing-plans checkpoints  

Which approach?

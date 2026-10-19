# Retrieval P0 Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD. Checkbox (`- [ ]`) tracking.  
> **Spec:** `docs/specs/2026-10-19-retrieval-p0-project-qualified-abstain-design.md`  
> **Branch:** `v0.9.36` @ `C:\Users\26327\VSCodeProjects\llm-wiki-mcp`  
> **MachineId:** `95e422f3-b1c1-4770-a235-b777be61cebe`

**Goal:** Land Approach A only — (1) stop path-inferred `project` on `raw/sources/file/...`, (2) enable `qualified_code` on active/auto/knowledge/all stage-one, (3) true abstain when only **weak** `wiki_relaxed` fills results — then reindex codingwork raw and re-eval the frozen gold set.

**Architecture:** Keep existing FTS ladders and `plan_fallback` matrix. Change projection (`page_from_file`), widen stage-one ladder gate in `_stage_one_fts_hits`, and gate `wiki_relaxed_answered` in `QueryExecutionContext` so weak noise clears selection and allows `raw_zero`. No vector path, no new aliases, no raw-ladder widening.

**Tech Stack:** Python 3, pytest, SQLite FTS5 (`RetrievalIndexStore`), `uv run`, existing `run_query_v2` / gold eval CLI.

## Global Constraints

- No vector DB / hybrid / embedding work
- TDD: failing test → fail → implement → pass → commit (per task)
- No absolute BM25 score threshold for abstain (branch-relative / signal-based only)
- Do not change MCP public field names (empty `results` list is allowed)
- PowerShell-friendly commands; `git add -f` for `docs/*`; **do not push** unless asked
- Out of scope: raw ladder widen, `Nui server widget` aliases, gold re-labeling

## File map

| File | Role |
|------|------|
| `src/retrieval/retrieval_index.py` | Change 1: `page_from_file` skip `setdefault(project)` for `file/` |
| `src/retrieval/query_pipeline.py` | Change 2: `_stage_one_fts_hits` qualified on active family |
| `src/retrieval/query_execution_context.py` | Change 3: strong vs weak before `wiki_relaxed_answered=True` |
| `src/retrieval/query_recall_policy.py` (or small helper next to recovery) | Change 3: `relaxed_fill_is_strong(question, items)` |
| `src/retrieval/query_recovery.py` | Only if `FallbackState` / docs need a one-line comment; **prefer not** to change `plan_fallback` matrix |
| `tests/retrieval/test_retrieval_index.py` | Project projection cases |
| `tests/retrieval/test_query_pipeline.py` | Active/knowledge Chinese `N/module` qualified path |
| `tests/retrieval/test_query_recall_policy.py` | Strong blocks raw (keep); weak abstain + raw_zero |
| `CHANGELOG.md` | User-visible note |
| `docs/specs/2026-10-19-retrieval-p0-project-qualified-abstain-design.md` | Spec (already committed) |
| `docs/plans/2026-10-19-retrieval-p0-project-qualified-abstain.md` | This plan |

### Current anchors (do not invent)

`page_from_file` today (`retrieval_index.py`):

```python
elif is_raw:
    parts = rel.split("/")
    # Ingest snapshots use raw/sources/<type>/<project>/<name>/... .
    if len(parts) >= 5:
        frontmatter.setdefault("project", parts[3])
    frontmatter.setdefault("type", "raw")
```

`_stage_one_fts_hits` today (`query_pipeline.py`):

```python
steps: list[LadderStep] = [LadderStep("strict", "strict", question, common_kwargs)]
if effective_scope == "raw" and has_qualified_identifier(question):
    steps.append(LadderStep("qualified_code", "qualified_code", question, common_kwargs))
```

`plan_fallback` wiki_relaxed vs raw_zero (`query_recovery.py`):

```python
if (not state.has_primary_recall and state.effective_scope in {"knowledge", "all"}
    and state.relaxed_available):
    return FallbackPlan("wiki_relaxed", ...)
if (not state.has_primary_recall and not state.wiki_relaxed_answered
    and state.effective_scope in {"knowledge", "all"}
    and state.raw_available in {"unknown", "yes"}):
    return FallbackPlan("raw_zero", ...)
```

Relaxed commit point today (`query_execution_context.py` ~557): after `wiki_relaxed_items` non-empty → set `self.selected`, then **`wiki_relaxed_answered = True`** unconditionally — this is what blocks `raw_zero` and causes unanswered FP on weak OR noise.

FTS project SQL (unchanged by this plan): `(page_project = '' OR page_project = ?)`.

---

### Task 0: Docs (this plan)

- [ ] Confirm spec already on branch (`c2f2134` or successor)
- [ ] Write this plan under `docs/plans/`; `git add -f`; commit docs only

---

### Task 1: `raw/sources/file` — no path-inferred project

**Files:**
- Modify: `src/retrieval/retrieval_index.py`
- Test: `tests/retrieval/test_retrieval_index.py`

**Interfaces / behavior:**

| Path | Expected `pages.project` |
|------|--------------------------|
| `raw/sources/file/NetSuite Help Docs/.../x.md` | `""` |
| `raw/sources/projects/demo/...` | `"demo"` (still inferred) |
| `raw/sources/chat/...` | `"unknown"` |
| file page with YAML `project: codingwork` | `"codingwork"` (frontmatter wins) |

**Steps:**

1. **Write failing tests** in `test_retrieval_index.py` (mirror chat test style):

```python
def test_raw_file_tree_does_not_infer_project_from_path_segment(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(
        root,
        "raw/sources/file/NetSuite Help Docs/01_SuiteCloud/NuiserverWidget Module.md",
        "# N/ui/serverWidget\n\nForm Field API",
    )
    store = RetrievalIndexStore(root, scope="raw")
    store.build(store.iter_vault_pages())
    page = next(p for p in store.page_candidates() if "NuiserverWidget" in p["path"])
    assert page["project"] == ""


def test_raw_projects_snapshot_still_infers_project(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "raw/sources/projects/demo/notes/a.md", "# Demo\n\nbody")
    store = RetrievalIndexStore(root, scope="raw")
    store.build(store.iter_vault_pages())
    assert store.page_candidates()[0]["project"] == "demo"


def test_raw_file_frontmatter_project_is_respected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(
        root,
        "raw/sources/file/NetSuite Help Docs/x.md",
        "---\nproject: codingwork\n---\n\n# X\n\nbody",
    )
    store = RetrievalIndexStore(root, scope="raw")
    store.build(store.iter_vault_pages())
    assert store.page_candidates()[0]["project"] == "codingwork"
```

Also add integration-style assert (can live in `test_query_recall_policy.py` or pipeline): raw scope + `project="codingwork"` **hits** a Help Docs file page (empty project passes SQL). Keep existing `test_v2_raw_fallback_respects_project_and_type_filters` green (projects snapshot still filtered).

2. **Run — expect fail:**

```powershell
cd C:\Users\26327\VSCodeProjects\llm-wiki-mcp
uv run pytest tests/retrieval/test_retrieval_index.py -k "raw_file_tree or raw_projects_snapshot or raw_file_frontmatter" -q
```

3. **Implement** in `page_from_file`:

```python
elif is_raw:
    parts = rel.split("/")
    # Vendor file trees (raw/sources/file/...) are shared docs — do not
    # treat the next path segment (e.g. "NetSuite Help Docs") as project.
    # Ingest snapshots keep raw/sources/<type>/<project>/... inference.
    if len(parts) >= 5 and parts[2].casefold() != "file":
        frontmatter.setdefault("project", parts[3])
    frontmatter.setdefault("type", "raw")
```

(`parts[0]=raw`, `[1]=sources`, `[2]=type`.) Chat branch unchanged above this `elif`.

4. **Run — expect pass** (same pytest `-k`). Manually sanity: existing chat project test still passes.

```powershell
uv run pytest tests/retrieval/test_retrieval_index.py -q
```

5. **Commit:**

```text
fix(retrieval): do not infer project for raw/sources/file trees
```

---

### Task 2: `qualified_code` on active/auto/knowledge/all stage-one

**Files:**
- Modify: `src/retrieval/query_pipeline.py` (`_stage_one_fts_hits`)
- Test: `tests/retrieval/test_query_pipeline.py` (mirror `test_v2_raw_scope_uses_qualified_aliases_on_the_primary_path`)

**Interfaces:**

- Input: `effective_scope` already resolved by pipeline (`auto` may remain `"auto"` or map to active store — use membership check covering `"raw", "auto", "knowledge", "all"`).
- Do **not** enable for `"history"` / `"archive"` in this task.
- Counters: `qualified_fts_hits` already returned from `_stage_one_fts_hits`; assert `> 0` and target wiki path in `results`.

**Steps:**

1. **Write failing test:**

```python
def test_v2_active_scope_uses_qualified_code_for_chinese_module_question(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    _write(
        root,
        "wiki/concepts/netsuite-ss-modules/n-record.md",
        "N/record",
        "N/record module methods: create load submitFields.",
        type="concept",
    )
    _write(
        root,
        "wiki/concepts/noise-methods.md",
        "方法列表",
        "各种方法 模块 有哪些 列表 noise only chinese bigrams.",
        type="concept",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root,
        "n/record模块有哪些方法",
        scope="knowledge",  # also parametrize auto if cheap
        retrieval_mode="lexical",
    )
    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/netsuite-ss-modules/n-record.md" in paths
    assert result["pipeline"]["counters"]["qualified_fts_hits"] > 0
    # Prefer module page near top (strict CJK AND alone often misses)
    assert paths[0] == "wiki/concepts/netsuite-ss-modules/n-record.md"
```

Reuse local `_write` / `create_wiki_root` / `refresh_indexes` already imported in that file (same as raw qualified test ~L1348).

2. **Run — expect fail** (today only `strict` on knowledge → CJK AND miss → relaxed noise may win or miss qualified counter):

```powershell
uv run pytest tests/retrieval/test_query_pipeline.py -k "active_scope_uses_qualified_code" -q
```

3. **Implement:**

```python
_QUALIFIED_STAGE_ONE_SCOPES = frozenset({"raw", "auto", "knowledge", "all"})

# in _stage_one_fts_hits:
steps: list[LadderStep] = [LadderStep("strict", "strict", question, common_kwargs)]
if (
    has_qualified_identifier(question)
    and effective_scope in _QUALIFIED_STAGE_ONE_SCOPES
):
    steps.append(LadderStep("qualified_code", "qualified_code", question, common_kwargs))
```

Keep `merge="merge_by_passage"`, `limit=50`. Do not change `lexical_analyzer`.

4. **Run — expect pass** + existing raw qualified alias test:

```powershell
uv run pytest tests/retrieval/test_query_pipeline.py -k "qualified" -q
```

5. **Commit:**

```text
fix(retrieval): enable qualified_code on active stage-one scopes
```

---

### Task 3: Weak `wiki_relaxed` true abstain

**Files:**
- Modify: `src/retrieval/query_execution_context.py` (gate at ~`if wiki_relaxed_items:`)
- Add helper: prefer `src/retrieval/query_recall_policy.py` as `relaxed_fill_is_strong(question, items) -> bool` (keeps policy owner for recall heuristics)
- Optional: thin wrapper used by quality features — **reuse signal definitions**, do not call absolute score cuts
- Test: `tests/retrieval/test_query_recall_policy.py`

**Strong vs weak (frozen in spec):**

- **Strong:** ≥1 selected page has identifier / phrase / exact / title-overlap signal  
  (align with `CandidateFeature.identifier_signal|phrase_signal|exact_signal|title_signal`, or equivalent checks: `has_qualified_identifier` alias in title/path/body; `identifier_phrases(question)` hit; title token overlap; exact question substring in title/path).
- **Weak:** none of the above — pure OR-token / CJK bigram relaxed fill.

**Behavior matrix:**

| Case | `results` | `wiki_relaxed_answered` | `raw_zero` |
|------|-----------|-------------------------|------------|
| Strong relaxed | non-empty | `True` | blocked (existing test) |
| Weak relaxed only | `[]` | `False` | allowed if scope∈{knowledge,all} & raw available |
| Strict/qualified primary | non-empty | n/a | n/a |

**Do not change** the pure `plan_fallback` branch order unless tests prove `relaxed_available` plumbing needs a tweak; preferred fix is: **never set `wiki_relaxed_answered=True` / never keep `self.selected` on weak fills**.

**Steps:**

1. **Write / adjust tests:**

- Keep **`test_v2_wiki_relaxed_answer_blocks_raw_fallback`** as the **strong** anchor (today it expects wiki path + `fallback.level == "none"` + `raw_fts_hits == 0`). If after helper it flips to weak, **strengthen the fixture** (e.g. ensure title/phrase overlap on `noisy-location.md`) so it remains strong — do not delete the “blocks raw” guarantee.

- **New weak abstain test:**

```python
def test_v2_weak_wiki_relaxed_abstains_and_allows_raw_zero(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Wiki pages share only loose OR tokens with the query — no identifier/title hit
    _write(root, "wiki/concepts/noise-a.md", "杂谈", "治理 单位 列表 摩擦 owner 模块 深度", type="concept")
    _write(root, "wiki/concepts/noise-b.md", "随笔", "架构 剩余 技术债 思考 guide", type="concept")
    raw = root / "raw/sources/references/architecture-debt.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "# 架构深化 技术债\n\nowner 模块 剩余摩擦 权威说明。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    # Query with no N/… and no multiword English identifier phrase;
    # strict misses; relaxed would only OR-match noise; raw has topical doc.
    result = run_query_v2(
        root,
        "架构深化 技术债 剩余摩擦 owner 模块",
        scope="knowledge",
        retrieval_mode="lexical",
    )
    # After weak abstain, either empty (if raw unavailable in fixture path)
    # or raw_zero filled — never the noise-only wiki pages as a fake answer.
    paths = [item["path"] for item in result["results"]]
    assert "wiki/concepts/noise-a.md" not in paths
    assert "wiki/concepts/noise-b.md" not in paths
    assert result["pipeline"]["counters"]["fts_hits"] == 0
    # If raw store built by refresh_indexes: expect raw hit + fallback raw
    if paths:
        assert paths[0].startswith("raw/")
        assert result["pipeline"]["fallback"]["level"] == "raw"
```

Tune fixture until it reliably classifies as weak under the helper (no `N/`, no English multiword run). Add a unit test on `relaxed_fill_is_strong` with synthetic items if faster.

2. **Run — expect fail** on new test (today returns wiki noise and blocks raw):

```powershell
uv run pytest tests/retrieval/test_query_recall_policy.py -k "wiki_relaxed_answer_blocks or weak_wiki_relaxed" -q
```

3. **Implement helper** (sketch — match real `PassageHit` fields):

```python
def relaxed_fill_is_strong(question: str, items: Sequence[Mapping[str, Any]]) -> bool:
    """True when wiki_relaxed fill has identifier/phrase/exact/title evidence."""
    if not items:
        return False
    q = question.casefold()
    phrases = [p.casefold() for p in identifier_phrases(question)]
    qualified = extract_qualified_identifiers(question)  # from lexical_analyzer
    for item in items:
        hit = item["hit"] if "hit" in item else item
        title = str(getattr(hit, "title", "") or "").casefold()
        path = str(getattr(hit, "page_path", "") or "").casefold()
        body = str(getattr(hit, "text", "") or "").casefold()
        blob = f"{title}\n{path}\n{body}"
        if title and (title in q or q in title):
            return True
        if any(p and p in blob for p in phrases):
            return True
        for ident in qualified:
            if any(alias.casefold() in blob for alias in ident.aliases):
                return True
        if len(q.strip()) >= 4 and q.strip() in {title, path}:
            return True
    return False
```

Wire in `query_execution_context.py`:

```python
if wiki_relaxed_items:
    ...  # existing score / graph / sort / adaptive_expand
    strong = recall_policy.relaxed_fill_is_strong(
        request_view.question, self.selected
    )
    if not strong:
        self.selected = []
        self.context_items = []
        # leave wiki_relaxed_answered False → raw_zero may run
        # optionally keep relaxed_fts_hits for diagnostics
    else:
        self.recovery = assemble_recovery(...)
        ...
        wiki_relaxed_answered = True
```

No absolute score constant. Do not enable quality-gate enforce as a substitute.

4. **Run — expect pass** for both strong-block and weak-abstain tests + nearby fallback suite:

```powershell
uv run pytest tests/retrieval/test_query_recall_policy.py tests/retrieval/test_query_recovery.py -q
```

5. **Commit:**

```text
fix(retrieval): abstain on weak wiki_relaxed-only fills
```

---

### Task 4: CHANGELOG, codingwork reindex, gold eval

**Files:**
- Modify: `CHANGELOG.md` (top section dated 2026-10-19 or new subsection)
- No gold edits

**Steps:**

1. **CHANGELOG** bullet (Chinese ok), cover three behaviors + “需重建 raw 索引”.

2. **Reindex codingwork raw** (user vault):

```powershell
uv run llm-wiki-mcp index build --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" --scope raw
uv run llm-wiki-mcp index status --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" --scope raw
```

Confirm `ok` / `fresh`. Active rebuild optional (Change 2 is query-time only).

3. **Re-run eval** against frozen gold:

```powershell
uv run llm-wiki-mcp retrieval-eval `
  --vault "C:\Users\26327\Documents\Obsidian Vault\codingwork" `
  --dataset "C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.jsonl" `
  --manifest "C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\gold.manifest.json" `
  --output-dir "C:\Users\26327\VSCodeProjects\llm-wiki-mcp\.eval\gold-sample-2026-10-19\reports-p0"
```

4. **Compare** to baseline `...\reports\retrieval-eval.json`:
   - Recall@10 macro ↑ (expect help on 0024–0029, 0004/0022)
   - `no_answer_false_positive_rate` ↓ (0003/0011)
   - `filter_correctness` stays `1.0`

5. **Full targeted regression:**

```powershell
uv run pytest tests/retrieval/test_retrieval_index.py tests/retrieval/test_query_pipeline.py tests/retrieval/test_query_recall_policy.py tests/retrieval/test_query_recovery.py -q
```

6. **Commit** changelog (and any plan checkbox updates if desired):

```text
docs: changelog for retrieval P0 project/qualified/abstain
```

Report metrics + commit hashes to parent; **do not push** unless asked.

---

## Self-review vs spec

| Spec requirement | Plan coverage |
|------------------|---------------|
| Change 1 only `file/` exempt; chat / wiki/projects / other raw types / frontmatter | Task 1 table + implementation snippet |
| Change 2 scopes raw+auto+knowledge+all; not history/archive mandate | Task 2 frozenset |
| Change 3 empty results; no absolute score; strong still blocks raw; weak allows raw_zero | Task 3 matrix + helper + context wire |
| Out of scope: raw ladder widen, Nui aliases, vector | Global Constraints |
| Success: raw reindex + same gold.jsonl eval | Task 4 |
| TDD + commits per bite | Tasks 1–3 steps |
| Files listed in spec | File map matches |

No TBD left: weak/strong signal set and wire point (`wiki_relaxed_answered`) are fixed. If `refresh_indexes` in tests does not build raw store, Task 3 fixture must call the same helper the existing raw_fallback tests use (copy from `test_v2_raw_fallback_uses_raw_documents_after_formal_miss`).

## Execution handoff

Start at **Task 1**. After Task 3 green, run Task 4 eval before calling the work done.

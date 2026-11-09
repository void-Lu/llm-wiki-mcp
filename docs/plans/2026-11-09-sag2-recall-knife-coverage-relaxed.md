# SAG2 Recall Knife (Coverage Raw Merge + CJK/Empty wiki_relaxed) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

I'm using the writing-plans skill to create the implementation plan.

**Goal:** On branch `v1.0.0`, raise codingwork gold R@10 from ~0.643 toward **≥ 0.75** by (1) merging one raw FTS when the wiki+hop pool leaves Latin/identifier query tokens uncovered, and (2) firing one bounded `wiki_relaxed` FTS when the pool is empty/near-empty **and** the query contains CJK — without restoring the full P2 coverage→relaxed→raw_zero ladder.

**Architecture:** Keep the v1.0.0 sag2_hop default path and first-knife weak-merge / empty-replace semantics. Extend the non-legacy branch of `QueryExecutionContext._run_fallback_recovery` into a small ordered orchestrator: **coverage → raw merge** (reuse `uncovered_latin_terms` + `merge_coverage_items`) → **empty/weak+CJK → single `wiki_relaxed`** (reuse `relaxed_recovery_items`, bound top-N) → **still-empty → raw replace** / **still-weak → raw merge** (existing `_run_raw_single_shot`, skipped when coverage already spent the one raw). Pure planners live in `query_recovery.py`; telemetry accumulates `recovery_reasons` under `pipeline.sag2_hop`.

**Tech Stack:** Python 3.11+, pytest, SQLite FTS5, existing `PassageHit` / `candidate_item` / `merge_coverage_items` / `merge_weak_primary_raw` / `relaxed_recovery_items` / MCP `wiki_query` envelope. Test with `uv run pytest` (`pythonpath=src`). Gold eval via `uv run llm-wiki-mcp retrieval-eval` against `/tmp/sag2-eval/…` when present.

## Global Constraints

- Spec (source of truth): `docs/specs/2026-11-09-sag2-recall-knife-coverage-relaxed-design.md` (commit `ca55db5` on `v1.0.0`).
- Prior path: hop-default + recall-lift (Approach A) already on branch; first-knife weak merge + Judge bonuses stay.
- Package version remains **1.0.0** (no bump unless user asks).
- **Knife 1+2 only.** Do **not** restore default P2 ladder (`enable_legacy_p2_ladder` stays `False`). Do **not** implement knife 3 (graph/bonus jitter) in this plan.
- **No vector DB** requirement; vectors remain opt-in/hybrid as today.
- **Local-only EvidenceJudge**; no vault egress / hosted JEV for judging.
- Knife 1: uncovered Latin/identifier in query → **one** raw FTS → **MERGE** (not replace). Prefer `merge_coverage_items` so only raw that covers uncovered terms is admitted.
- Knife 2: wiki+hop **empty or weak** (`wiki_hop_pool_kind != "strong"`) **AND** query has CJK → **one** bounded `wiki_relaxed` FTS; if still empty use existing empty→raw **replace**.
- Near-empty lock: `wiki_hop_pool_kind(...) == "weak"` (top evidence `< 0.15`), same threshold as first knife.
- Dedup with first-knife weak-pool raw merge: **at most one** raw FTS on the default path per query; if coverage already ran raw, skip the weak/empty raw second shot (unless coverage never ran and pool is empty/weak).
- Dedup identity: existing `passage_id` / `page_path` via `merge_coverage_items` / `merge_weak_primary_raw` / `select_best_per_page` — do not invent a new key.
- After merge/relaxed: sort/select with existing fused scores + `adaptive_expand` + `top_k` (reuse P2 / first-knife ordering; no new tie-break invention beyond what those helpers already do).
- Telemetry reasons (accumulate under `pipeline.sag2_hop.recovery_reasons`): `coverage_raw_merge`, `wiki_relaxed_cjk`, `wiki_relaxed_empty`, plus existing weak/empty via `raw_fallback_mode` ∈ {`none`,`raw_merge`,`raw_replace`} (and `raw_single_shot` True when either merge or replace fired).
- Bound for knife 2: `WIKI_RELAXED_SINGLE_SHOT_LIMIT = 40` (candidate search + retained top-N after `select_best_per_page`).
- MCP public field names stay stable; only add fields under `pipeline.sag2_hop` / keep `pipeline.coverage` readable (`triggered` may become True when knife 1 fires).
- TDD each task: failing test → fail → implement → pass → commit.
- `docs/*` is gitignored → `git add -f` for plans/specs/CHANGELOG as needed.
- **Do not push** unless the user asks.
- **Eval gate:** same-protocol codingwork gold **R@10 ≥ 0.75**. If unmet, record baseline / measured / delta **loudly** — never silently claim success. Baseline after first knife ≈ **0.6428** (29 cases); hop-default ≈ **0.6655**; P2 ladder reference ≈ **0.8333**.

## File map

| File | Role |
|------|------|
| `src/retrieval/query_recovery.py` | **Modify.** Add `query_has_cjk`, `plan_coverage_raw_merge`, `plan_wiki_relaxed_single_shot` pure helpers; keep `wiki_hop_pool_kind` / `plan_raw_single_shot`. |
| `src/retrieval/query_recall_policy.py` | **Modify (minimal).** Add `WIKI_RELAXED_SINGLE_SHOT_LIMIT = 40`; optional `limit` on `RelaxedRecoveryRequestView` / `relaxed_recovery_items` (default stays `IDENTIFIER_PHRASE_CANDIDATES`). Reuse `uncovered_latin_terms`, `merge_coverage_items`, `merge_weak_primary_raw`, `relaxed_recovery_items`. Export new constant in `__all__`. |
| `src/retrieval/query_execution_context.py` | **Modify.** Non-legacy path: orchestrate coverage→raw merge, then CJK/empty wiki_relaxed, then existing empty/weak raw (deduped). Thread `sag2_recovery_reasons: list[str]` (+ freeze on execution view). Set `coverage_fallback` when knife 1 admits raw. |
| `src/retrieval/query_pipeline.py` | **Modify.** Project `recovery_reasons` under `pipeline["sag2_hop"]`. |
| `tests/retrieval/test_query_recovery.py` | **Modify.** Unit tests for `query_has_cjk` / planners. |
| `tests/retrieval/test_query_recall_policy.py` | **Modify (minimal).** Cover optional `limit` on relaxed recovery if added; leave existing `uncovered_latin_terms` tests intact. |
| `tests/retrieval/test_sag2_pipeline_default.py` | **Modify.** Integration: coverage raw merge keeps wiki; CJK empty → one relaxed; default path never runs full ladder; telemetry reasons. |
| `tests/retrieval/test_query_import_contract.py` | **Modify.** Export `WIKI_RELAXED_SINGLE_SHOT_LIMIT` if added to `__all__`. |
| `CHANGELOG.md` | **Modify.** Chinese behavior note under v1.0.0. |
| `docs/specs/2026-11-09-sag2-recall-knife-coverage-relaxed-design.md` | Spec (read-only during impl). |
| `docs/plans/2026-11-09-sag2-recall-knife-coverage-relaxed.md` | This plan. |

### Current anchors (do not invent)

Non-legacy fallback today (`query_execution_context.py`):

```python
if not self.enable_legacy_p2_ladder:
    self._run_raw_single_shot(request_view)
    return
```

`_run_raw_single_shot` today: empty → `raw_replace`; weak → `merge_weak_primary_raw` → `raw_merge`; strong → no-op. Sets `sag2_raw_fallback_mode` / `sag2_raw_single_shot`.

Coverage detector already exists (`query_recall_policy.uncovered_latin_terms`): Latin `[a-z0-9_]+` tokens with `len >= 3` absent from selected title+text (identifiers like `run_query_v2` / casefolded CamelCase are included via `tokens()`).

Legacy coverage merge helper: `merge_coverage_items(active, raw, uncovered_terms)` — filters raw that cover uncovered terms, RRF-fuses, keeps wiki.

Legacy relaxed helper: `relaxed_recovery_items(store, RelaxedRecoveryRequestView(...))` currently searches with `mode="relaxed"` and `limit=IDENTIFIER_PHRASE_CANDIDATES` (200).

Telemetry today (`query_pipeline.py`): `pipeline.sag2_hop` has `raw_single_shot` + `raw_fallback_mode`; `pipeline.coverage.triggered` mirrors `coverage_fallback`.

---

### Task 1: Pure planners — CJK gate, coverage trigger, wiki_relaxed trigger

**Files:**
- Modify: `src/retrieval/query_recovery.py`
- Modify: `src/retrieval/query_recall_policy.py` (constant + optional relaxed `limit`)
- Test: `tests/retrieval/test_query_recovery.py`
- Test: `tests/retrieval/test_query_import_contract.py` (export list)
- Optional: `tests/retrieval/test_query_recall_policy.py` (limit slicing)

**Interfaces:**
- Consumes: `wiki_hop_pool_kind`, `uncovered_latin_terms` (callers compute uncovered; planners are pure).
- Produces:
  ```python
  def query_has_cjk(question: str) -> bool: ...

  def plan_coverage_raw_merge(
      *,
      uncovered_latin_terms: Sequence[str],
      selected_nonempty: bool,
      effective_scope: str,
  ) -> bool:
      """True when knife 1 should fire one raw → merge."""

  def plan_wiki_relaxed_single_shot(
      *,
      pool_kind: WikiHopPoolKind,
      question: str,
      effective_scope: str,
  ) -> Literal["wiki_relaxed_empty", "wiki_relaxed_cjk"] | None:
      """Reason code when knife 2 should fire; None to skip."""
  ```
  - `WIKI_RELAXED_SINGLE_SHOT_LIMIT = 40` in `query_recall_policy.py`
  - `RelaxedRecoveryRequestView.limit: int | None = None` — when set, `relaxed_recovery_items` uses it as FTS `limit`; else `IDENTIFIER_PHRASE_CANDIDATES`.

**Lock decisions (from spec + code):**
- CJK: any Han / kana / fullwidth char — same class as `query_quality_policy._CJK_RE`: `[\u3400-\u9fff\u3040-\u30ff\uff00-\uffef]`.
- Knife 1 requires **non-empty** selected (primary present) + nonempty uncovered + scope ∈ {`knowledge`,`all`}. Empty pool never uses coverage merge (would treat all Latin as uncovered and collide with empty→raw replace).
- Knife 2 requires CJK **and** `pool_kind in {"empty","weak"}` + scope ∈ {`knowledge`,`all`}. Reason: `wiki_relaxed_empty` if empty, else `wiki_relaxed_cjk`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/retrieval/test_query_recovery.py
from retrieval.query_recovery import (
    plan_coverage_raw_merge,
    plan_wiki_relaxed_single_shot,
    query_has_cjk,
)


def test_query_has_cjk_detects_han_and_rejects_latin_only() -> None:
    assert query_has_cjk("如何配置工时填写") is True
    assert query_has_cjk("suiteql 工时") is True
    assert query_has_cjk("access reports") is False
    assert query_has_cjk("saveTimeBill run_query_v2") is False


def test_plan_coverage_raw_merge_requires_primary_uncovered_and_scope() -> None:
    assert (
        plan_coverage_raw_merge(
            uncovered_latin_terms=("rag",),
            selected_nonempty=True,
            effective_scope="knowledge",
        )
        is True
    )
    assert (
        plan_coverage_raw_merge(
            uncovered_latin_terms=("rag",),
            selected_nonempty=False,
            effective_scope="knowledge",
        )
        is False
    )
    assert (
        plan_coverage_raw_merge(
            uncovered_latin_terms=(),
            selected_nonempty=True,
            effective_scope="knowledge",
        )
        is False
    )
    assert (
        plan_coverage_raw_merge(
            uncovered_latin_terms=("rag",),
            selected_nonempty=True,
            effective_scope="raw",
        )
        is False
    )


def test_plan_wiki_relaxed_single_shot_empty_or_weak_with_cjk_only() -> None:
    assert (
        plan_wiki_relaxed_single_shot(
            pool_kind="empty",
            question="如何配置工时填写",
            effective_scope="all",
        )
        == "wiki_relaxed_empty"
    )
    assert (
        plan_wiki_relaxed_single_shot(
            pool_kind="weak",
            question="suiteql 工时",
            effective_scope="knowledge",
        )
        == "wiki_relaxed_cjk"
    )
    assert (
        plan_wiki_relaxed_single_shot(
            pool_kind="strong",
            question="如何配置工时填写",
            effective_scope="knowledge",
        )
        is None
    )
    assert (
        plan_wiki_relaxed_single_shot(
            pool_kind="empty",
            question="access reports",
            effective_scope="knowledge",
        )
        is None
    )
    assert (
        plan_wiki_relaxed_single_shot(
            pool_kind="empty",
            question="如何配置",
            effective_scope="archive",
        )
        is None
    )
```

Cover FTS `limit` behavior in Task 3's pipeline tests (monkeypatch `search_fts` kwargs if needed). In Task 1 only lock the constant + view field:


```python
# tests/retrieval/test_query_recall_policy.py — append
from retrieval.query_recall_policy import (
    IDENTIFIER_PHRASE_CANDIDATES,
    WIKI_RELAXED_SINGLE_SHOT_LIMIT,
    RelaxedRecoveryRequestView,
)
from retrieval.query_shared import QueryFilters


def test_wiki_relaxed_single_shot_limit_constant_and_view_default() -> None:
    assert WIKI_RELAXED_SINGLE_SHOT_LIMIT == 40
    assert WIKI_RELAXED_SINGLE_SHOT_LIMIT < IDENTIFIER_PHRASE_CANDIDATES
    view = RelaxedRecoveryRequestView(
        metadata={},
        question="如何配置",
        scope="knowledge",
        project=None,
        filters=QueryFilters(),
        extra_terms=(),
        limit=WIKI_RELAXED_SINGLE_SHOT_LIMIT,
    )
    assert view.limit == 40
```

Update `tests/retrieval/test_query_import_contract.py` expected export list to include `"WIKI_RELAXED_SINGLE_SHOT_LIMIT"`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest \
  tests/retrieval/test_query_recovery.py::test_query_has_cjk_detects_han_and_rejects_latin_only \
  tests/retrieval/test_query_recovery.py::test_plan_coverage_raw_merge_requires_primary_uncovered_and_scope \
  tests/retrieval/test_query_recovery.py::test_plan_wiki_relaxed_single_shot_empty_or_weak_with_cjk_only \
  tests/retrieval/test_query_recall_policy.py::test_wiki_relaxed_single_shot_limit_constant_and_view_default \
  tests/retrieval/test_query_import_contract.py -v
```

Expected: FAIL — missing symbols / missing `limit` field / missing export.

- [ ] **Step 3: Write minimal implementation**

Add to `src/retrieval/query_recovery.py` (near the existing wiki_hop helpers):

```python
import re

_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uff00-\uffef]")


def query_has_cjk(question: str) -> bool:
    """True when the question contains CJK / kana / fullwidth characters."""

    return bool(_CJK_RE.search(question or ""))


def plan_coverage_raw_merge(
    *,
    uncovered_latin_terms: Sequence[str],
    selected_nonempty: bool,
    effective_scope: str,
) -> bool:
    """Whether sag2 default path should fire one coverage raw → merge."""

    return (
        bool(selected_nonempty)
        and bool(uncovered_latin_terms)
        and effective_scope in {"knowledge", "all"}
    )


def plan_wiki_relaxed_single_shot(
    *,
    pool_kind: WikiHopPoolKind,
    question: str,
    effective_scope: str,
) -> Literal["wiki_relaxed_empty", "wiki_relaxed_cjk"] | None:
    """Return knife-2 reason when empty/weak pool + CJK should try wiki_relaxed."""

    if effective_scope not in {"knowledge", "all"}:
        return None
    if pool_kind == "strong":
        return None
    if not query_has_cjk(question):
        return None
    if pool_kind == "empty":
        return "wiki_relaxed_empty"
    return "wiki_relaxed_cjk"
```

In `src/retrieval/query_recall_policy.py`:

```python
WIKI_RELAXED_SINGLE_SHOT_LIMIT = 40
```

Extend `RelaxedRecoveryRequestView`:

```python
@dataclass(frozen=True)
class RelaxedRecoveryRequestView:
    metadata: Mapping[str, Mapping[str, Any]]
    question: str
    scope: str
    project: str | None
    filters: QueryFilters
    extra_terms: Sequence[str]
    cancellation: QueryCancellationContext | None = None
    limit: int | None = None
```

In `relaxed_recovery_items`, replace the hard-coded limit:

```python
    fts_limit = (
        int(request.limit)
        if request.limit is not None
        else IDENTIFIER_PHRASE_CANDIDATES
    )
    try:
        hits = store.search_fts(
            request.question,
            limit=fts_limit,
            project=request.project,
            page_type=request.filters.type,
            tags=list(request.filters.tags),
            mode="relaxed",
            extra_terms=request.extra_terms,
        )
```

Add `"WIKI_RELAXED_SINGLE_SHOT_LIMIT"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest \
  tests/retrieval/test_query_recovery.py::test_query_has_cjk_detects_han_and_rejects_latin_only \
  tests/retrieval/test_query_recovery.py::test_plan_coverage_raw_merge_requires_primary_uncovered_and_scope \
  tests/retrieval/test_query_recovery.py::test_plan_wiki_relaxed_single_shot_empty_or_weak_with_cjk_only \
  tests/retrieval/test_query_recovery.py::test_wiki_hop_pool_kind_empty_weak_strong \
  tests/retrieval/test_query_recall_policy.py::test_wiki_relaxed_single_shot_limit_constant_and_view_default \
  tests/retrieval/test_query_import_contract.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_recovery.py src/retrieval/query_recall_policy.py \
  tests/retrieval/test_query_recovery.py \
  tests/retrieval/test_query_recall_policy.py \
  tests/retrieval/test_query_import_contract.py
git commit -m "$(cat <<'EOF'
feat(retrieval): add sag2 coverage/CJK wiki_relaxed planners

EOF
)"
```

---

### Task 2: Knife 1 — coverage → raw merge wiring + telemetry field

**Files:**
- Modify: `src/retrieval/query_execution_context.py`
- Modify: `src/retrieval/query_pipeline.py` (project `recovery_reasons`)
- Test: `tests/retrieval/test_sag2_pipeline_default.py`

**Interfaces:**
- Consumes: `plan_coverage_raw_merge`, `uncovered_latin_terms`, `merge_coverage_items`, existing raw store open / `search_fts` patterns from `_run_raw_single_shot`.
- Produces:
  - `QueryExecutionContext.sag2_recovery_reasons: list[str]` (and same on `QueryExecutionView`), default `[]`.
  - When knife 1 fires and admits raw into selection: append `"coverage_raw_merge"`; set `coverage_fallback=True`; set `sag2_raw_single_shot=True`; set `sag2_raw_fallback_mode="raw_merge"` (coverage is a merge, not replace). Prefer **not** inventing a fourth `raw_fallback_mode` value so existing `in {none, raw_merge, raw_replace}` assertions keep working; the distinct signal is `recovery_reasons`.
  - `pipeline["sag2_hop"]["recovery_reasons"]`: `list[str]` copy.
  - `pipeline["coverage"]["triggered"]` True when knife 1 admitted raw (via existing `coverage_fallback` projection).
  - After knife 1, `_run_raw_single_shot` must **not** fire a second raw for the same query (dedupe). Implement by teaching `_run_raw_single_shot` to no-op when `sag2_raw_single_shot` is already True **or** by restructuring so coverage + empty/weak share one orchestrator that calls raw at most once.

**Orchestrator shape (lock):** replace the non-legacy early return body with `_run_sag2_default_recovery(request_view)`:

```python
def _run_sag2_default_recovery(self, request_view: QueryRequestView) -> None:
    self.sag2_recovery_reasons = []
    self.uncovered_latin_terms = recall_policy.uncovered_latin_terms(
        request_view.question, self.selected
    )
    # Knife 1
    if plan_coverage_raw_merge(
        uncovered_latin_terms=self.uncovered_latin_terms,
        selected_nonempty=bool(self.selected),
        effective_scope=request_view.effective_scope,
    ):
        self._run_coverage_raw_merge(request_view)
    # Knife 2 + existing empty/weak raw: Task 3 fills knife 2;
    # for Task 2 call existing _run_raw_single_shot but skip if raw already used.
    if not self.sag2_raw_single_shot:
        self._run_raw_single_shot(request_view)
    elif self.sag2_raw_single_shot and "coverage_raw_merge" in self.sag2_recovery_reasons:
        # coverage already consumed the single raw; do not second-shot.
        pass
```

`_run_coverage_raw_merge` mirrors `_run_raw_single_shot`'s raw open/search/eligible loop, then:

```python
merged = recall_policy.merge_coverage_items(
    self.selected,
    raw_items,
    self.uncovered_latin_terms,
    rrf_k=request_view.effective_rrf_k,
)
selected = recall_policy.adaptive_expand(
    select_best_per_page(merged), request_view.top_k
)[: request_view.top_k]
# assemble_recovery(...); set flags; append "coverage_raw_merge"
self.coverage_fallback = any(
    item["hit"].source_kind == "raw" for item in selected
)
if self.coverage_fallback:
    self.sag2_recovery_reasons.append("coverage_raw_merge")
self.sag2_raw_single_shot = True
self.sag2_raw_fallback_mode = "raw_merge"
```

If raw unavailable/empty: leave wiki selected as-is; still record intent? Prefer: append `coverage_raw_merge` only when selection actually gained a raw row **or** when the branch was entered and raw was attempted — lock to **branch entered + raw_items non-empty + merge produced raw in selected** for `coverage_fallback`; always append reason when branch entered and raw search returned items (even if merge filtered all — then do **not** set `coverage_fallback`). Simplest testable rule: set reason + `coverage_fallback` iff any selected item has `source_kind == "raw"` after merge (same as legacy coverage).

Thread `sag2_recovery_reasons` through `execute()` → `QueryExecutionView` → `_frozen_view` exactly like `sag2_raw_fallback_mode`.

Projection:

```python
"sag2_hop": {
    ...
    "raw_fallback_mode": view.sag2_raw_fallback_mode,
    "recovery_reasons": list(view.sag2_recovery_reasons),
},
```

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/retrieval/test_sag2_pipeline_default.py
def test_coverage_raw_merge_keeps_wiki_and_adds_raw(tmp_path: Path) -> None:
    """Uncovered Latin on a strong-ish wiki pool must merge raw, not replace."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/invoice.md",
        {"title": "Invoice", "type": "concept"},
        "invoice approval is documented here",
    )
    raw = root / "raw/sources/references/rag.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: RAG Reference\n---\n\nRAG evidence complements invoice retrieval.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "invoice RAG", retrieval_mode="lexical", top_k=5
    )
    assert result["ok"] is True
    all_paths = [item["path"] for item in result["results"]] + [
        item["path"] for item in result.get("additional_results", [])
    ]
    assert "wiki/concepts/invoice.md" in all_paths
    assert any("rag" in str(p).casefold() or str(p).startswith("raw/") for p in all_paths)
    sag2 = result["pipeline"]["sag2_hop"]
    assert "coverage_raw_merge" in sag2["recovery_reasons"]
    assert sag2["raw_single_shot"] is True
    assert sag2["raw_fallback_mode"] == "raw_merge"
    assert result["pipeline"]["coverage"]["triggered"] is True
    assert "rag" in [
        t.casefold() for t in result["pipeline"]["coverage"]["uncovered_latin_terms"]
    ] or result["pipeline"]["coverage"]["triggered"] is True
    # Must not look like full P2 ladder (no wiki_relaxed / raw_zero lexical mode).
    assert result["pipeline"]["lexical"]["mode"] not in {
        "wiki_relaxed",
        "raw_zero",
        "coverage",
    }


def test_coverage_and_weak_share_one_raw_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage raw must not be followed by a second weak raw FTS."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/invoice.md",
        {"title": "Invoice", "type": "concept"},
        "invoice approval is documented here",
    )
    raw = root / "raw/sources/references/rag.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: RAG Reference\n---\n\nRAG evidence complements invoice retrieval.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    import retrieval.query_execution_context as qec
    from retrieval.retrieval_index import RetrievalIndexStore

    calls = {"n": 0}
    real_search = RetrievalIndexStore.search_fts

    def counting_search(self, query, **kwargs):  # noqa: ANN001
        # Count only raw-store searches (source_kind paths under raw/).
        if "raw" in str(getattr(self, "root", "")) or "raw" in str(
            getattr(self, "_root", "")
        ):
            calls["n"] += 1
        # Fallback: count every search_fts after hop by monkeypatching the
        # raw_store instance method inside _run_coverage_raw_merge — if root
        # detection is flaky, patch QueryExecutionContext.get_raw_store to wrap
        # search_fts. Prefer the wrapper approach below if this heuristic fails.
        return real_search(self, query, **kwargs)

    monkeypatch.setattr(RetrievalIndexStore, "search_fts", counting_search)

    # Force weak classification AFTER coverage so a buggy second shot would fire.
    real_kind = qec.wiki_hop_pool_kind

    def kind_after(selected, evidence):  # noqa: ANN001
        # During coverage selected is wiki-only with uncovered latin; after merge
        # a second shot would see weak if we force it.
        return real_kind(selected, evidence)

    monkeypatch.setattr(qec, "wiki_hop_pool_kind", kind_after)

    result = run_query_v2(root, "invoice RAG", retrieval_mode="lexical", top_k=5)
    assert "coverage_raw_merge" in result["pipeline"]["sag2_hop"]["recovery_reasons"]
    # At most one raw FTS. If root-heuristic undercounts, assert via a wrapper on
    # get_raw_store instead (implementer: switch to that if calls["n"] stays 0).
    assert calls["n"] <= 1
```

**Fixture note for raw-call counting:** If `RetrievalIndexStore.root` is unreliable, wrap at `QueryExecutionContext.get_raw_store`:

```python
    real_get = qec.QueryExecutionContext.get_raw_store

    def wrapped(self):  # noqa: ANN001
        store = real_get(self)
        if not getattr(store, "_knife_counted", False):
            orig = store.search_fts

            def counted(query, **kwargs):  # noqa: ANN001
                calls["n"] += 1
                return orig(query, **kwargs)

            store.search_fts = counted  # type: ignore[method-assign]
            store._knife_counted = True  # type: ignore[attr-defined]
        return store

    monkeypatch.setattr(qec.QueryExecutionContext, "get_raw_store", wrapped)
```

Update `test_pipeline_includes_sag2_hop_telemetry` required key set to include `"recovery_reasons"`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest \
  tests/retrieval/test_sag2_pipeline_default.py::test_coverage_raw_merge_keeps_wiki_and_adds_raw \
  tests/retrieval/test_sag2_pipeline_default.py::test_coverage_and_weak_share_one_raw_shot \
  tests/retrieval/test_sag2_pipeline_default.py::test_pipeline_includes_sag2_hop_telemetry \
  -v
```

Expected: FAIL — missing `recovery_reasons` / coverage not merging on default path.

- [ ] **Step 3: Implement coverage merge + projection**

1. Import planners in `query_execution_context.py`:
   ```python
   from retrieval.query_recovery import (
       plan_coverage_raw_merge,
       plan_raw_single_shot,
       plan_wiki_relaxed_single_shot,  # used in Task 3; import now or in Task 3
       wiki_hop_is_weak,
       wiki_hop_pool_kind,
   )
   ```
2. Add `sag2_recovery_reasons: list[str] = field(default_factory=list, init=False)` on context; `sag2_recovery_reasons: Sequence[str] = ()` on `QueryExecutionView`; copy in `execute` / `_frozen_view`.
3. Implement `_run_coverage_raw_merge` by extracting the shared “open raw + strict FTS + eligible + step bonus” block from `_run_raw_single_shot` into a private helper `_collect_raw_single_shot_items(...) -> list[dict]` **if** that reduces duplication; otherwise duplicate the minimal loop once (YAGNI: small duplication OK for Task 2, refactor only if both call sites already diverge painfully).
4. Change non-legacy branch to `_run_sag2_default_recovery` as sketched (knife 2 stubbed as pass-through to `_run_raw_single_shot` until Task 3).
5. Project `recovery_reasons` in `assemble_public_projection`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py -v
uv run pytest tests/retrieval/test_query_recovery.py -q --tb=line
```

Expected: PASS. Existing empty/weak raw tests still green. If `test_raw_weak_pool_merges_and_keeps_wiki` fails because uncovered latin on “access reports” somehow triggers coverage, assert that fixture has no uncovered Latin relative to the stub (both share `reports`) — adjust wiki body so all Latin query tokens are covered **or** keep monkeypatch forcing weak without uncovered.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_execution_context.py src/retrieval/query_pipeline.py \
  tests/retrieval/test_sag2_pipeline_default.py
git commit -m "$(cat <<'EOF'
feat(retrieval): merge coverage raw into sag2 hop pools

EOF
)"
```

---

### Task 3: Knife 2 — empty/CJK bounded wiki_relaxed single-shot

**Files:**
- Modify: `src/retrieval/query_execution_context.py` (`_run_sag2_default_recovery` + `_run_wiki_relaxed_single_shot`)
- Test: `tests/retrieval/test_sag2_pipeline_default.py`

**Interfaces:**
- Consumes: `plan_wiki_relaxed_single_shot`, `relaxed_recovery_items`, `WIKI_RELAXED_SINGLE_SHOT_LIMIT`, `RelaxedRecoveryRequestView(limit=...)`, `relaxed_fill_is_strong` (reuse — if fill is weak noise, treat as unanswered and fall through to empty→raw replace).
- Produces:
  - Append `"wiki_relaxed_empty"` or `"wiki_relaxed_cjk"` to `sag2_recovery_reasons` when the branch is entered (even if fill abstains — so telemetry records the attempt). Optionally also append only on strong fill; **lock:** append when branch entered; set `lexical_mode` to `"relaxed"` (public maps to `wiki_relaxed` via existing projection helpers if any) **only when** a strong fill updates `selected`.
  - `counters.relaxed_fts_hits` updated via existing `self.relaxed_fts_hits`.
  - Order inside `_run_sag2_default_recovery`:
    1. Knife 1 coverage raw merge (Task 2)
    2. Recompute `pool_kind = wiki_hop_pool_kind(self.selected, evidence_scores)`
    3. Knife 2 wiki_relaxed if `plan_wiki_relaxed_single_shot(...)` returns a reason
    4. If still empty → `_run_raw_single_shot` (replace) **unless** raw already used by coverage
    5. Elif still weak and raw not yet used → `_run_raw_single_shot` (merge)

**Strong-fill rule:** call `recall_policy.relaxed_fill_is_strong(question, selected, extra_terms=[])`. If False, clear the relaxed selection (abstain) so empty→raw replace can run — mirrors legacy weak-relaxed abstain, but **do not** schedule further ladder branches.

**Merge vs replace for relaxed into a weak (non-empty) pool:** when `pool_kind == "weak"` and relaxed returns strong items, **replace** `self.selected` with the relaxed adaptive_expand result (legacy wiki_relaxed behavior) — wiki_relaxed is a wiki-side recovery, not raw. Do not raw-merge on top unless still empty after abstain.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/retrieval/test_sag2_pipeline_default.py
def test_cjk_empty_pool_fires_one_wiki_relaxed(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Strict AND may miss; relaxed OR / CJK bigrams should hit.
    write_test_page(
        root,
        "wiki/concepts/timebill.md",
        {"title": "工时填写", "type": "concept"},
        "本文说明如何配置工时填写流程与保存步骤。",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "如何配置工时填写", retrieval_mode="lexical", top_k=5
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert "wiki_relaxed_empty" in sag2["recovery_reasons"] or (
        "wiki_relaxed_cjk" in sag2["recovery_reasons"]
    )
    # Single-shot: must not also look like raw_zero / coverage ladder chain.
    assert result["pipeline"]["lexical"]["mode"] not in {"raw_zero", "coverage"}
    assert result["pipeline"]["counters"]["relaxed_fts_hits"] >= 0
    # If relaxed answered, results should be wiki paths.
    if result["results"]:
        assert result["results"][0]["path"].startswith("wiki/")


def test_cjk_relaxed_still_empty_falls_back_to_raw_replace(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    # No wiki pages that can answer; raw holds the CJK-adjacent Latin help text.
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: 工时帮助\n---\n\n如何配置工时填写：打开菜单后保存。",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(
        root, "如何配置工时填写", retrieval_mode="lexical", top_k=5
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert "wiki_relaxed_empty" in sag2["recovery_reasons"]
    # After failed/empty relaxed, empty→raw replace.
    assert sag2["raw_fallback_mode"] == "raw_replace"
    assert sag2["raw_single_shot"] is True
    assert result["results"]
    assert str(result["results"][0]["path"]).startswith("raw/")


def test_default_path_never_runs_full_p2_ladder_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when coverage + CJK empty signals exist, do not call plan_fallback chain."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/invoice.md",
        {"title": "Invoice", "type": "concept"},
        "invoice approval",
    )
    refresh_indexes(root)

    import retrieval.query_execution_context as qec
    import retrieval.query_recovery as recovery

    calls = {"plan_fallback": 0}
    real_plan = recovery.plan_fallback

    def counted(state):  # noqa: ANN001
        calls["plan_fallback"] += 1
        return real_plan(state)

    monkeypatch.setattr(qec, "plan_fallback", counted)
    monkeypatch.setattr(recovery, "plan_fallback", counted)

    run_query_v2(root, "invoice RAG 如何配置", retrieval_mode="lexical", top_k=5)
    assert calls["plan_fallback"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest \
  tests/retrieval/test_sag2_pipeline_default.py::test_cjk_empty_pool_fires_one_wiki_relaxed \
  tests/retrieval/test_sag2_pipeline_default.py::test_cjk_relaxed_still_empty_falls_back_to_raw_replace \
  tests/retrieval/test_sag2_pipeline_default.py::test_default_path_never_runs_full_p2_ladder_chain \
  -v
```

Expected: FAIL — knife 2 not wired / reasons missing.

- [ ] **Step 3: Implement wiki_relaxed single-shot**

```python
def _run_wiki_relaxed_single_shot(
    self,
    request_view: QueryRequestView,
    reason: Literal["wiki_relaxed_empty", "wiki_relaxed_cjk"],
) -> None:
    self.sag2_recovery_reasons.append(reason)
    self.cancellation.checkpoint("fallback")
    items, self.relaxed_fts_hits, relaxed_warning = recall_policy.relaxed_recovery_items(
        self.store,
        recall_policy.RelaxedRecoveryRequestView(
            metadata=request_view.metadata,
            question=request_view.question,
            scope=request_view.effective_scope,
            project=request_view.project,
            filters=request_view.filters,
            extra_terms=(),
            cancellation=self.cancellation,
            limit=recall_policy.WIKI_RELAXED_SINGLE_SHOT_LIMIT,
        ),
    )
    if relaxed_warning:
        self.status = {**self.status, "code": relaxed_warning}
    if not items:
        return
    # optional step_bonus / compose_score — reuse the minimal scoring from legacy
    # wiki_relaxed block (compose_score + step_bonus) for parity.
    step_counts = recall_policy.step_counts_for_pages(
        sorted({item["hit"].page_path for item in items}),
        self.store,
        None,
    )
    for item in items:
        count = step_counts.get(item["hit"].page_path, 0)
        item["score"] = compose_score(
            item["hit"],
            request_view.question,
            request_view.intent,
            request_view.effective_scope,
            request_view.metadata,
            base=item["hit"].score,
            freshness=True,
            step_bonus=step_bonus(count),
        )
    items.sort(
        key=lambda item: (
            -item["score"],
            item["hit"].page_path,
            item["hit"].passage_id,
        )
    )
    selected = recall_policy.adaptive_expand(
        select_best_per_page(items), request_view.top_k
    )[: request_view.top_k]
    # Hard bound top-N even if adaptive_expand grows.
    selected = selected[: recall_policy.WIKI_RELAXED_SINGLE_SHOT_LIMIT]
    if not recall_policy.relaxed_fill_is_strong(
        request_view.question, selected, extra_terms=[]
    ):
        return  # abstain; leave prior selected (possibly empty)
    self.selected = selected
    self.recovery = assemble_recovery(
        selected,
        condition=RecoveryCondition(),
        candidates=items,
        store=self.store,
        cancellation=self.cancellation,
    )
    self.context_items = self.recovery.context_items
    self.lexical_mode = "relaxed"
```

Wire into `_run_sag2_default_recovery` after knife 1:

```python
    evidence_scores = {
        item["hit"].passage_id: float(item.get("evidence_score", 0.0))
        for item in self.selected
    }
    pool_kind = wiki_hop_pool_kind(self.selected, evidence_scores)
    relaxed_reason = plan_wiki_relaxed_single_shot(
        pool_kind=pool_kind,
        question=request_view.question,
        effective_scope=request_view.effective_scope,
    )
    if relaxed_reason is not None:
        self._run_wiki_relaxed_single_shot(request_view, relaxed_reason)

    if not self.sag2_raw_single_shot:
        self._run_raw_single_shot(request_view)
```

Ensure `_run_raw_single_shot` still classifies empty/weak **after** relaxed may have filled the pool.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py -v
uv run pytest tests/retrieval -q --tb=line
```

Expected: new knife tests PASS; no new regressions. Update any default-path test that assumed `relaxed_fts_hits == 0` **unconditionally** — keep that assertion only for Latin-only fixtures; CJK empty fixtures may be > 0.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_execution_context.py tests/retrieval/test_sag2_pipeline_default.py
git commit -m "$(cat <<'EOF'
feat(retrieval): add bounded wiki_relaxed single-shot for CJK empty pools

EOF
)"
```

---

### Task 4: CHANGELOG note

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:** none.

- [ ] **Step 1: Edit CHANGELOG under the v1.0.0 / 2026-11-09 section**

Add an additive subsection after the recall-lift block:

```markdown
## 2026-11-09 — v1.0.0：SAG2 召回第二刀（coverage raw merge + CJK wiki_relaxed）

- **刀 1 — 空洞 raw merge：** hop+图扩展后，若问句仍有未覆盖 Latin/标识符 token（复用 `uncovered_latin_terms`）且池非空，则 **一次** raw FTS，经 `merge_coverage_items` **merge** 进 wiki+hop（非整池 replace）；与第一刀弱池 merge **共用至多一次** raw。
- **刀 2 — 空池/CJK wiki_relaxed：** 池为空或弱（top 证据 `< 0.15`）**且**问句含 CJK 时，发 **单次** 有界 `wiki_relaxed`（`WIKI_RELAXED_SINGLE_SHOT_LIMIT=40`）；弱噪声弃权；仍空则走既有 empty→raw **replace**。
- **Telemetry：** `pipeline.sag2_hop.recovery_reasons` 可含 `coverage_raw_merge` / `wiki_relaxed_cjk` / `wiki_relaxed_empty`；`raw_fallback_mode` 仍为 `none` | `raw_merge` | `raw_replace`；`pipeline.coverage.triggered` 在刀 1 采纳 raw 时可为 `true`。
- **不做：** 不恢复默认 P2 ladder；本轮不含刀 3（图扩展/bonus 抖动）。
- **验收：** codingwork gold 同口径 **R@10 ≥ 0.75**；未达标须 FAIL_LOUD。详设 `docs/specs/2026-11-09-sag2-recall-knife-coverage-relaxed-design.md`。
```

- [ ] **Step 2: Commit**

```bash
git add -f CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: note sag2 recall knife coverage+relaxed in CHANGELOG

EOF
)"
```

---

### Task 5: Eval smoke — codingwork gold R@10 ≥ 0.75 (fail loudly)

**Files:**
- Modify (optional results note): `docs/plans/2026-11-09-sag2-recall-knife-coverage-relaxed.md` — append a “Task 5 results” table after the run.
- No application code.

**Interfaces:** none new. Uses existing CLI `llm-wiki-mcp retrieval-eval`.

**Baseline to beat:** first-knife FAIL_LOUD **R@10 ≈ 0.6428** (29 cases). Hop-default ≈ **0.6655**. **Gate: R@10 ≥ 0.75**.

- [ ] **Step 1: Unit smoke (always on box)**

```bash
uv run pytest \
  tests/retrieval/test_query_recovery.py::test_query_has_cjk_detects_han_and_rejects_latin_only \
  tests/retrieval/test_query_recovery.py::test_plan_coverage_raw_merge_requires_primary_uncovered_and_scope \
  tests/retrieval/test_query_recovery.py::test_plan_wiki_relaxed_single_shot_empty_or_weak_with_cjk_only \
  tests/retrieval/test_sag2_pipeline_default.py \
  -q
```

Expected: all PASS.

- [ ] **Step 2: Broader retrieval regression (informational)**

```bash
uv run pytest tests/retrieval -q --tb=line
```

Expected: no new failures vs pre-task HEAD (legacy ladder tests remain opt-in).

- [ ] **Step 3: Codingwork gold eval (gate)**

Artifacts from prior runs on this box:

- Vault: `/tmp/sag2-eval/codingwork-vault`
- Dataset: `/tmp/sag2-eval/codingwork-gold.jsonl`
- Manifest: `/tmp/sag2-eval/codingwork-gold.manifest.json`

```bash
mkdir -p /tmp/sag2-eval/reports-recall-knife
uv run llm-wiki-mcp retrieval-eval \
  --vault /tmp/sag2-eval/codingwork-vault \
  --dataset /tmp/sag2-eval/codingwork-gold.jsonl \
  --manifest /tmp/sag2-eval/codingwork-gold.manifest.json \
  --output-dir /tmp/sag2-eval/reports-recall-knife \
  --top-k 10 \
  --retrieval-mode lexical \
  --entrypoint engine \
  --query-version v2
```

Extract macro R@10:

```bash
python3 - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("/tmp/sag2-eval/reports-recall-knife/retrieval-eval.json").read_text())
metrics = report["metrics"]
r10 = (
    metrics.get("recall_at_10")
    or metrics.get("macro_recall_at_10")
    or metrics.get("recall@10")
    or metrics.get("R@10")
)
if r10 is None and isinstance(metrics.get("ranking"), dict):
    r10 = metrics["ranking"].get("recall_at_10") or metrics["ranking"].get("macro_recall_at_10")
print("R@10=", r10)
print("metrics_keys=", sorted(metrics.keys()))
if r10 is None:
    raise SystemExit("FAIL_LOUD: could not locate R@10 in report metrics")
if float(r10) < 0.75:
    raise SystemExit(
        f"FAIL_LOUD: R@10={r10} < 0.75 (first-knife baseline~0.6428; hop-default~0.6655)"
    )
print("PASS: R@10 gate met")
PY
```

**Fail-loudly criterion (mandatory):**

- If R@10 `< 0.75` → Task 5 **failed gate**: write measured number, baselines `0.6428` / `0.6655`, delta, and commit hash into the results note; **do not** claim knife success; leave application code as-is for follow-up (knife 3 / top-N / uncovered definition) — **do not** silently re-enable P2 ladder.
- If vault/dataset missing → `SKIPPED_NO_VAULT` + exact command above; unit/regression must still pass.

Same-protocol note: drop `codingwork-0029` only if those two gold raw paths are still missing (same as prior runs). Document case count actually scored.

- [ ] **Step 4: Append results note to this plan + commit if docs changed**

```markdown
#### Task 5 results note (YYYY-MM-DD UTC+8)

| Run | Cases | R@10 macro | Notes |
| --- | --- | --- | --- |
| Unit smoke (Tasks 1–3 tests) | … | — | … |
| `tests/retrieval` | … | — | … |
| codingwork-wiki-query-gold | … | … | PASS ≥0.75 / FAIL_LOUD / SKIPPED_NO_VAULT |
```

```bash
git add -f docs/plans/2026-11-09-sag2-recall-knife-coverage-relaxed.md
git commit -m "$(cat <<'EOF'
docs: record sag2 recall knife eval smoke results

EOF
)" || true
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Uncovered Latin/identifier → one raw → **merge** | Task 2 (`merge_coverage_items` + `uncovered_latin_terms`) |
| Empty/near-empty + CJK → one bounded `wiki_relaxed` | Task 1 planners + Task 3 |
| Still empty → existing empty→raw **replace** | Task 3 (falls through to `_run_raw_single_shot`) |
| Do **not** restore full P2 ladder | Global Constraints + Task 3 ladder-chain test |
| Telemetry: `coverage_raw_merge`, `wiki_relaxed_cjk` / `wiki_relaxed_empty`, existing raw_merge/replace | Tasks 2–3 (`recovery_reasons` + `raw_fallback_mode`) |
| Dedup with first-knife weak raw; same identity keys | Task 2 (single raw shot) + reuse merge helpers |
| Unit contracts: coverage merge / single relaxed / no full ladder | Tasks 2–3 |
| codingwork gold R@10 ≥ 0.75; fail loudly | Task 5 |
| CHANGELOG behavior note | Task 4 |
| Near-empty / top-N locked in plan | Global Constraints + Task 1 (`weak` + limit 40) |

**Ambiguities resolved by reading code:**
1. **Near-empty** = `wiki_hop_pool_kind == "weak"` (top evidence `< 0.15`), not a new numeric gate.
2. **Identifier coverage** = existing `uncovered_latin_terms` (Latin `[a-z0-9_]+` len≥3 after `tokens()` casefold), no second detector.
3. **Knife 2 CJK gate** = `(empty OR weak) AND has_CJK` (mermaid reading); empty Latin-only skips relaxed and uses existing raw replace.
4. **Coverage merge helper** = `merge_coverage_items` (filters covering raw), not `merge_weak_primary_raw`.
5. **Telemetry split** = keep `raw_fallback_mode` tri-state; add `recovery_reasons` list for knife-specific codes.
6. **Relaxed bound** = `WIKI_RELAXED_SINGLE_SHOT_LIMIT = 40` via new optional `RelaxedRecoveryRequestView.limit`.

**Placeholder scan:** No TBD/TODO. Thresholds, paths, pytest commands, eval CLI, and reason string literals are concrete.

**Type consistency:** `plan_wiki_relaxed_single_shot` → `"wiki_relaxed_empty"|"wiki_relaxed_cjk"|None`; `recovery_reasons` list[str]; `raw_fallback_mode` stays `"none"|"raw_merge"|"raw_replace"`; `WIKI_RELAXED_SINGLE_SHOT_LIMIT = 40` used in view + slice.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-11-09-sag2-recall-knife-coverage-relaxed.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (`superpowers:subagent-driven-development`).
2. **Inline Execution** — execute tasks in this session with `superpowers:executing-plans` and checkpoints.

Which approach?

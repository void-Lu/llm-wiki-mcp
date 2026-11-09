# SAG2 Recall-Lift (Weak Raw Merge + Judge Bonuses) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

I'm using the writing-plans skill to create the implementation plan.

**Goal:** Raise codingwork gold R@10 from ~0.665 toward **≥ 0.80** on branch `v1.0.0` by (1) merging single-shot raw into a weak wiki+hop pool instead of replacing it, and (2) adding bounded title / CJK / identifier bonuses to `HeuristicEvidenceJudge` — without restoring the P2 ladder.

**Architecture:** Keep the v1.0.0 sag2_hop default path. Extend the local heuristic judge so title/path, CJK spans, and identifier-like tokens can lift seed scores (and thus expansion quality). Change only the post-hop raw single-shot branch in `QueryExecutionContext._run_raw_single_shot`: **empty pool → replace**; **weak pool (top evidence < 0.15) → merge** via existing `merge_weak_primary_raw`; strong pool unchanged. Emit telemetry `raw_fallback_mode` ∈ {`none`,`raw_merge`,`raw_replace`} alongside the existing `raw_single_shot` bool.

**Tech Stack:** Python 3.11+, pytest, SQLite FTS5, existing `PassageHit` / `candidate_item` / `merge_weak_primary_raw` / MCP `wiki_query` envelope. Test with `uv run pytest` (`pythonpath=src`). Gold eval via `uv run llm-wiki-mcp retrieval-eval` when the codingwork vault+dataset are available on the box (`/tmp/sag2-eval/…`).

## Global Constraints

- Spec (source of truth): `docs/specs/2026-11-09-sag2-recall-lift-merge-judge-design.md` (commit `b03f54e` on `v1.0.0`).
- Prior path: `docs/specs/2026-11-09-sag2-hop-default-design.md` + landed plan `docs/plans/2026-11-09-sag2-hop-default.md`.
- Package version remains **1.0.0** (no bump unless user asks).
- **Approach A only:** empty replace + weak merge + Judge title/CJK/identifier bonuses. **Do not** restore default P2 ladder; **do not** implement Approach C (TermSelector rare proper-name keep) in this plan.
- **Local-only Judge:** no HTTP / hosted JEV / vault egress for judging; clamp scores to `[0,1]`.
- Weak threshold stays **`top evidence < 0.15`** (`wiki_hop_is_weak`); this plan changes merge-vs-replace semantics, not the numeric gate.
- Dedup identity for merge: existing passage/page identity used by `merge_weak_primary_raw` / `select_best_per_page` (do not invent a new key).
- After merge, sort/select with existing `adaptive_expand` + `top_k` (reuse P2 weak-primary merge ordering; no new tie-break invention).
- MCP public field names stay stable; add `pipeline.sag2_hop.raw_fallback_mode`; keep `raw_single_shot` True whenever either merge or replace fired.
- Vectors remain opt-in; no index rebuild required (query-time).
- TDD each task: failing test → fail → implement → pass → commit.
- `docs/*` is gitignored → `git add -f` for plans/specs/CHANGELOG as needed.
- **Do not push** unless the user asks.
- **Eval gate:** same-protocol codingwork gold **R@10 ≥ 0.80**. If unmet, record baseline / measured / delta **loudly** — never silently claim success.

## File map

| File | Role |
|------|------|
| `src/retrieval/sag2_evidence.py` | **Modify.** Extend `EvidenceJudge` / `HeuristicEvidenceJudge` / `LocalLlmEvidenceJudge` with optional `title` / `page_path`; add bounded title, CJK-span, identifier bonuses; clamp `[0,1]`. |
| `src/retrieval/sag2_hop.py` | **Modify.** Pass `hit.title` and `hit.page_path` into `judge.score(...)` so bonuses actually fire during hop. |
| `src/retrieval/query_recovery.py` | **Modify (minimal).** Keep `wiki_hop_is_weak` / `plan_raw_single_shot`. Add pure helper `wiki_hop_pool_kind(selected, evidence_scores) -> Literal["empty","weak","strong"]` for clear empty-vs-weak branching (threshold still 0.15). |
| `src/retrieval/query_execution_context.py` | **Modify.** `_run_raw_single_shot`: empty → replace; weak → `merge_weak_primary_raw`; set `sag2_raw_fallback_mode` + keep `sag2_raw_single_shot`. Thread field through execute view / frozen view. |
| `src/retrieval/query_pipeline.py` | **Modify.** Project `raw_fallback_mode` under `pipeline["sag2_hop"]`. |
| `src/retrieval/query_recall_policy.py` | **Reuse.** Call existing `merge_weak_primary_raw` — no formula change required. |
| `tests/retrieval/test_sag2_evidence.py` | **Modify.** Title / CJK / identifier ranking + clamp + no-network still holds. |
| `tests/retrieval/test_sag2_hop.py` | **Modify only if** a call-site stub needs the new kwargs (controller still works with Protocol defaults). Prefer a small unit asserting title-aware scoring when judge receives title. |
| `tests/retrieval/test_sag2_pipeline_default.py` | **Modify.** Weak-pool merge keeps wiki path; empty still replace; telemetry `raw_merge` / `raw_replace`. |
| `tests/retrieval/test_query_recovery.py` | **Modify (minimal).** Cover `wiki_hop_pool_kind` empty/weak/strong. |
| `CHANGELOG.md` | **Modify.** Note recall-lift behavior under the v1.0.0 section (or a dated subsection). |
| `docs/specs/2026-11-09-sag2-recall-lift-merge-judge-design.md` | Spec (read-only during impl). |
| `docs/plans/2026-11-09-sag2-recall-lift-merge-judge.md` | This plan. |

### Current anchors (do not invent)

Weak/empty gate today (`src/retrieval/query_recovery.py`):

```python
def wiki_hop_is_weak(selected, evidence_scores) -> bool:
    if not selected:
        return True
    top = max((float(evidence_scores.get(item["hit"].passage_id, 0.0)) for item in selected), default=0.0)
    return top < 0.15
```

Raw single-shot today **always replaces** (`query_execution_context.py` ~614–631):

```python
# Empty/weak wiki: raw_only replace (no weak-primary merge ladder).
selected = select_best_per_page(raw_items)
...
self.selected = selected
```

P2 already has the merge helper to reuse (`query_recall_policy.merge_weak_primary_raw`).

Judge today scores only `(question, passage_text, context=…)` and ignores `PassageHit.title` / `page_path` (`sag2_hop.py` ~76).

Telemetry today (`query_pipeline.py` ~809–816): `raw_single_shot: bool` only.

---

### Task 1: HeuristicEvidenceJudge title / CJK / identifier bonuses

**Files:**
- Modify: `src/retrieval/sag2_evidence.py`
- Modify: `src/retrieval/sag2_hop.py` (pass `title=` / `page_path=` at call site)
- Test: `tests/retrieval/test_sag2_evidence.py`
- Optional small assert: `tests/retrieval/test_sag2_hop.py` (only if needed to lock call-site wiring)

**Interfaces:**
- Consumes: existing `EvidenceJudge` protocol; `PassageHit.title` / `PassageHit.page_path`.
- Produces:
  - Updated signature (keyword-only additions, backward compatible):
    ```python
    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
        title: str = "",
        page_path: str = "",
    ) -> float: ...
    ```
  - Bounded bonuses (exact constants below) added on top of existing coverage / phrase / context logic; final `max(0.0, min(1.0, …))`.
  - `LocalLlmEvidenceJudge.score` forwards `title` / `page_path` to fallback and to `scorer` only if the scorer accepts them — simplest: keep `ScorerFn` as today and apply heuristic bonuses only in `HeuristicEvidenceJudge`; `LocalLlmEvidenceJudge` passes kwargs into `fallback.score(...)` on fallback paths, and when calling `scorer` uses the old 3-arg form (YAGNI: do not reinvent local-LLM scorer API this task).

**Bonus constants (lock in code + tests):**

| Signal | How computed | Cap |
|--------|--------------|-----|
| Title/path | `\|q_tokens ∩ (title_tokens ∪ path_tokens)\| / \|q_tokens\|` × `0.25` | ≤ `0.25` |
| CJK span | For each CJK run (≥2 chars) in question that appears as substring in `passage_text` (or title): `0.10` per distinct hit | ≤ `0.20` |
| Identifier | Query tokens matching `[A-Za-z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]+)+` (CamelCase) or containing `_` / digit, present in passage/title/path | `0.15` if ≥1 hit else `0.0` |

Path tokens: split `page_path` on `/` and `_` / `-`, drop empty + extension (`.md`), tokenize with existing `_TOKEN`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/retrieval/test_sag2_evidence.py
from __future__ import annotations

import socket
from typing import Sequence

import pytest

from retrieval.sag2_evidence import HeuristicEvidenceJudge, LocalLlmEvidenceJudge


def test_title_bonus_outranks_body_only_weak_overlap() -> None:
    judge = HeuristicEvidenceJudge()
    q = "email capture"
    # Body mentions only one query token weakly; title carries both.
    titled = judge.score(
        q,
        "capture settings live here.",
        title="Email Capture Plug-in",
        page_path="wiki/concepts/email-capture.md",
    )
    body_only = judge.score(
        q,
        "capture settings live here.",
        title="Unrelated Page",
        page_path="wiki/concepts/other.md",
    )
    assert 0.0 <= body_only < titled <= 1.0


def test_cjk_span_bonus_outranks_latin_only_body() -> None:
    judge = HeuristicEvidenceJudge()
    q = "如何配置工时填写"
    with_cjk = judge.score(
        q,
        "本文说明如何配置工时填写流程。",
        title="工时",
        page_path="wiki/concepts/timebill.md",
    )
    latinish = judge.score(
        q,
        "configuration documentation without the chinese span.",
        title="Config",
        page_path="wiki/concepts/config.md",
    )
    assert 0.0 <= latinish < with_cjk <= 1.0


def test_identifier_bonus_rewards_snake_and_camel_hits() -> None:
    judge = HeuristicEvidenceJudge()
    q = "call saveTimeBill then run_query_v2"
    hit = judge.score(
        q,
        "Handlers invoke saveTimeBill before indexing.",
        title="run_query_v2 notes",
        page_path="wiki/concepts/run_query_v2.md",
    )
    miss = judge.score(
        q,
        "Handlers invoke the nightly job before indexing.",
        title="Nightly job",
        page_path="wiki/concepts/nightly.md",
    )
    assert 0.0 <= miss < hit <= 1.0


def test_bonuses_still_clamp_to_unit_interval() -> None:
    judge = HeuristicEvidenceJudge()
    q = "EmailCapture saveTimeBill 工时填写 run_query_v2"
    score = judge.score(
        q,
        "EmailCapture saveTimeBill 工时填写 run_query_v2 " * 3,
        title="EmailCapture 工时填写 run_query_v2",
        page_path="wiki/concepts/EmailCapture-run_query_v2.md",
        context=["EmailCapture saveTimeBill 工时填写"],
    )
    assert 0.0 <= score <= 1.0


def test_local_llm_fallback_forwards_title_path() -> None:
    fallback = HeuristicEvidenceJudge()
    judge = LocalLlmEvidenceJudge(fallback=fallback, scorer=None)
    q = "email capture"
    direct = fallback.score(
        q, "capture settings", title="Email Capture", page_path="wiki/concepts/email-capture.md"
    )
    wrapped = judge.score(
        q, "capture settings", title="Email Capture", page_path="wiki/concepts/email-capture.md"
    )
    assert wrapped == direct


def test_judge_path_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a, **_k):  # noqa: ANN001
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    judge = HeuristicEvidenceJudge()
    assert (
        judge.score(
            "suiteql 工时",
            "SuiteQL 工时 examples",
            title="SuiteQL",
            page_path="wiki/concepts/suiteql.md",
        )
        >= 0.0
    )
```

Keep the existing overlap / context / LLM-raise tests; do not delete them. If `test_judge_path_makes_no_network_calls` already exists, **replace** its body with the richer call above (same name).

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_sag2_evidence.py -v
```

Expected: FAIL on the new bonus tests (`score()` unexpected keyword `title` / ranking assertions), or collection/TypeError until signature is extended.

- [ ] **Step 3: Implement bonuses + wire hop call site**

Replace / extend `src/retrieval/sag2_evidence.py` as follows (full module target):

```python
"""Local-only evidence judges for SAG2 hop (no hosted JEV, no vault egress)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]+", re.I)
_CJK_SPAN = re.compile(r"[一-鿿]{2,}")
_CAMEL = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")
_IDENT_LOOSE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b|\b[A-Za-z]+[0-9][A-Za-z0-9_]*\b")
_QUESTION_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "with", "吗", "呢", "的", "了", "是", "什么",
        "怎么", "如何",
    }
)

_TITLE_BONUS_WEIGHT = 0.25
_CJK_BONUS_PER_HIT = 0.10
_CJK_BONUS_CAP = 0.20
_IDENTIFIER_BONUS = 0.15


class EvidenceJudge(Protocol):
    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
        title: str = "",
        page_path: str = "",
    ) -> float:
        """Return evidence score in ``[0, 1]`` for ``passage_text`` given ``question``."""


def _tokens(text: str) -> set[str]:
    return {
        tok.casefold()
        for tok in _TOKEN.findall(text)
        if tok.casefold() not in _QUESTION_STOP and len(tok) > 1
    }


def _path_tokens(page_path: str) -> set[str]:
    stem = page_path.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    pieces = re.split(r"[/_\\-]+", page_path)
    pieces.append(stem)
    out: set[str] = set()
    for piece in pieces:
        out |= _tokens(piece)
    return out


def _identifier_terms(question: str) -> set[str]:
    found = {m.group(0) for m in _CAMEL.finditer(question)}
    found |= {m.group(0) for m in _IDENT_LOOSE.finditer(question)}
    return {term for term in found if len(term) > 1}


class HeuristicEvidenceJudge:
    """Coverage / phrase / context overlap heuristic (default local judge)."""

    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
        title: str = "",
        page_path: str = "",
    ) -> float:
        q = _tokens(question)
        if not q and not _CJK_SPAN.search(question) and not _identifier_terms(question):
            return 0.0
        p = _tokens(passage_text)
        covered = (len(q & p) / len(q)) if q else 0.0
        phrase_hit = 0.0
        lowered_p = passage_text.casefold()
        if len(q) >= 2 and sum(1 for t in q if t in lowered_p) >= 2:
            phrase_hit = 0.15
        ctx_bonus = 0.0
        if context:
            ctx_toks: set[str] = set()
            for block in context:
                ctx_toks |= _tokens(block)
            if ctx_toks and q:
                ctx_bonus = 0.1 * (len(q & ctx_toks) / len(q))

        title_toks = _tokens(title) | _path_tokens(page_path)
        title_bonus = 0.0
        if q and title_toks:
            title_bonus = _TITLE_BONUS_WEIGHT * (len(q & title_toks) / len(q))

        haystack = f"{title}\n{page_path}\n{passage_text}"
        cjk_hits = 0
        for span in dict.fromkeys(_CJK_SPAN.findall(question)):
            if span in haystack:
                cjk_hits += 1
        cjk_bonus = min(_CJK_BONUS_CAP, _CJK_BONUS_PER_HIT * cjk_hits)

        id_bonus = 0.0
        id_terms = _identifier_terms(question)
        if id_terms:
            lowered_hay = haystack.casefold()
            if any(term.casefold() in lowered_hay for term in id_terms):
                id_bonus = _IDENTIFIER_BONUS

        return max(0.0, min(1.0, covered + phrase_hit + ctx_bonus + title_bonus + cjk_bonus + id_bonus))


ScorerFn = Callable[[str, str, Sequence[str]], float]


class LocalLlmEvidenceJudge:
    """Optional local scorer with mandatory heuristic fallback; never HTTP."""

    def __init__(
        self,
        *,
        fallback: EvidenceJudge,
        scorer: ScorerFn | None = None,
    ) -> None:
        self._fallback = fallback
        self._scorer = scorer

    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
        title: str = "",
        page_path: str = "",
    ) -> float:
        if self._scorer is None:
            return self._fallback.score(
                question, passage_text, context=context, title=title, page_path=page_path
            )
        try:
            value = float(self._scorer(question, passage_text, context))
        except Exception:
            return self._fallback.score(
                question, passage_text, context=context, title=title, page_path=page_path
            )
        if value != value:  # NaN
            return 0.0
        return max(0.0, min(1.0, value))
```

Update the hop call site in `src/retrieval/sag2_hop.py` (`ensure_scored`):

```python
            try:
                score = float(
                    self._judge.score(
                        question,
                        hit.text,
                        context=context,
                        title=hit.title,
                        page_path=hit.page_path,
                    )
                )
            except Exception:
                score = 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_sag2_evidence.py tests/retrieval/test_sag2_hop.py -v
```

Expected: PASS (existing hop tests still green; new bonus tests green).

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/sag2_evidence.py src/retrieval/sag2_hop.py tests/retrieval/test_sag2_evidence.py
git add tests/retrieval/test_sag2_hop.py 2>/dev/null || true
git commit -m "$(cat <<'EOF'
feat(retrieval): add title/CJK/identifier bonuses to HeuristicEvidenceJudge

EOF
)"
```

---

### Task 2: Weak raw merge (empty still replace) + telemetry

**Files:**
- Modify: `src/retrieval/query_recovery.py` — add `wiki_hop_pool_kind`
- Modify: `src/retrieval/query_execution_context.py` — merge vs replace + `sag2_raw_fallback_mode`
- Modify: `src/retrieval/query_pipeline.py` — project `raw_fallback_mode`
- Test: `tests/retrieval/test_query_recovery.py` (unit for pool kind)
- Test: `tests/retrieval/test_sag2_pipeline_default.py` (integration merge/replace/telemetry)

**Interfaces:**
- Consumes: `wiki_hop_is_weak` threshold 0.15; `plan_raw_single_shot`; `recall_policy.merge_weak_primary_raw`; `select_best_per_page`; `adaptive_expand`.
- Produces:
  - `wiki_hop_pool_kind(selected, evidence_scores) -> Literal["empty","weak","strong"]`
  - `QueryExecutionContext.sag2_raw_fallback_mode: str` with values `"none"` | `"raw_merge"` | `"raw_replace"` (default `"none"`)
  - Same field on the frozen / execution view used by `assemble_public_projection`
  - `pipeline["sag2_hop"]["raw_fallback_mode"]` string
  - `pipeline["sag2_hop"]["raw_single_shot"]` remains `True` when mode is `raw_merge` or `raw_replace`

**Merge / replace rules (exact):**

| `wiki_hop_pool_kind` | raw FTS non-empty | Result set | `raw_fallback_mode` |
|----------------------|-------------------|------------|---------------------|
| `empty` | yes | `select_best_per_page(raw_items)` then `adaptive_expand` → **replace** `self.selected` | `raw_replace` |
| `weak` | yes | `merge_weak_primary_raw(self.selected, raw_items)` then `select_best_per_page` if needed / `adaptive_expand` → **keep wiki rows** | `raw_merge` |
| `strong` | n/a (do not call raw) | unchanged | `none` |
| any | raw empty / unavailable | leave wiki selected as-is when weak; empty stays empty + existing empty envelopes | stay `raw_merge`/`raw_replace` only if you already set the flag before search — **prefer:** set mode only when raw_items produced a selection change; if raw unavailable/empty and wiki was weak, set mode to `raw_merge` intent still? Spec wants telemetry when raw triggers. Keep today's behavior: set `sag2_raw_single_shot=True` when planning fires; set `raw_fallback_mode` to the **intended** branch (`raw_merge` vs `raw_replace`) as soon as the branch is chosen (even if raw returns empty), so tests can assert mode without depending on raw hit count. |

Fallback reasons stay `wiki_hop_empty` / `wiki_hop_weak` as today.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_query_recovery.py — append
from retrieval.query_recovery import wiki_hop_pool_kind


def test_wiki_hop_pool_kind_empty_weak_strong() -> None:
    class Hit:
        def __init__(self, passage_id: str) -> None:
            self.passage_id = passage_id

    weak_item = {"hit": Hit("p1"), "evidence_score": 0.05}
    strong_item = {"hit": Hit("p2"), "evidence_score": 0.40}
    assert wiki_hop_pool_kind([], {}) == "empty"
    assert wiki_hop_pool_kind([weak_item], {"p1": 0.05}) == "weak"
    assert wiki_hop_pool_kind([strong_item], {"p2": 0.40}) == "strong"
    # Boundary: exactly 0.15 is strong (weak is strictly < 0.15).
    assert wiki_hop_pool_kind([{"hit": Hit("p3")}], {"p3": 0.15}) == "strong"
```

```python
# tests/retrieval/test_sag2_pipeline_default.py — append
from pathlib import Path

from retrieval.query_pipeline import run_query_v2
from tests.helpers import write_test_page
from wiki.wiki_index import refresh_indexes
from wiki.wiki_paths import create_wiki_root


def test_raw_empty_pool_still_replace(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "access reports", retrieval_mode="lexical")
    assert result["results"]
    assert result["results"][0]["path"] == "raw/sources/file/default/help.md"
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_single_shot"] is True
    assert sag2["raw_fallback_mode"] == "raw_replace"


def test_raw_weak_pool_merges_and_keeps_wiki(tmp_path: Path) -> None:
    """Weak wiki evidence must survive raw single-shot (merge, not replace)."""
    root = tmp_path / "vault"
    create_wiki_root(root)
    # Wiki page: only weak overlap with the question (one shared token).
    write_test_page(
        root,
        "wiki/concepts/report-stub.md",
        {"title": "Stub Notes", "type": "concept"},
        "reports overview without the access verb.",
    )
    raw = root / "raw/sources/file/default/help.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\ntitle: Reports Help\n---\n\nAccess reports from the Reports menu.",
        encoding="utf-8",
    )
    refresh_indexes(root)

    result = run_query_v2(root, "access reports", retrieval_mode="lexical", top_k=5)
    assert result["ok"] is True
    paths = [item["path"] for item in result["results"]]
    kinds = [item.get("source_kind") for item in result["results"]]
    # Wiki stub must remain somewhere in the returned / additional pool or results.
    all_paths = paths + [item["path"] for item in result.get("additional_results", [])]
    assert "wiki/concepts/report-stub.md" in all_paths
    assert any(k == "raw" or str(p).startswith("raw/") for p, k in zip(paths, kinds)) or any(
        str(p).startswith("raw/") for p in all_paths
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_single_shot"] is True
    assert sag2["raw_fallback_mode"] == "raw_merge"
    # Must not look like a ladder branch.
    assert result["pipeline"]["coverage"]["triggered"] is False


def test_pipeline_sag2_telemetry_includes_raw_fallback_mode(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_test_page(
        root,
        "wiki/concepts/email-capture.md",
        {"title": "Email Capture", "type": "concept"},
        "Email capture plug-in implementation uses the Email Capture Plug-in interface.",
    )
    refresh_indexes(root)
    result = run_query_v2(root, "email capture", top_k=3)
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_fallback_mode"] in {"none", "raw_merge", "raw_replace"}
```

Update existing `test_pipeline_includes_sag2_hop_telemetry` required key set to include `"raw_fallback_mode"`.

**Fixture note:** If the weak-merge integration test does not trigger `wiki_hop_weak` on the first try (Judge bonuses may push the stub ≥ 0.15), force weakness by monkeypatching:

```python
    import retrieval.query_recovery as recovery

    monkeypatch.setattr(recovery, "wiki_hop_is_weak", lambda selected, evidence: bool(selected))
    monkeypatch.setattr(
        recovery,
        "wiki_hop_pool_kind",
        lambda selected, evidence: "weak" if selected else "empty",
    )
```

Prefer a real weak fixture first; use the monkeypatch only if the natural fixture is flaky after Task 1 bonuses. Document which path landed in the commit message.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_query_recovery.py::test_wiki_hop_pool_kind_empty_weak_strong \
  tests/retrieval/test_sag2_pipeline_default.py -v
```

Expected: FAIL — missing `wiki_hop_pool_kind` / missing `raw_fallback_mode` / weak path still replace-only.

- [ ] **Step 3: Implement pool kind + merge branch + telemetry**

Add to `src/retrieval/query_recovery.py`:

```python
from typing import Any, Literal, Mapping, Sequence

WikiHopPoolKind = Literal["empty", "weak", "strong"]


def wiki_hop_pool_kind(
    selected: Sequence[Mapping[str, Any]],
    evidence_scores: Mapping[str, float],
) -> WikiHopPoolKind:
    """Classify wiki+hop pool for raw single-shot branching."""

    if not selected:
        return "empty"
    top = max(
        (float(evidence_scores.get(item["hit"].passage_id, 0.0)) for item in selected),
        default=0.0,
    )
    if top < 0.15:
        return "weak"
    return "strong"
```

Keep `wiki_hop_is_weak` as a thin wrapper for compatibility:

```python
def wiki_hop_is_weak(
    selected: Sequence[Mapping[str, Any]],
    evidence_scores: Mapping[str, float],
) -> bool:
    """True when wiki+hop selected is empty or top evidence is below 0.15."""

    return wiki_hop_pool_kind(selected, evidence_scores) != "strong"
```

In `query_execution_context.py`:

1. Add field `sag2_raw_fallback_mode: str = "none"` on the mutable context **and** the frozen/execution view dataclass (mirror how `sag2_raw_single_shot` is threaded — copy into `execute()` return and `_frozen_view`).
2. Rewrite the selection tail of `_run_raw_single_shot` (after `raw_items` is non-empty) roughly as:

```python
        from retrieval.query_recovery import wiki_hop_pool_kind

        kind = wiki_hop_pool_kind(self.selected, evidence_scores)
        self.sag2_raw_fallback_mode = "raw_replace" if kind == "empty" else "raw_merge"
        self.sag2_raw_single_shot = True

        if kind == "empty":
            selected = select_best_per_page(raw_items)
            selected = recall_policy.adaptive_expand(selected, request_view.top_k)[
                : request_view.top_k
            ]
            recovery = assemble_recovery(
                selected,
                condition=RecoveryCondition("raw", (reason,), "item"),
                candidates=raw_items,
                store=self.store,
                raw_store=raw_store,
                cancellation=self.cancellation,
            )
        else:
            # weak: merge, preserve wiki rows
            merged = recall_policy.merge_weak_primary_raw(self.selected, raw_items)
            selected = select_best_per_page(merged)
            selected = recall_policy.adaptive_expand(selected, request_view.top_k)[
                : request_view.top_k
            ]
            recovery = assemble_recovery(
                selected,
                condition=RecoveryCondition("raw", (reason,), "item"),
                candidates=merged,
                store=self.store,
                raw_store=raw_store,
                cancellation=self.cancellation,
            )

        self.selected = selected
        self.context_items = recovery.context_items
        self.recovery = recovery
        self.lexical_mode = "strict"
        self.coverage_fallback = False
```

Set `sag2_raw_fallback_mode` earlier when the branch is chosen (right after computing `reason`), and reset to `"none"` at the start of `_run_raw_single_shot` / `_run_fallback_recovery` like `sag2_raw_single_shot`.

Import `wiki_hop_pool_kind` at module top alongside `wiki_hop_is_weak` (avoid inline import in final code).

In `query_pipeline.py` projection:

```python
        "sag2_hop": {
            "hops_run": view.sag2_hops_run,
            "judge_calls": view.sag2_judge_calls,
            "expansion_term_count": len(view.sag2_expansion_terms),
            "stop_reason": view.sag2_stop_reason,
            "truncated": view.sag2_truncated,
            "raw_single_shot": view.sag2_raw_single_shot,
            "raw_fallback_mode": view.sag2_raw_fallback_mode,
        },
```

Ensure `run_query_v2` copies hop evidence onto selected items **before** execution so `evidence_scores` in `_run_raw_single_shot` is non-zero when hop ran (already true via `item["evidence_score"]` — do not regress).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_query_recovery.py::test_wiki_hop_pool_kind_empty_weak_strong \
  tests/retrieval/test_sag2_pipeline_default.py \
  tests/retrieval/test_sag2_evidence.py -v
```

Expected: PASS. Also run a broader smoke:

```bash
uv run pytest tests/retrieval -q --tb=line
```

Expected: all previously green retrieval tests still pass (fix any legacy assumptions that weak raw always replace — update those tests to expect merge).

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_recovery.py src/retrieval/query_execution_context.py \
  src/retrieval/query_pipeline.py \
  tests/retrieval/test_query_recovery.py tests/retrieval/test_sag2_pipeline_default.py
git commit -m "$(cat <<'EOF'
feat(retrieval): merge raw into weak sag2 pools instead of replace

EOF
)"
```

---

### Task 3: CHANGELOG note

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:** none.

- [ ] **Step 1: Edit CHANGELOG under the v1.0.0 / 2026-11-09 section**

Add a bullet block (Chinese, matching existing tone) after the hop-default bullets:

```markdown
## 2026-11-09 — v1.0.0：SAG2 召回回升（弱池 raw merge + Judge 加权）

- **弱池 raw：** wiki+hop 候选非空但 top 证据分 `< 0.15` 时，单次 raw FTS **merge** 并入（复用 `merge_weak_primary_raw`），**不再**整池 replace；**空池**仍 replace。
- **Judge 加权：** `HeuristicEvidenceJudge` 对标题/路径 token、CJK 连续片段、标识符式 token 增加有界 bonus，分数仍 clamp 到 `[0,1]`；hop 调用传入 `PassageHit.title` / `page_path`。
- **Telemetry：** `pipeline.sag2_hop.raw_fallback_mode` = `none` | `raw_merge` | `raw_replace`；`raw_single_shot` 在 merge/replace 时仍为 `true`。
- **不做：** 不恢复默认 P2 ladder；本轮不含 TermSelector 问句稀有专名保留（Approach C）。
- **验收：** codingwork gold 同口径 **R@10 ≥ 0.80**；未达标须显式记录，不得静默宣称成功。详设 `docs/specs/2026-11-09-sag2-recall-lift-merge-judge-design.md`。
```

Keep the earlier “SAG2 hop 默认主路径” section; this is an additive subsection on the same version line.

- [ ] **Step 2: Commit**

```bash
git add -f CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: note sag2 recall-lift merge + judge bonuses in CHANGELOG

EOF
)"
```

---

### Task 4: Eval smoke — codingwork gold R@10 ≥ 0.80 (fail loudly)

**Files:**
- Modify (optional results note): `docs/plans/2026-11-09-sag2-recall-lift-merge-judge.md` (this file) — append a “Task 4 results” table after the run.
- No application code.

**Interfaces:** none new. Uses existing CLI `llm-wiki-mcp retrieval-eval`.

**Baseline to beat:** LU-C / box note on `v1.0.0` hop-default: codingwork gold **R@10 ≈ 0.6655** (29 cases). **Gate: R@10 ≥ 0.80**.

- [ ] **Step 1: Unit smoke (always on box)**

```bash
uv run pytest \
  tests/retrieval/test_sag2_evidence.py \
  tests/retrieval/test_sag2_hop.py \
  tests/retrieval/test_sag2_pipeline_default.py \
  tests/retrieval/test_query_recovery.py::test_wiki_hop_pool_kind_empty_weak_strong \
  -q
```

Expected: all PASS.

- [ ] **Step 2: Broader retrieval regression (informational)**

```bash
uv run pytest tests/retrieval -q --tb=line
```

Expected: no new failures vs pre-task HEAD.

- [ ] **Step 3: Codingwork gold eval (gate)**

On this box the prior hop-default run left artifacts at `/tmp/sag2-eval/`:

- Vault: `/tmp/sag2-eval/codingwork-vault`
- Dataset: `/tmp/sag2-eval/codingwork-gold.jsonl`
- Manifest: `/tmp/sag2-eval/codingwork-gold.manifest.json`

If those paths exist and the vault index is queryable, run:

```bash
mkdir -p /tmp/sag2-eval/reports-recall-lift
uv run llm-wiki-mcp retrieval-eval \
  --vault /tmp/sag2-eval/codingwork-vault \
  --dataset /tmp/sag2-eval/codingwork-gold.jsonl \
  --manifest /tmp/sag2-eval/codingwork-gold.manifest.json \
  --output-dir /tmp/sag2-eval/reports-recall-lift \
  --top-k 10 \
  --retrieval-mode lexical \
  --entrypoint engine \
  --query-version v2
```

Extract macro R@10 from `/tmp/sag2-eval/reports-recall-lift/retrieval-eval.json` (field path historically under `metrics` — use:

```bash
python3 - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("/tmp/sag2-eval/reports-recall-lift/retrieval-eval.json").read_text())
metrics = report["metrics"]
# Prefer explicit keys used by this codebase's report writer:
r10 = (
    metrics.get("recall_at_10")
    or metrics.get("macro_recall_at_10")
    or metrics.get("recall@10")
    or metrics.get("R@10")
)
# Fall back: nested ranking block if present
if r10 is None and isinstance(metrics.get("ranking"), dict):
    r10 = metrics["ranking"].get("recall_at_10") or metrics["ranking"].get("macro_recall_at_10")
print("R@10=", r10)
print("metrics_keys=", sorted(metrics.keys()))
if r10 is None:
    raise SystemExit("FAIL_LOUD: could not locate R@10 in report metrics")
if float(r10) < 0.80:
    raise SystemExit(f"FAIL_LOUD: R@10={r10} < 0.80 (baseline~0.6655)")
print("PASS: R@10 gate met")
PY
```

**Fail-loudly criterion (mandatory):**

- If R@10 `< 0.80` → treat Task 4 as **failed gate**: write the measured number, baseline `0.6655`, delta, and commit hash into the results note; **do not** claim the recall-lift succeeded; leave application code as-is for a follow-up (Approach C / threshold / merge ranking) — do **not** silently re-enable P2 ladder.
- If `/tmp/sag2-eval/codingwork-vault` (or dataset) is **missing** on the box → do **not** invent numbers. Record in the results note: `SKIPPED_NO_VAULT` + the exact command above for LU-C; unit/regression steps still must pass. Parent/user runs gold on LU-C before merging if box cannot.

**Same-protocol note:** Drop `codingwork-0029` only if those two gold raw paths are still missing (same as hop-default run). Prefer running the full JSONL and letting the eval harness report missing paths; document case count actually scored.

- [ ] **Step 4: Append results note to this plan + commit if docs changed**

```markdown
#### Task 4 results note (YYYY-MM-DD UTC+8)

| Run | Cases | R@10 macro | Notes |
| --- | --- | --- | --- |
| Unit smoke (Task 1–2 tests) | … | — | … |
| `tests/retrieval` | … | — | … |
| codingwork-wiki-query-gold | … | … | PASS ≥0.80 / FAIL_LOUD / SKIPPED_NO_VAULT |
```

```bash
git add -f docs/plans/2026-11-09-sag2-recall-lift-merge-judge.md
git commit -m "$(cat <<'EOF'
docs: record sag2 recall-lift eval smoke results

EOF
)" || true
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Weak pool raw **merge** (keep wiki) | Task 2 |
| Empty pool raw **replace** | Task 2 |
| Telemetry `raw_merge` vs `raw_replace` | Task 2 (`raw_fallback_mode`) |
| Judge title/path bounded bonus | Task 1 |
| Judge CJK span bounded bonus | Task 1 |
| Judge identifier-like bounded bonus | Task 1 |
| Clamp `[0,1]`; local-only / no HTTP | Task 1 (tests) |
| Do not restore P2 ladder / Approach C | Global Constraints + Task 2/4 notes |
| Unit contracts: merge / replace / Judge ranking | Tasks 1–2 |
| codingwork gold R@10 ≥ 0.80; fail loudly if not | Task 4 |
| CHANGELOG behavior note | Task 3 |

**Placeholder scan:** No TBD/TODO. Bonus caps, threshold `0.15`, file paths, pytest commands, and eval CLI are concrete. Weak-fixture monkeypatch is an explicit fallback, not an open design hole.

**Type consistency:** `score(..., title=, page_path=)` used in Task 1 hop wiring and tests; `wiki_hop_pool_kind` → `"empty"|"weak"|"strong"`; `raw_fallback_mode` → `"none"|"raw_merge"|"raw_replace"` across context / projection / tests.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-11-09-sag2-recall-lift-merge-judge.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (`superpowers:subagent-driven-development`).
2. **Inline Execution** — execute tasks in this session with `superpowers:executing-plans` and checkpoints.

Which approach?

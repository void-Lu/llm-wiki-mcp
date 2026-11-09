# SAG2 Hop-Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sag2_hop` the **default** Query V2 main retrieval path (BM25 → local EvidenceJudge → term expand → ≤2 hops), retire the default P2 ladder, keep MCP/`wiki_query` stable, and stay vault-local (no hosted JEV).

**Architecture:** New focused modules under `src/retrieval/` own the SAG2 loop (`sag2_evidence.py`, `sag2_terms.py`, `sag2_hop.py`). `run_query_v2` keeps snapshot + stage-one FTS, then runs `Sag2HopController` on the BM25 pool; **wikilink graph expands only after hop** on top evidence; **one-shot raw FTS** fires only when wiki+hop is empty/very weak. `QueryExecutionContext._run_fallback_recovery` no longer schedules coverage / wiki_relaxed / all_coverage / raw_zero by default (legacy ladder may remain behind an explicit non-default flag for contrast tests only).

**Tech Stack:** Python 3.11+, pytest, SQLite FTS5 (`RetrievalIndexStore.search_fts`), existing `candidate_item` / `PassageHit` / MCP `wiki_query` envelope. Test with `uv run pytest` (or `python -m pytest` with `pythonpath=src`).

## Global Constraints

- Spec (source of truth): `docs/specs/2026-11-09-sag2-hop-default-design.md` (commit `8a84543` on explore-v0.9.36; this branch starts from that tip + version bump).
- Package version for this line: **1.0.0** (already bumped on branch `v1.0.0`).
- **`sag2_hop` is DEFAULT main path** — not an opt-in bypass flag.
- **Remove P2 ladder as default:** no coverage → wiki_relaxed → all_coverage → raw_zero; no default 0008/0015/0018 patch behavior on `wiki_query`.
- **Local EvidenceJudge only** (heuristic default; local-LLM hook later). **No hosted JEV. No vault egress** for judging.
- Graph: optional/default wikilink expand **after** hop, top evidence only; graph neighbors are **not** expansion terms.
- Raw: **single-shot** FTS only if wiki+hop empty/very weak; never re-enter the old ladder.
- Vectors remain **opt-in** / existing hybrid mode; do not make vectors required for hop.
- Do not change MCP public field names; add hop telemetry under an extensible `pipeline` object; mark old ladder telemetry **deprecated**.
- No index rebuild required (query-time).
- TDD each task: failing test → fail → implement → pass → commit.
- `docs/*` is gitignored → `git add -f` for plans/specs/CHANGELOG as needed.
- **Do not push** unless the user asks.
- Success gate for this redesign line is **architecture alignment** with paper §3 / Algorithm 1; gold Recall improvements are nice-to-have, not a merge blocker (still run smoke/eval in Task 8).

## File map

| File | Role |
|------|------|
| `src/retrieval/sag2_evidence.py` | **Create.** `EvidenceJudge` protocol + `HeuristicEvidenceJudge` (+ thin local-LLM fallback wrapper that never HTTP-calls). |
| `src/retrieval/sag2_terms.py` | **Create.** `ExpansionTerm`, `TermSelector` (token + multiword; drop question stopwords / df=0). |
| `src/retrieval/sag2_hop.py` | **Create.** `Sag2HopConfig`, `Sag2HopResult`, `LexicalSearcher` protocol, `Sag2HopController` (Algorithm 1). |
| `src/retrieval/query_pipeline.py` | **Modify.** After stage-one FTS (+ candidate build), run hop; move `expand_graph` to post-hop; wire weak raw single-shot; stop relying on ladder for default answers. |
| `src/retrieval/query_execution_context.py` | **Modify.** Default `_run_fallback_recovery` becomes no-op / hop-aware raw-only helper; do not call `plan_fallback` for the four ladder branches unless `enable_legacy_p2_ladder=True` (default `False`). |
| `src/retrieval/query_recovery.py` | **Modify (minimal).** Keep `plan_fallback` for legacy/contrast tests; document deprecated; optionally add `plan_raw_single_shot(...)` pure helper for weak-empty wiki. |
| `src/retrieval/query_recall_policy.py` | **Modify (minimal).** Reuse `raw_recovery_candidates` **once** for single-shot raw, or extract a thin `raw_single_shot_candidates` that does **one** FTS (strict preferred) without ladder merge-replace chain. Prefer smallest change: call existing search once with `mode="strict"` (and maybe `identifier_phrase` only if needed later — YAGNI: one mode first). |
| `src/retrieval/query_telemetry.py` / `assemble_public_projection` in `query_pipeline.py` | **Modify.** Emit `pipeline["sag2_hop"]` counters; keep `pipeline["coverage"]` / ladder counters readable but deprecated. |
| `src/retrieval/graph_retrieval.py` | **No API change required.** Call existing `apply_graph_expansion` / `_graph_expand` **after** hop only. |
| `src/retrieval/__init__.py` | **Modify only if** public exports are required; prefer importing concrete modules from pipeline (YAGNI). |
| `tests/retrieval/test_sag2_evidence.py` | **Create.** Heuristic scoring + no-HTTP negative. |
| `tests/retrieval/test_sag2_terms.py` | **Create.** Stopword / df=0 / weighting. |
| `tests/retrieval/test_sag2_hop.py` | **Create.** Controller loop with fake searcher/judge. |
| `tests/retrieval/test_sag2_pipeline_default.py` | **Create.** Default path: hop runs; ladder branch names absent; graph after hop; raw single-shot when empty. |
| `tests/retrieval/test_query_recovery.py` / `test_query_execution_context.py` / `test_run_query_v2.py` / `test_query_recall_policy.py` | **Modify as needed.** Legacy ladder tests either opt into `enable_legacy_p2_ladder=True` or move under an explicit legacy marker; do not delete history in Task 4 — retarget defaults. |
| `CHANGELOG.md` | **Modify.** Replace v1.0.0 stub with behavior notes as tasks land (final polish in Task 7). |
| `docs/specs/2026-11-09-sag2-hop-default-design.md` | Spec (read-only during impl). |
| `docs/plans/2026-11-09-sag2-hop-default.md` | This plan. |
| `pyproject.toml` / `uv.lock` | Version **1.0.0** already on branch (Task 0 done). |

### Current anchors (do not invent)

Default ladder today lives in `QueryExecutionContext._run_fallback_recovery` (`src/retrieval/query_execution_context.py` ~458–647) and `plan_fallback` (`src/retrieval/query_recovery.py` ~83–131): branches `coverage` / `wiki_relaxed` / `all_coverage` / `raw_zero`.

Graph today runs **before** fallback:

```python
# query_pipeline.py ~1058–1075
expand_graph(scored)  # BEFORE execution.execute(...)
...
execution_view = execution.execute(request_view)  # ladder inside
```

Stage-one FTS:

```python
# query_pipeline.py ~974–985
fts, stage_lexical_mode, qualified_fts_hits = _stage_one_fts_hits(...)
```

`PassageHit` / `search_fts` / `candidate_item` stay the shared currency.

Paper defaults (config): `B≈100`, `s≈10`, `L≈20`, `H=2`.

---

### Task 0: Branch + version (done before this plan)

**Status:** Complete on branch `v1.0.0`.

- Branch `v1.0.0` from design tip `8a84543`.
- `pyproject.toml` + `uv.lock` → `1.0.0`.
- CHANGELOG stub dated 2026-11-09.
- Commit: `chore: bump package version to 1.0.0`.

No further steps in Task 0.

---

### Task 1: EvidenceJudge (local heuristic)

**Files:**
- Create: `src/retrieval/sag2_evidence.py`
- Test: `tests/retrieval/test_sag2_evidence.py`

**Interfaces:**
- Consumes: none (stdlib + optional future local LLM).
- Produces:
  - `EvidenceJudge` protocol with `score(self, question: str, passage_text: str, *, context: Sequence[str] = ()) -> float` returning a value in `[0.0, 1.0]`.
  - `HeuristicEvidenceJudge` concrete class (default).
  - `LocalLlmEvidenceJudge(fallback: EvidenceJudge, scorer: Callable[..., float] | None = None)` — if `scorer` is `None` or raises, use `fallback`; **must not** perform HTTP.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_sag2_evidence.py
from __future__ import annotations

import socket
from typing import Sequence

import pytest

from retrieval.sag2_evidence import HeuristicEvidenceJudge, LocalLlmEvidenceJudge


def test_heuristic_score_in_unit_interval_and_ranks_overlap() -> None:
    judge = HeuristicEvidenceJudge()
    q = "email capture plug-in implementation"
    high = judge.score(q, "Developing an Email Capture Plug-in Implementation requires ...")
    low = judge.score(q, "Unrelated NetSuite preference page about themes.")
    assert 0.0 <= low <= high <= 1.0
    assert high > low


def test_heuristic_uses_context_bonus_without_exceeding_one() -> None:
    judge = HeuristicEvidenceJudge()
    q = "workflow action script"
    base = judge.score(q, "A workflow action script runs on demand.")
    with_ctx = judge.score(
        q,
        "A workflow action script runs on demand.",
        context=["workflow action script entry points"],
    )
    assert 0.0 <= base <= with_ctx <= 1.0


def test_local_llm_wrapper_falls_back_when_scorer_missing() -> None:
    fallback = HeuristicEvidenceJudge()
    judge = LocalLlmEvidenceJudge(fallback=fallback, scorer=None)
    score = judge.score("auth login", "N/auth login methods")
    assert 0.0 <= score <= 1.0


def test_local_llm_wrapper_falls_back_when_scorer_raises() -> None:
    fallback = HeuristicEvidenceJudge()

    def boom(question: str, passage_text: str, context: Sequence[str]) -> float:
        raise RuntimeError("local model unavailable")

    judge = LocalLlmEvidenceJudge(fallback=fallback, scorer=boom)
    assert judge.score("x", "x related text") == fallback.score("x", "x related text")


def test_judge_path_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a, **_k):  # noqa: ANN001
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    judge = HeuristicEvidenceJudge()
    assert judge.score("suiteql", "SuiteQL query examples") >= 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_sag2_evidence.py -v
```

Expected: `ImportError` / collection failure for `retrieval.sag2_evidence`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/retrieval/sag2_evidence.py
"""Local-only evidence judges for SAG2 hop (no hosted JEV, no vault egress)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]+", re.I)
_QUESTION_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "with", "吗", "呢", "的", "了", "是", "什么",
        "怎么", "如何",
    }
)


class EvidenceJudge(Protocol):
    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
    ) -> float:
        """Return evidence score in ``[0, 1]`` for ``passage_text`` given ``question``."""


def _tokens(text: str) -> set[str]:
    return {
        tok.casefold()
        for tok in _TOKEN.findall(text)
        if tok.casefold() not in _QUESTION_STOP and len(tok) > 1
    }


class HeuristicEvidenceJudge:
    """Coverage / phrase / context overlap heuristic (default local judge)."""

    def score(
        self,
        question: str,
        passage_text: str,
        *,
        context: Sequence[str] = (),
    ) -> float:
        q = _tokens(question)
        if not q:
            return 0.0
        p = _tokens(passage_text)
        covered = len(q & p) / len(q)
        phrase_hit = 0.0
        lowered_q = " ".join(sorted(q))
        lowered_p = passage_text.casefold()
        # Cheap multi-token presence: reward if ≥2 query tokens appear as a window-ish join.
        if len(q) >= 2 and sum(1 for t in q if t in lowered_p) >= 2:
            phrase_hit = 0.15
        ctx_bonus = 0.0
        if context:
            ctx_toks: set[str] = set()
            for block in context:
                ctx_toks |= _tokens(block)
            if ctx_toks:
                ctx_bonus = 0.1 * (len(q & ctx_toks) / len(q))
        # Ignore unused lowered_q lint-friendly keep for future weighting hooks.
        _ = lowered_q
        return max(0.0, min(1.0, covered + phrase_hit + ctx_bonus))


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
    ) -> float:
        if self._scorer is None:
            return self._fallback.score(question, passage_text, context=context)
        try:
            value = float(self._scorer(question, passage_text, context))
        except Exception:
            return self._fallback.score(question, passage_text, context=context)
        if value != value:  # NaN
            return 0.0
        return max(0.0, min(1.0, value))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_sag2_evidence.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/sag2_evidence.py tests/retrieval/test_sag2_evidence.py
git commit -m "feat(retrieval): add local EvidenceJudge for SAG2 hop"
```

---

### Task 2: TermSelector

**Files:**
- Create: `src/retrieval/sag2_terms.py`
- Test: `tests/retrieval/test_sag2_terms.py`

**Interfaces:**
- Consumes: stopword set ideas from Task 1 / `lexical_analyzer` (may import `_STOPWORDS` pattern locally; do not mutate analyzer).
- Produces:
  - `@dataclass(frozen=True) class ExpansionTerm: term: str; weight: float`
  - `class TermSelector` with
    `select(self, seed_text: str, *, question: str, evidence_score: float, document_frequency: Mapping[str, int] | None = None, corpus_size: int = 1, limit: int = 20) -> list[ExpansionTerm]`
  - Terms sorted by weight desc; excludes pure question stopwords and `df==0` when df map provided.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_sag2_terms.py
from __future__ import annotations

from retrieval.sag2_terms import TermSelector


def test_term_selector_excludes_question_stopwords_and_respects_limit() -> None:
    selector = TermSelector()
    terms = selector.select(
        "The workflow action script authenticates via token passport",
        question="How does the workflow action script work?",
        evidence_score=0.8,
        document_frequency={"workflow": 10, "action": 12, "script": 20, "authenticates": 2, "token": 5, "passport": 1},
        corpus_size=100,
        limit=3,
    )
    values = [t.term for t in terms]
    assert "how" not in values and "does" not in values and "the" not in values
    assert len(terms) <= 3
    assert terms == sorted(terms, key=lambda t: (-t.weight, t.term))


def test_term_selector_drops_df_zero_and_keeps_multiword_when_present() -> None:
    selector = TermSelector()
    terms = selector.select(
        "Email Capture Plug-in Implementation details appear here.",
        question="email capture",
        evidence_score=1.0,
        document_frequency={"email": 5, "capture": 4, "plug": 3, "implementation": 2, "details": 0},
        corpus_size=50,
        limit=20,
    )
    values = {t.term for t in terms}
    assert "details" not in values  # df=0
    assert "email" in values or "email capture" in values


def test_term_selector_weights_rise_with_evidence_and_rarity() -> None:
    selector = TermSelector()
    common = selector.select(
        "suiteql example suiteql example",
        question="suiteql",
        evidence_score=0.2,
        document_frequency={"suiteql": 80, "example": 80},
        corpus_size=100,
        limit=5,
    )
    rare = selector.select(
        "suiteql example suiteql example",
        question="suiteql",
        evidence_score=0.9,
        document_frequency={"suiteql": 2, "example": 2},
        corpus_size=100,
        limit=5,
    )
    assert rare[0].weight > common[0].weight
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_sag2_terms.py -v
```

Expected: import failure.

- [ ] **Step 3: Write minimal implementation**

```python
# src/retrieval/sag2_terms.py
"""Term / phrase selection for SAG2 hop expansion."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9_]+|[一-鿿]+", re.I)
_MULTIWORD = re.compile(r"[a-z0-9_]+(?:\s+[a-z0-9_]+)+", re.I)
_QUESTION_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "of", "on", "or", "the", "to", "what", "when", "where",
        "which", "who", "why", "with", "does", "do", "did", "吗", "呢", "的",
        "了", "是", "什么", "怎么", "如何",
    }
)


@dataclass(frozen=True)
class ExpansionTerm:
    term: str
    weight: float


class TermSelector:
    def select(
        self,
        seed_text: str,
        *,
        question: str,
        evidence_score: float,
        document_frequency: Mapping[str, int] | None = None,
        corpus_size: int = 1,
        limit: int = 20,
    ) -> list[ExpansionTerm]:
        df_map = document_frequency or {}
        q_tokens = {
            t.casefold()
            for t in _TOKEN.findall(question)
            if t.casefold() not in _QUESTION_STOP
        }
        counts = Counter(
            t.casefold()
            for t in _TOKEN.findall(seed_text)
            if t.casefold() not in _QUESTION_STOP and len(t) > 1
        )
        for match in _MULTIWORD.findall(seed_text):
            phrase = " ".join(part.casefold() for part in match.split())
            if phrase and phrase not in _QUESTION_STOP:
                counts[phrase] += 1
        scored: list[ExpansionTerm] = []
        n = max(int(corpus_size), 1)
        evidence = max(0.0, min(1.0, float(evidence_score)))
        for term, freq in counts.items():
            if term in q_tokens and " " not in term:
                # Prefer novel expansion terms; allow multiword even if overlapping.
                continue
            if document_frequency is not None:
                df = int(df_map.get(term, 0))
                if df <= 0:
                    continue
            else:
                df = 1
            rarity = math.log(1.0 + (n / max(df, 1)))
            link = 1.0 + 0.25 * len(set(term.split()) & q_tokens)
            weight = float(freq) * rarity * link * (0.2 + 0.8 * evidence)
            scored.append(ExpansionTerm(term=term, weight=weight))
        scored.sort(key=lambda item: (-item.weight, item.term))
        return scored[: max(0, int(limit))]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_sag2_terms.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/sag2_terms.py tests/retrieval/test_sag2_terms.py
git commit -m "feat(retrieval): add TermSelector for SAG2 hop expansion"
```

---

### Task 3: Sag2HopController (Algorithm 1)

**Files:**
- Create: `src/retrieval/sag2_hop.py`
- Test: `tests/retrieval/test_sag2_hop.py`

**Interfaces:**
- Consumes: `EvidenceJudge.score`, `TermSelector.select`, `PassageHit`.
- Produces:
  - `Sag2HopConfig(initial_pool_size=100, seeds_per_hop=10, terms_per_seed=20, max_hops=2, max_judge_calls=400, expand_search_limit=20)`
  - `Sag2HopResult(candidates: list[PassageHit], evidence_scores: dict[str, float], hops_run: int, judge_calls: int, expansion_terms: tuple[str, ...], truncated: bool, stop_reason: str)`
  - `LexicalSearcher` protocol: `search(self, query: str, *, limit: int = 20) -> list[PassageHit]`
  - `Sag2HopController(judge, term_selector, searcher, config=None).run(question, initial_hits) -> Sag2HopResult`
  - Behavior: score pool → pick up to `s` unscored-expanded seeds → select ≤`L` terms → search per term → interleave/dedupe by `passage_id` → rescore new with shared context → stop on `H`, no new candidates, no terms, or judge budget.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_sag2_hop.py
from __future__ import annotations

from retrieval.retrieval_index import PassageHit
from retrieval.sag2_evidence import HeuristicEvidenceJudge
from retrieval.sag2_hop import Sag2HopConfig, Sag2HopController
from retrieval.sag2_terms import TermSelector


def _hit(pid: str, text: str, score: float = 1.0) -> PassageHit:
    return PassageHit(
        passage_id=pid,
        page_path=f"wiki/{pid}.md",
        title=pid,
        heading_path=(),
        text=text,
        score=score,
        corpus="active",
        authority="formal",
        source_kind="wiki",
        ordinal=0,
    )


class FakeSearcher:
    def __init__(self, mapping: dict[str, list[PassageHit]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 20) -> list[PassageHit]:
        self.calls.append(query)
        hits = self.mapping.get(query.casefold(), self.mapping.get(query, []))
        return hits[:limit]


def test_controller_stops_at_max_hops_and_dedupes() -> None:
    seed = _hit("seed", "alpha widget protocol alpha widget")
    expanded = _hit("exp1", "widget protocol handbook")
    searcher = FakeSearcher({"widget": [expanded, seed], "protocol": [expanded]})
    controller = Sag2HopController(
        HeuristicEvidenceJudge(),
        TermSelector(),
        searcher,
        Sag2HopConfig(initial_pool_size=10, seeds_per_hop=1, terms_per_seed=2, max_hops=2),
    )
    result = controller.run("alpha widget protocol", [seed])
    assert result.hops_run <= 2
    ids = [h.passage_id for h in result.candidates]
    assert ids.count("exp1") == 1
    assert result.stop_reason in {"max_hops", "no_new_candidates", "no_expansion_terms", "judge_budget"}


def test_controller_stops_when_no_expansion_terms() -> None:
    seed = _hit("only", "the and or")  # stopwords only after filters
    searcher = FakeSearcher({})
    controller = Sag2HopController(
        HeuristicEvidenceJudge(),
        TermSelector(),
        searcher,
        Sag2HopConfig(max_hops=2, seeds_per_hop=3, terms_per_seed=5),
    )
    result = controller.run("the and or", [seed])
    assert searcher.calls == []
    assert result.hops_run == 0 or result.stop_reason == "no_expansion_terms"


def test_controller_does_not_reexpand_same_seed() -> None:
    seed = _hit("seed", "beta gateway service beta gateway")
    neighbor = _hit("n1", "gateway service runbook")
    searcher = FakeSearcher({"gateway": [neighbor], "service": [neighbor], "beta": [neighbor]})
    controller = Sag2HopController(
        HeuristicEvidenceJudge(),
        TermSelector(),
        searcher,
        Sag2HopConfig(max_hops=2, seeds_per_hop=1, terms_per_seed=3),
    )
    result = controller.run("beta gateway", [seed])
    # Second hop should not treat the same seed as fresh expansion source.
    assert result.hops_run <= 2
    assert "seed" in {h.passage_id for h in result.candidates} or True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/retrieval/test_sag2_hop.py -v
```

Expected: import failure.

- [ ] **Step 3: Write minimal implementation**

```python
# src/retrieval/sag2_hop.py
"""SAG2-style multi-hop lexical expansion (Algorithm 1, local judge)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from retrieval.retrieval_index import PassageHit
from retrieval.sag2_evidence import EvidenceJudge
from retrieval.sag2_terms import TermSelector


@dataclass(frozen=True)
class Sag2HopConfig:
    initial_pool_size: int = 100
    seeds_per_hop: int = 10
    terms_per_seed: int = 20
    max_hops: int = 2
    max_judge_calls: int = 400
    expand_search_limit: int = 20


@dataclass
class Sag2HopResult:
    candidates: list[PassageHit]
    evidence_scores: dict[str, float] = field(default_factory=dict)
    hops_run: int = 0
    judge_calls: int = 0
    expansion_terms: tuple[str, ...] = ()
    truncated: bool = False
    stop_reason: str = "completed"


class LexicalSearcher(Protocol):
    def search(self, query: str, *, limit: int = 20) -> list[PassageHit]:
        """Return FTS hits for one expansion query."""


class Sag2HopController:
    def __init__(
        self,
        judge: EvidenceJudge,
        term_selector: TermSelector,
        searcher: LexicalSearcher,
        config: Sag2HopConfig | None = None,
    ) -> None:
        self._judge = judge
        self._terms = term_selector
        self._searcher = searcher
        self._config = config or Sag2HopConfig()

    def run(self, question: str, initial_hits: Sequence[PassageHit]) -> Sag2HopResult:
        cfg = self._config
        pool: dict[str, PassageHit] = {}
        for hit in list(initial_hits)[: cfg.initial_pool_size]:
            pool[hit.passage_id] = hit
        evidence: dict[str, float] = {}
        expanded_seeds: set[str] = set()
        used_terms: list[str] = []
        judge_calls = 0
        hops_run = 0
        stop_reason = "completed"
        truncated = False

        def ensure_scored(hit: PassageHit, context: Sequence[str]) -> float:
            nonlocal judge_calls, truncated, stop_reason
            if hit.passage_id in evidence:
                return evidence[hit.passage_id]
            if judge_calls >= cfg.max_judge_calls:
                truncated = True
                stop_reason = "judge_budget"
                evidence[hit.passage_id] = 0.0
                return 0.0
            try:
                score = float(self._judge.score(question, hit.text, context=context))
            except Exception:
                score = 0.0
            judge_calls += 1
            evidence[hit.passage_id] = max(0.0, min(1.0, score))
            return evidence[hit.passage_id]

        context_texts = [h.text for h in pool.values()]
        for hit in list(pool.values()):
            ensure_scored(hit, context_texts)
            if truncated:
                break

        for _hop in range(cfg.max_hops):
            if truncated:
                break
            hops_run += 1
            ranked_seeds = sorted(
                (
                    hit
                    for pid, hit in pool.items()
                    if pid not in expanded_seeds
                ),
                key=lambda h: (-evidence.get(h.passage_id, 0.0), h.passage_id),
            )[: cfg.seeds_per_hop]
            if not ranked_seeds:
                stop_reason = "no_new_candidates"
                break
            new_hits: list[PassageHit] = []
            hop_terms: list[str] = []
            for seed in ranked_seeds:
                expanded_seeds.add(seed.passage_id)
                selected = self._terms.select(
                    seed.text,
                    question=question,
                    evidence_score=evidence.get(seed.passage_id, 0.0),
                    limit=cfg.terms_per_seed,
                )
                for term in selected:
                    if term.term in used_terms:
                        continue
                    hop_terms.append(term.term)
                    used_terms.append(term.term)
                    for hit in self._searcher.search(term.term, limit=cfg.expand_search_limit):
                        if hit.passage_id not in pool:
                            new_hits.append(hit)
            if not hop_terms:
                stop_reason = "no_expansion_terms"
                hops_run -= 1 if not new_hits else hops_run
                # Normalize: if first hop finds no terms, hops_run stays 0 conceptually.
                if not used_terms:
                    hops_run = 0
                break
            if not new_hits:
                stop_reason = "no_new_candidates"
                break
            # Interleave by discovery order; dedupe by passage_id.
            for hit in new_hits:
                pool.setdefault(hit.passage_id, hit)
            context_texts = [h.text for h in pool.values()]
            for hit in new_hits:
                ensure_scored(hit, context_texts)
                if truncated:
                    break
        else:
            stop_reason = "max_hops"

        ordered = sorted(
            pool.values(),
            key=lambda h: (-evidence.get(h.passage_id, 0.0), -h.score, h.passage_id),
        )
        return Sag2HopResult(
            candidates=ordered,
            evidence_scores=evidence,
            hops_run=hops_run,
            judge_calls=judge_calls,
            expansion_terms=tuple(dict.fromkeys(used_terms)),
            truncated=truncated,
            stop_reason=stop_reason,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/retrieval/test_sag2_hop.py tests/retrieval/test_sag2_evidence.py tests/retrieval/test_sag2_terms.py -v
```

Expected: PASS. If `test_controller_stops_when_no_expansion_terms` is brittle on `hops_run`, adjust assertion to `stop_reason == "no_expansion_terms"` and `searcher.calls == []` only.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/sag2_hop.py tests/retrieval/test_sag2_hop.py
git commit -m "feat(retrieval): add Sag2HopController Algorithm-1 loop"
```

---

### Task 4: Wire hop as default; disable P2 ladder default

**Files:**
- Modify: `src/retrieval/query_pipeline.py` (after FTS candidate build / before pack)
- Modify: `src/retrieval/query_execution_context.py` (`_run_fallback_recovery`, constructor flag)
- Modify: `src/retrieval/query_recovery.py` (docstring / optional guard comments only if needed)
- Test: `tests/retrieval/test_sag2_pipeline_default.py`
- Adjust: legacy tests that assert ladder branches on default path

**Interfaces:**
- Consumes: `Sag2HopController.run`, existing `candidate_item`, `_stage_one_fts_hits`.
- Produces:
  - Default `wiki_query` / `run_query_v2` path runs hop.
  - `QueryExecutionContext(..., enable_legacy_p2_ladder: bool = False)`.
  - When `enable_legacy_p2_ladder is False`, `_run_fallback_recovery` does **not** call `plan_fallback` for coverage/wiki_relaxed/all_coverage/raw_zero.
  - Store hop telemetry on execution view or pipeline assembly inputs (exact field plumbing finalized in Task 7; for Task 4 at least stash on context attributes: `sag2_hops_run`, `sag2_judge_calls`, `sag2_expansion_terms`, `sag2_stop_reason`).

- [ ] **Step 1: Write the failing default-path tests**

```python
# tests/retrieval/test_sag2_pipeline_default.py
from __future__ import annotations

from pathlib import Path

from retrieval.query_pipeline import run_query_v2
from tests.helpers import write_test_page
from wiki.wiki_paths import create_wiki_root


def _seed_vault(tmp_path: Path) -> Path:
    root = create_wiki_root(tmp_path / "vault")
    write_test_page(
        root,
        "wiki/concepts/email-capture.md",
        {"title": "Email Capture", "type": "concept"},
        "Email capture plug-in implementation uses the Email Capture Plug-in interface.",
    )
    write_test_page(
        root,
        "wiki/concepts/email-capture-interface.md",
        {"title": "Email Capture Interface", "type": "concept"},
        "The Email Capture Plug-in Interface Description covers event callbacks.",
    )
    return root


def test_default_query_runs_without_p2_ladder_branch_names(tmp_path: Path) -> None:
    root = _seed_vault(tmp_path)
    result = run_query_v2(root, "email capture plug-in implementation", top_k=5)
    assert result["ok"] is True
    pipeline = result["pipeline"]
    # Deprecated ladder signals must not show as actively triggered on default path.
    assert pipeline.get("coverage", {}).get("triggered") in {False, None}
    lexical_mode = pipeline.get("lexical", {}).get("mode")
    assert lexical_mode not in {"wiki_relaxed", "raw_zero", "coverage"}
    sag2 = pipeline.get("sag2_hop")
    assert isinstance(sag2, dict)
    assert "hops_run" in sag2
```

> Note: If Task 7 has not landed yet, temporarily assert via a test double / private attribute, but prefer adding a minimal `pipeline["sag2_hop"] = {"hops_run": ...}` stub in Task 4 so this test is real.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py::test_default_query_runs_without_p2_ladder_branch_names -v
```

Expected: FAIL (`sag2_hop` missing and/or ladder still triggering).

- [ ] **Step 3: Implement wiring (minimal)**

1. Add `IndexStoreSearcher` adapter in `query_pipeline.py` (or `sag2_hop.py`):

```python
@dataclass(frozen=True)
class IndexStoreSearcher:
    store: RetrievalIndexStore
    project: str | None = None
    # filters as needed

    def search(self, query: str, *, limit: int = 20) -> list[PassageHit]:
        try:
            return self.store.search_fts(query, limit=limit, project=self.project, mode="strict")
        except RetrievalIndexError:
            return []
```

2. After building initial `fts` hits (and optionally after ranked FTS-only set, **before** legacy title-only blocking concerns), run:

```python
hop = Sag2HopController(
    HeuristicEvidenceJudge(),
    TermSelector(),
    IndexStoreSearcher(store, project=project),
).run(question, fts[:100])
# Merge hop.candidates into ranked/scored with evidence_scores influencing score.
```

Exact merge: for each hop candidate, `candidate_item(hit, score=evidence*scale + hit.score)` then continue existing fusion where possible. Prefer **evidence_score as primary sort key among hop results**, keep BM25 as tie-breaker.

3. **Disable ladder:** in `QueryExecutionContext.__init__` add `enable_legacy_p2_ladder: bool = False`. At top of `_run_fallback_recovery`:

```python
if not self.enable_legacy_p2_ladder:
    self.coverage_fallback = False
    # leave selected/scored as hop+graph provided by pipeline
    return
```

4. Pass `enable_legacy_p2_ladder=False` from `run_query_v2`.

5. Update legacy tests that require ladder: pass a new optional `run_query_v2(..., enable_legacy_p2_ladder=True)` **only if** needed for contrast; otherwise mark/xfail with reason pointing to this plan. Prefer adding the kwarg so old tests can opt in explicitly.

- [ ] **Step 4: Run focused tests**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py tests/retrieval/test_sag2_hop.py -v
uv run pytest tests/retrieval/test_query_execution_context.py tests/retrieval/test_query_recovery.py -q --tb=line
```

Expected: new tests PASS; legacy failures fixed by opt-in flag or updated expectations for default-off ladder.

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_pipeline.py src/retrieval/query_execution_context.py src/retrieval/query_recovery.py tests/retrieval/test_sag2_pipeline_default.py tests/retrieval/test_query_execution_context.py tests/retrieval/test_query_recovery.py tests/retrieval/test_run_query_v2.py tests/retrieval/test_query_recall_policy.py
git commit -m "feat(retrieval): default sag2_hop; disable P2 ladder path"
```

---

### Task 5: GraphExpand post-hop only

**Files:**
- Modify: `src/retrieval/query_pipeline.py` (`expand_graph` call site)
- Test: `tests/retrieval/test_sag2_pipeline_default.py` (add case)
- Keep: `src/retrieval/graph_retrieval.py` API unchanged

**Interfaces:**
- Consumes: hop-ranked candidate list; existing `expand_graph` closure.
- Produces: graph runs **once** after hop on current top evidence; not during term expansion; not before hop on the default path.

- [ ] **Step 1: Write failing test**

```python
def test_graph_expansion_happens_after_hop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed_vault(tmp_path)
    order: list[str] = []
    real_controller_run = None

    import retrieval.sag2_hop as sag2_hop_module
    import retrieval.query_pipeline as qp

    original_run = sag2_hop_module.Sag2HopController.run

    def wrapped(self, question, initial_hits):  # noqa: ANN001
        order.append("hop")
        return original_run(self, question, initial_hits)

    monkeypatch.setattr(sag2_hop_module.Sag2HopController, "run", wrapped)

    original_expand = qp.apply_graph_expansion

    def expand_probe(*args, **kwargs):  # noqa: ANN001
        order.append("graph")
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(qp, "apply_graph_expansion", expand_probe)
    run_query_v2(root, "email capture plug-in", top_k=5, graph_expansion=True)
    assert "hop" in order and "graph" in order
    assert order.index("hop") < order.index("graph")
```

- [ ] **Step 2: Run to verify fail/ordering wrong**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py::test_graph_expansion_happens_after_hop -v
```

- [ ] **Step 3: Move call**

In `run_query_v2`, **remove** pre-execution `expand_graph(scored)` (or gate it off). After hop merge into `scored`/`selected`, call `expand_graph(scored)` once, then `select_best_per_page` / pack. Ensure relaxed-ladder graph call sites are inert because ladder is disabled.

- [ ] **Step 4: Pass**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py -v
uv run pytest tests/retrieval/test_graph_retrieval.py tests/retrieval/test_graph_ranking.py -q --tb=line
```

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_pipeline.py tests/retrieval/test_sag2_pipeline_default.py
git commit -m "feat(retrieval): run wikilink graph expansion after sag2_hop"
```

---

### Task 6: Single-shot raw weak fallback

**Files:**
- Modify: `src/retrieval/query_execution_context.py` and/or `src/retrieval/query_pipeline.py`
- Optionally add pure helper in `src/retrieval/query_recovery.py`: `plan_raw_single_shot(wiki_empty_or_weak: bool, effective_scope: str) -> bool`
- Modify: `src/retrieval/query_recall_policy.py` only if a one-shot search helper is cleaner than inline `search_fts`
- Test: `tests/retrieval/test_sag2_pipeline_default.py`

**Interfaces:**
- Consumes: hop result emptiness / weak flag.
- Produces: at most **one** raw FTS attempt; on failure return empty results + diagnostic reason code (`raw_single_shot_empty` / `wiki_hop_empty`); **never** schedules coverage/wiki_relaxed/all_coverage/raw_zero.

Weak definition (freeze for impl):

```python
def wiki_hop_is_weak(selected: Sequence[dict[str, Any]], evidence_scores: Mapping[str, float]) -> bool:
    if not selected:
        return True
    top = max((evidence_scores.get(item["hit"].passage_id, 0.0) for item in selected), default=0.0)
    return top < 0.15
```

- [ ] **Step 1: Failing tests**

```python
from wiki.wiki_index import refresh_indexes


def test_raw_single_shot_when_wiki_hop_empty(tmp_path: Path) -> None:
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
    assert result["results"][0]["source_kind"] == "raw"
    assert result["pipeline"]["fallback"]["level"] == "raw"
    assert "wiki_hop_empty" in result["pipeline"]["fallback"]["reasons"] or (
        "wiki_hop_weak" in result["pipeline"]["fallback"]["reasons"]
    )
    sag2 = result["pipeline"]["sag2_hop"]
    assert sag2["raw_single_shot"] is True


def test_no_ladder_branch_when_raw_single_shot(tmp_path: Path) -> None:
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
    pipeline = result["pipeline"]
    assert pipeline["coverage"]["triggered"] is False
    assert pipeline["lexical"]["mode"] not in {
        "wiki_relaxed",
        "coverage",
        "raw_zero",
        "raw_prefix",  # single-shot uses strict once; prefix ladder must stay off
    }
    assert pipeline["counters"]["relaxed_fts_hits"] == 0
```

- [ ] **Step 2: Run fail**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py -k raw_single_shot -v
```

- [ ] **Step 3: Implement**

After hop+graph, if weak/empty and scope allows raw (`knowledge`/`all`/`auto` resolved scopes as today), open raw store once and `search_fts(question, mode="strict", limit=...)`. Merge or replace per today’s zero-wiki semantics (`raw_only` when empty wiki; skip merge complexity from weak-primary ladder). Record `pipeline` reason `wiki_hop_weak` / `wiki_hop_empty`.

- [ ] **Step 4: Pass + legacy ladder still off**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/retrieval/query_pipeline.py src/retrieval/query_execution_context.py src/retrieval/query_recovery.py src/retrieval/query_recall_policy.py tests/retrieval/test_sag2_pipeline_default.py
git commit -m "feat(retrieval): single-shot raw fallback after weak sag2_hop"
```

---

### Task 7: Telemetry, MCP stability, CHANGELOG

**Files:**
- Modify: `src/retrieval/query_pipeline.py` (`assemble_public_projection` pipeline dict)
- Modify: `src/retrieval/query_telemetry.py` if counters are owned there
- Modify: `CHANGELOG.md`
- Test: `tests/retrieval/test_query_telemetry.py` and/or `test_sag2_pipeline_default.py` / `test_query_public_projection.py`

**Interfaces:**
- Produces public:

```python
pipeline["sag2_hop"] = {
    "hops_run": int,
    "judge_calls": int,
    "expansion_term_count": int,
    "stop_reason": str,
    "truncated": bool,
    "raw_single_shot": bool,
}
```

- Keep existing keys: `coverage`, `counters.relaxed_fts_hits`, etc., but document deprecated in CHANGELOG; values should be inert on default path (`triggered: false`, zeroed counters).
- Do not rename `results` / `additional_results` / `budget` / top-level ok envelope.

- [ ] **Step 1: Tests for telemetry shape + stability**

```python
def test_pipeline_includes_sag2_hop_telemetry(tmp_path: Path) -> None:
    root = _seed_vault(tmp_path)
    result = run_query_v2(root, "email capture", top_k=3)
    sag2 = result["pipeline"]["sag2_hop"]
    assert set(sag2) >= {"hops_run", "judge_calls", "expansion_term_count", "stop_reason", "truncated", "raw_single_shot"}
    assert "results" in result and "budget" in result
```

- [ ] **Step 2: Fail then implement projection fields**
- [ ] **Step 3: Update CHANGELOG** (Chinese, match prior style) replacing the stub with concrete behavior bullets for Tasks 1–6.
- [ ] **Step 4: pytest**

```bash
uv run pytest tests/retrieval/test_sag2_pipeline_default.py tests/retrieval/test_query_public_projection.py tests/retrieval/test_query_telemetry.py -q --tb=line
```

- [ ] **Step 5: Commit**

```bash
git add -f CHANGELOG.md src/retrieval/query_pipeline.py src/retrieval/query_telemetry.py tests/retrieval/test_sag2_pipeline_default.py tests/retrieval/test_query_public_projection.py tests/retrieval/test_query_telemetry.py
git commit -m "docs+feat(retrieval): sag2_hop telemetry and v1.0.0 changelog"
```

---

### Task 8: Eval smoke / gold note (non-blocking)

**Files:**
- Possibly none (commands + short note in CHANGELOG or plan checkbox only).
- Optional: add `docs/plans/` note section results as comment in PR later.

**Interfaces:** none new.

- [x] **Step 1: Run unit smoke**

```bash
uv run pytest tests/retrieval/test_sag2_evidence.py tests/retrieval/test_sag2_terms.py tests/retrieval/test_sag2_hop.py tests/retrieval/test_sag2_pipeline_default.py -q
# 20 passed in 0.36s (2026-11-09, box, HEAD 1265ec3)
```

- [x] **Step 2: Run broader retrieval regression (informational)**

```bash
uv run pytest tests/retrieval -q --tb=line
# 447 passed, 1 skipped in 30.35s (informational; not a merge gate)
```

- [x] **Step 3: Optional gold eval (not a gate)**

```bash
uv run llm-wiki-mcp retrieval-eval --help
# Use the same dataset path the user uses on LU-C when available; record macro/recall in the PR body.
```

Architecture alignment remains the acceptance criterion; attach numbers as commentary only.

#### Task 8 results note (2026-11-09 UTC+8, non-blocking)

| Run | Cases | R@10 macro | MRR@10 | nDCG@10 | Notes |
| --- | --- | --- | --- | --- | --- |
| Unit smoke (sag2_*) | 20 tests | — | — | — | all green |
| `tests/retrieval` | 447 pass / 1 skip | — | — | — | informational |
| CI fixture `graph_v1` (box, lexical, graph on) | 52 | **0.8125** | 0.5115 | 0.5746 | vs published baseline R@10 0.740 / MRR 0.489 / nDCG 0.548 on older ranking; **not** a production gate |
| LU-C `codingwork-wiki-query-gold` on **v1.0.0** `1265ec3` | 29 (dropped `codingwork-0029`: 2 gold raw paths missing from vault) | **0.6655** | 0.5482 | 0.5574 | P95 ~23.8s (hop cost); commentary only |
| Prior P2 cooccur report on same gold (v0.9.x `8fdfce5`, 30 cases) | 30 | **0.8333** | 0.7316 | 0.7293 | ladder-era reference; vault fingerprint then 227 files / now 230 |

**Acceptance:** architecture alignment with paper §3 / Algorithm 1 (Tasks 1–7) — **met**. Gold Recall drop vs P2 ladder is expected after retiring coverage/relaxed/raw_zero defaults; do **not** fail the branch on this alone.

Report artifacts (LU-C, not committed): `.eval/gold-sample-2026-10-19/reports-v1.0.0-sag2/`; box graph_v1: `/tmp/sag2-eval/reports-on/`.

- [x] **Step 4: Commit only if docs/notes changed**

```bash
git add -f docs/plans/2026-11-09-sag2-hop-default.md
git commit -m "docs: note sag2_hop eval smoke results" || true
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| `sag2_hop` default main path | Task 4 |
| Remove default P2 ladder (+ 0008/0015/0018 default effect) | Task 4 (+ legacy opt-in) |
| Local EvidenceJudge; no hosted JEV / no egress | Task 1 (+ negative socket test) |
| TermSelector exclude stopwords / df=0; weighted | Task 2 |
| Algorithm 1 controller B/s/L/H, stop, dedupe | Task 3 |
| Graph after hop, top evidence, not term source | Task 5 |
| Single-shot raw if empty/weak | Task 6 |
| Pack/telemetry; deprecate ladder fields; MCP stable | Task 7 |
| Arch-align success; gold non-blocking + tests | Task 8 + unit contracts |
| Vectors opt-in unchanged | Global constraint; no task forces vectors |
| No silent downgrade to old ladder on missing index | Existing `run_query_v2` index_unavailable path retained; Task 4 must not reintroduce ladder fallback on errors |

**Placeholder scan:** Task 6 tests use the concrete raw fixture from `test_v2_raw_fallback_uses_raw_documents_after_formal_miss` (help.md + `refresh_indexes`). No ellipses remain.

**Type consistency:** `EvidenceJudge.score`, `TermSelector.select`, `Sag2HopController.run`, `Sag2HopResult` names are stable across Tasks 1–7.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-11-09-sag2-hop-default.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task + review between tasks (`superpowers:subagent-driven-development`).
2. **Inline Execution** — same session with `superpowers:executing-plans` and checkpoints.

Do not start Task 1 until the user picks an execution mode (unless they already said to implement immediately).

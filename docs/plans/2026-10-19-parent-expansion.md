# Parent Expansion (Small-to-Big) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Expand FTS-selected small passages with same-page ordinal neighbors before context packing, without schema changes, keeping citations on the hit passage.

**Architecture:** Add a pure `parent_expansion` module; look up neighbors via existing `page_path+ordinal` index; wire into `query_pipeline` before `pack_context`.

**Tech Stack:** Python 3, pytest, existing `retrieval_index` / `context_packer`.

## Global Constraints

- Work only on branch `v0.9.36` at `C:\Users\26327\VSCodeProjects\llm-wiki-mcp` (machine LU-C / machineId `95e422f3-b1c1-4770-a235-b777be61cebe`).
- No vector DB / embedding changes; no SQLite schema migration.
- Citations must remain on the original hit `passage_id`.
- TDD: failing tests first; run targeted pytest before claiming done.
- Prefer PowerShell-compatible shell commands on the Windows machine.
- Do not push unless asked; commit locally with a clear message when tasks complete.

---

## File map

| File | Role |
|------|------|
| `src/retrieval/parent_expansion.py` | **New** pure expand + budget + metadata |
| `src/retrieval/retrieval_index.py` | Add helper to fetch by `(page_path, ordinal)` if missing |
| `src/retrieval/query_pipeline.py` | Call expand before `pack_context` |
| `tests/retrieval/test_parent_expansion.py` | **New** unit tests |
| `docs/superpowers/specs/2026-10-19-parent-expansion-design.md` | Already written |

---

### Task 1: Neighbor fetch + pure expand (TDD)

**Files:**
- Create: `tests/retrieval/test_parent_expansion.py`
- Create: `src/retrieval/parent_expansion.py`
- Modify: `src/retrieval/retrieval_index.py` (optional small getter)

- [x] Write failing tests for: expand ±1; missing neighbors; `same_heading_only=True`; budget drops `+1` before hit; citation/`passage_id` unchanged; overlap stripped between parts
- [x] Run `python -m pytest tests/retrieval/test_parent_expansion.py -q` and confirm fail
- [x] Implement `expand_hit_texts(...)` (name as fits codebase style) returning expanded text + metadata
- [x] Add `RetrievalIndexStore` method e.g. `passages_at_ordinals(page_path, ordinals) -> list[PassageHit]` using existing index
- [x] Re-run tests until green
- [x] Commit: `feat(retrieval): add parent passage expansion helpers`

### Task 2: Wire into query pipeline

**Files:**
- Modify: `src/retrieval/query_pipeline.py`
- Modify: `tests/retrieval/test_parent_expansion.py` or a thin pipeline test if one exists that asserts packed content

- [x] Before `pack_context`, expand `public_context_items` / `ContextPassage` content using store lookup
- [x] Pass through expansion metadata into packed passage dicts or citation_metadata without breaking public MCP contracts
- [x] Default `expand_parents=True`, `parent_window=1`, `same_heading_only=False`
- [x] Add/adjust test proving packed body includes neighbor sentence while citation id is hit id
- [x] Run targeted pytest green
- [x] Commit: `feat(retrieval): expand parent context before pack_context`

### Task 3: Verify + docs touch

- [x] Run `python -m pytest tests/retrieval/test_parent_expansion.py tests/retrieval/test_context_packer.py -q`
- [x] If CHANGELOG is maintained for minor feats, add a short bullet under unreleased/v0.9.36
- [x] Report: files changed, test results, any follow-ups

---

## Done when

- Spec path exists; tests above pass on LU-C
- Query context pack can include ±1 neighbor text without schema rebuild
- Citations still reference hit passages
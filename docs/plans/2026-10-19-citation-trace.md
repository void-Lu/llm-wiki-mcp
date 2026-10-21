# Citation Trace Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD. Checkbox (`- [ ]`) tracking.

**Goal:** Attach additive `citation_trace` (+ per-result expansion/passage_id fields) to public query projections; optional JSON dump; never put passage bodies into query telemetry.

**Architecture:** Pure `citation_trace` builder; wire in `assemble_public_projection`; request flag for side-file dump under `.llm-wiki/citation-traces/`.

**Tech Stack:** Python 3, pytest, existing `redact_query` / parent expansion metadata.

## Global Constraints

- Branch `v0.9.36` at `C:\Users\26327\VSCodeProjects\llm-wiki-mcp` (machineId `95e422f3-b1c1-4770-a235-b777be61cebe`)
- Additive public fields only; no telemetry schema / vector / LLM judge work
- PowerShell-friendly commands; commit locally; do not push unless asked

## File map

| File | Role |
|------|------|
| `src/retrieval/citation_trace.py` | **New** build + optional dump |
| `src/retrieval/query_pipeline.py` | Attach trace; surface fields on results |
| `src/retrieval/query_execution_context.py` | `dump_citation_trace` on `QueryRequestView` |
| `tests/retrieval/test_citation_trace.py` | **New** unit + projection tests |
| `docs/specs/2026-10-19-citation-trace-design.md` | Design |
| `docs/plans/2026-10-19-citation-trace.md` | This plan |

---

### Task 1: Docs

- [x] Write design + plan; `git add -f` under ignored `docs/*`; commit docs

### Task 2: Pure builder + dump (TDD)

- [x] Failing tests for shape, no body keys, dump on/off
- [x] Implement `build_citation_trace` / `write_citation_trace_dump`
- [x] Green + commit helpers if split

### Task 3: Wire projection

- [x] Surface `passage_id` + expansion fields on results
- [x] Attach `citation_trace`; honor `dump_citation_trace`
- [x] Extend projection tests (incl. parent expansion case)
- [x] Targeted pytest green; commit

### Task 4: Report

- [x] Summarize files, tests, commits, deviations


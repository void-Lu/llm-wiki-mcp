# Small-to-Big Parent Expansion Design

**Date:** 2026-10-19  
**Branch:** v0.9.36  
**Status:** Approved for implementation

## Goal

After lexical/FTS retrieval ranks **small passages**, expand each selected hit with same-page neighbors (`ordinal ± window`) before `pack_context`, so the model reads a fuller parent block while citations still point at the **hit** passage.

No SQLite schema change. No vector-index dependency.

## Non-goals

- Embedding / vector recall changes
- Feedback loops or LLM-as-judge eval (later)
- Changing public MCP tool shapes beyond optional expansion metadata on packed context / traces

## Current baseline

- `passage_chunker.py`: heading-aware chunks with overlap; `ordinal` per page
- `retrieval_index.py`: `passages(page_path, ordinal)` index; `load_passages`, `PassageHit`
- `context_packer.py`: strips chunk overlap between packed neighbors; does **not** fetch unread ±1 neighbors for reading context
- `query_pipeline.py`: builds `ContextPassage` from hit text only, then `pack_context`

## Design

### Placement

New pure module `src/retrieval/parent_expansion.py`:

1. Input: selected hit records (at least `passage_id`, `page_path`, `ordinal`, `heading_path`, `text`, score/evidence fields needed downstream)
2. Neighbor lookup callback or store method: load passages for `(page_path, ordinal)` set
3. Output: expanded reading text per hit + metadata; **identity fields unchanged** (`passage_id`, path, heading for citation)

Wire in `query_pipeline.py` **after** public selection / quality gate, **before** `pack_context`.

### Neighbor rules

- Default `window=1` → try `ordinal-1` and `ordinal+1` on the same `page_path`
- `same_heading_only` (default `false` per approved option A): when `true`, drop neighbors whose `heading_path` ≠ hit
- Missing neighbor → skip; never invent text
- Strip chunker overlap between hit and neighbors using existing `strip_chunk_overlap` helpers where practical
- Cap: per-parent max chars/tokens and total expansion budget across all hits (constants documented in module); if over budget, prefer keeping hit body and drop farthest neighbor first (prefer keeping `-1` over `+1` if only one fits—document choice)

### Citation / trace

- Public citation still uses hit `passage_id` / path / heading
- Expansion metadata on packed passage or parallel trace field: `expanded: true`, `neighbor_ordinals: [...]`, `expansion_chars: N`
- Do not reassign citation to neighbor ids

### Multi-hit same page

- Expand each hit independently, then existing packer dedupe/overlap logic applies
- Avoid feeding the exact same neighbor text twice when two adjacent hits both expand into each other: packer + overlap strip should collapse; add a unit test

### Config

- Feature flag or kwargs on pipeline: `expand_parents: bool = True`, `parent_window: int = 1`, `same_heading_only: bool = False`
- Keep defaults on for query path that builds context packs; tests can disable

## Testing

- Unit tests in `tests/retrieval/test_parent_expansion.py` (TDD)
- Extend `test_context_packer` / pipeline tests only if wiring needs it
- Cases: ±1 join; missing edge ordinals; `same_heading_only`; budget trim; citation id stable; adjacent hits

## Rollout

- Land on `v0.9.36` with tests green
- No index rebuild required
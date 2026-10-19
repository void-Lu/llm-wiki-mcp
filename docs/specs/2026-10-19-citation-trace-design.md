# Citation / Retrieval Trace Design

**Date:** 2026-10-19  
**Branch:** v0.9.36  
**Status:** Approved for implementation

## Goal

On every successful public query projection, attach a structured **citation_trace** so operators and later eval can see: what was asked (redacted), which passages were selected, score families, retrieval mode signals, and parent-expansion metadata — without putting passage bodies into `query_telemetry`.

## Non-goals

- LLM-as-judge / gold-free scoring (next round)
- SQLite schema changes for telemetry
- Vector / embedding changes
- Breaking or renaming existing MCP public fields (additive only)

## Current baseline

- `assemble_public_projection` builds `results` with path/heading/score/scores; history-only `_citation_metadata` for provenance
- Parent expansion sets `ContextPassage.expanded` / `neighbor_ordinals` / `expansion_chars` but public `results` do not yet surface them
- `pipeline.debug` (request flag) lists ranking diagnostics; not a full citation field set
- `QueryTelemetry` stores redacted query + `passage_ids` only; `stores_content: false` by design
- `TelemetrySettings.store_query_body` exists but remains false; this work does not enable body storage

## Design

### Trace shape (public, additive)

Top-level response key `citation_trace`:

```json
{
  "query_redacted": "<redact_query(question)>",
  "scope": "<public_scope>",
  "intent": "<intent>",
  "retrieval_mode": "lexical|vector|hybrid",
  "lexical_mode": "<from execution view>",
  "hits": [
    {
      "citation": "[1]",
      "passage_id": "...",
      "path": "...",
      "heading": "...",
      "score": 1.2,
      "scores": {"fts": 0.0, "vector": 0.0, "rrf": 0.0, "graph": 0.0},
      "source_kind": "formal",
      "score_family": "main_rrf|...",
      "expanded": true,
      "neighbor_ordinals": [0, 2],
      "expansion_chars": 42
    }
  ]
}
```

Rules:

- Hits cover **public** `results` (top_k slice), same order as citations
- Trace hits **must not** include `content` / `text` / body fields
- `query_redacted` uses existing `redact_query` (secret scrub + whitespace normalize)
- Parent expansion fields come from the expansion map used before `pack_context`; if expansion skipped, `expanded=false`, empty neighbors, `expansion_chars=0`

### Per-result additive fields

Each item in `results` (and optionally `additional_results`) also gains:

- `passage_id`
- `expanded`
- `neighbor_ordinals`
- `expansion_chars`

Citation index/`path`/`heading` remain the hit identity (unchanged from parent-expansion design).

### Telemetry

- No change to `query_telemetry` columns or `record()` payload: still no passage body
- Tests assert telemetry insert / status still report `stores_content: false` and recorded rows lack body

### Optional debug dump

- Request flag `dump_citation_trace: bool = False` on `QueryRequestView` / `run_query_v2`
- When true: write `citation_trace` JSON under  
  `{vault}/.llm-wiki/citation-traces/{query_hash}.json`  
  (`query_hash` = sha256 of `query_redacted`, same family as telemetry)
- On success, set `response["citation_trace_path"]` to the vault-relative posix path
- Dump file must not contain passage bodies (same trace object)

### Placement

- New pure helpers in `src/retrieval/citation_trace.py`: `build_citation_trace`, `write_citation_trace_dump`
- Wire inside `assemble_public_projection` after results are built (has scores + expansion map)
- Keep `pipeline.debug` as-is (orthogonal ranking dump)

## Testing

- Unit: trace shape; no body keys; expansion fields present when expanded
- Projection: `results[].passage_id` / expansion fields; `citation_trace` present
- Telemetry: default path never persists body
- Dump: flag off → no file; flag on → file exists, no body keys, path in response

## Rollout

- Land on `v0.9.36` with tests green; local commit only unless asked to push

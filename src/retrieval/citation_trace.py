"""Build and optionally dump citation / retrieval traces (no passage bodies)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from retrieval.query_telemetry import redact_query

BODY_KEYS = frozenset({"content", "text", "body", "passage_text", "raw_text"})
CITATION_TRACE_DIR = Path(".llm-wiki") / "citation-traces"


def _score_family(item: Mapping[str, Any]) -> str:
    fusion = item.get("fusion_source")
    if isinstance(fusion, str) and fusion.strip():
        return fusion.strip()
    if item.get("rrf"):
        return "main_rrf"
    return "score"


def _expansion_fields(
    passage_id: str,
    expansion_by_passage_id: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    meta = expansion_by_passage_id.get(passage_id) if expansion_by_passage_id else None
    if not meta:
        return {
            "expanded": False,
            "neighbor_ordinals": [],
            "expansion_chars": 0,
        }
    neighbors = meta.get("neighbor_ordinals") or ()
    return {
        "expanded": bool(meta.get("expanded")),
        "neighbor_ordinals": [int(value) for value in neighbors],
        "expansion_chars": int(meta.get("expansion_chars") or 0),
    }


def build_citation_trace(
    *,
    question: str,
    public_scope: str,
    intent: str,
    retrieval_mode: str,
    lexical_mode: str,
    public_selected: Sequence[Mapping[str, Any]],
    expansion_by_passage_id: Mapping[str, Mapping[str, Any]] | None = None,
    heading_of: Callable[[Any], str],
) -> dict[str, Any]:
    """Assemble a body-free citation trace for the public result slice."""

    hits: list[dict[str, Any]] = []
    for index, item in enumerate(public_selected, 1):
        hit = item["hit"]
        expansion = _expansion_fields(hit.passage_id, expansion_by_passage_id)
        hits.append(
            {
                "citation": f"[{index}]",
                "passage_id": hit.passage_id,
                "path": hit.page_path,
                "heading": heading_of(hit),
                "score": float(item["score"]),
                "scores": {
                    "fts": float(hit.score),
                    "vector": float(item.get("vector_score") or 0.0),
                    "rrf": float(item.get("rrf") or 0.0),
                    "graph": float(item.get("graph_score") or 0.0),
                },
                "source_kind": hit.source_kind,
                "score_family": _score_family(item),
                **expansion,
            }
        )
    return {
        "query_redacted": redact_query(question),
        "scope": public_scope,
        "intent": intent,
        "retrieval_mode": retrieval_mode,
        "lexical_mode": lexical_mode,
        "hits": hits,
    }


def citation_trace_query_hash(query_redacted: str) -> str:
    return hashlib.sha256(query_redacted.encode()).hexdigest()


def write_citation_trace_dump(vault_root: str | Path, trace: Mapping[str, Any]) -> str:
    """Write *trace* under ``.llm-wiki/citation-traces/``; return vault-relative path."""

    root = Path(vault_root).expanduser().resolve()
    query_redacted = str(trace.get("query_redacted") or "")
    relative = CITATION_TRACE_DIR / f"{citation_trace_query_hash(query_redacted)}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(trace), ensure_ascii=False, indent=2, sort_keys=True)
    # Refuse to dump if a body key slipped in.
    parsed = json.loads(payload)
    flat_keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            flat_keys.update(value.keys())
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(parsed)
    if not BODY_KEYS.isdisjoint(flat_keys):
        raise ValueError("citation_trace dump must not include passage body fields")
    target.write_text(payload + "\n", encoding="utf-8")
    return relative.as_posix()


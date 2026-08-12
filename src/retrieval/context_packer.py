"""Compact, single-body context packing for passage query results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from retrieval.token_units import count_response_tokens


@dataclass(frozen=True)
class ContextPassage:
    passage_id: str
    path: str
    heading: str
    content: str
    score: float
    evidence_kind: str
    citation_metadata: Mapping[str, str] = field(default_factory=dict)


def estimate_response_tokens(value: str) -> int:
    """Count response-budget units; this scale is not a passage chunk limit."""

    return max(1, count_response_tokens(value)) if value else 0


def estimate_tokens(value: str) -> int:
    """Compatibility facade for callers of the former response estimator."""

    return estimate_response_tokens(value)


def _deduplicate_overlap(previous: str, current: str) -> str:
    if not previous or not current:
        return current
    previous_words = previous.split()
    current_words = current.split()
    maximum = min(len(previous_words), len(current_words), 48)
    for length in range(maximum, 0, -1):
        if previous_words[-length:] == current_words[:length]:
            return " ".join(current_words[length:])
    return current


def pack_context(
    passages: Iterable[ContextPassage],
    *,
    hard_limit: int,
    intent: str,
    budget_scale: int | None = None,
) -> dict[str, object]:
    """Pack page-ordered bodies for the canonical query response.

    ``budget_scale`` lets callers grow the budget with the number of requested
    results (for example 400 tokens per result).  The intent target remains a
    floor: a ``research`` question keeps its deep budget even with a small
    result count, while a wider result set scales the pack accordingly.
    """

    target = {"exact_entity": 2_000, "concept": 4_000, "comparison": 8_000, "research": 16_000}.get(intent, 4_000)
    if budget_scale is not None:
        target = max(target, budget_scale)
    budget = min(max(1, hard_limit), target)
    candidates = list(passages)
    output: list[dict[str, object]] = []
    citation_metadata: dict[str, dict[str, str]] = {}
    used = 0
    previous_by_path: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    last_tokens = 0
    for item in candidates:
        key = (item.path, item.content)
        if key in seen:
            continue
        seen.add(key)
        content = _deduplicate_overlap(previous_by_path.get(item.path, ""), item.content).strip()
        if not content:
            continue
        tokens = estimate_response_tokens(content)
        if used + tokens > budget:
            continue
        if output and output[-1]["path"] == item.path and output[-1]["heading"] == item.heading:
            output[-1]["content"] = f"{output[-1]['content']}\n\n{content}"
            output[-1]["tokens"] = last_tokens + tokens
            citation_metadata[str(output[-1]["citation"])].update(item.citation_metadata)
            previous_by_path[item.path] = f"{previous_by_path.get(item.path, '')} {content}".strip()
            last_tokens += tokens
            used += tokens
            continue
        citation = f"[{len(output) + 1}]"
        output.append({"citation": citation, "path": item.path, "heading": item.heading, "evidence_kind": item.evidence_kind, "content": content, "tokens": tokens})
        last_tokens = tokens
        citation_metadata[citation] = dict(item.citation_metadata)
        previous_by_path[item.path] = f"{previous_by_path.get(item.path, '')} {content}".strip()
        used += tokens
    citations = [
        {
            "citation": item["citation"],
            "path": item["path"],
            "heading": item["heading"],
            **({"metadata": citation_metadata[str(item["citation"])]} if citation_metadata[str(item["citation"])] else {}),
        }
        for item in output
    ]
    return {"passages": output, "citations": citations, "budget": {"target": target, "total": budget, "used": used, "omitted": max(0, sum(estimate_response_tokens(item.content) for item in candidates) - used)}}

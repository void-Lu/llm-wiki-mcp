"""Compact, single-body context packing for passage query results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from retrieval.body_budget import DEFAULT_INTENT_TARGET, INTENT_TARGETS
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

    Context is aggregated by path in one pass; ``seen`` and
    ``previous_by_path`` record budget-gated and overlap-deduplicated content,
    not grouping behavior.

    ``budget_scale`` lets callers grow the budget with the number of requested
    results (for example 400 tokens per result).  The intent target remains a
    floor: a ``research`` question keeps its deep budget even with a small
    result count, while a wider result set scales the pack accordingly.
    """

    target = INTENT_TARGETS.get(intent, DEFAULT_INTENT_TARGET)
    if budget_scale is not None:
        target = max(target, budget_scale)
    budget = min(max(1, hard_limit), target)
    candidates = list(passages)
    aggregated: dict[str, dict[str, object]] = {}
    order: list[str] = []
    used = 0
    previous_by_path: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
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
        path = str(item.path)
        entry = aggregated.get(path)
        if entry is None:
            aggregated[path] = {
                "path": path,
                "heading": item.heading,
                "evidence_kind": item.evidence_kind,
                "content": content,
                "tokens": tokens,
            }
            order.append(path)
        else:
            entry["content"] = f"{entry['content']}\n\n{content}"
            entry["tokens"] = int(entry["tokens"]) + tokens
        previous_by_path[item.path] = f"{previous_by_path.get(item.path, '')} {content}".strip()
        used += tokens

    retained_tokens = sum(int(aggregated[path]["tokens"]) for path in order)
    used = retained_tokens
    return {
        "passages": [aggregated[path] for path in order],
        "budget": {
            "target": target,
            "total": budget,
            "used": used,
            "omitted": max(
                0,
                sum(estimate_response_tokens(item.content) for item in candidates)
                - retained_tokens,
            ),
        },
    }

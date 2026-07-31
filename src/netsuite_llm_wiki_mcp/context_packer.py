"""Compact, single-body context packing for passage query results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_WORD_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class ContextPassage:
    passage_id: str
    path: str
    heading: str
    content: str
    score: float
    evidence_kind: str


def estimate_tokens(value: str) -> int:
    """Cheap stable estimator shared by response packing and telemetry."""
    return max(1, len(_WORD_RE.findall(value))) if value else 0


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


def pack_context(passages: Iterable[ContextPassage], *, hard_limit: int, intent: str) -> dict[str, object]:
    """Keep bodies only in ``context_pack.passages`` and obey a hard limit."""
    target = {"exact_entity": 2_000, "concept": 4_000, "comparison": 8_000, "research": 16_000}.get(intent, 4_000)
    budget = min(max(1, hard_limit), target)
    candidates = list(passages)
    output: list[dict[str, str | int]] = []
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
        tokens = estimate_tokens(content)
        if used + tokens > budget:
            continue
        if output and output[-1]["path"] == item.path and output[-1]["heading"] == item.heading:
            output[-1]["content"] = f"{output[-1]['content']}\n\n{content}"
            output[-1]["tokens"] = int(output[-1]["tokens"]) + tokens
            previous_by_path[item.path] = f"{previous_by_path.get(item.path, '')} {content}".strip()
            used += tokens
            continue
        citation = f"[{len(output) + 1}]"
        output.append({"citation": citation, "path": item.path, "heading": item.heading, "evidence_kind": item.evidence_kind, "content": content, "tokens": tokens})
        previous_by_path[item.path] = f"{previous_by_path.get(item.path, '')} {content}".strip()
        used += tokens
    citations = [{"citation": item["citation"], "path": item["path"], "heading": item["heading"]} for item in output]
    return {"passages": output, "citations": citations, "budget": {"target": target, "total": budget, "used": used, "omitted": max(0, sum(estimate_tokens(item.content) for item in candidates) - used)}}

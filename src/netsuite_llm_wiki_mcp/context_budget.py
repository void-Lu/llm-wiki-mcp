"""Context budget allocator for wiki query context packs.

Computes per-section token budgets from a total context window size.
Extracted as an independent module for testability and configurability.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONTEXT_TOKENS = 16_000
MIN_CONTEXT_TOKENS = 4_000
MAX_CONTEXT_TOKENS = 1_000_000

RESPONSE_RESERVE_FRAC = 0.15
INDEX_FRAC = 0.05
PAGES_FRAC = 0.50
CHAT_HISTORY_FRAC = 0.20
PER_PAGE_FRAC = 0.30
PER_PAGE_FLOOR = 1_200


@dataclass(frozen=True)
class ContextBudget:
    total: int
    response_reserve: int
    index_budget: int
    pages_budget: int
    chat_history_budget: int
    system_budget: int
    max_page_size: int


def compute_context_budget(
    context_window_tokens: int | None = None,
) -> ContextBudget:
    raw = context_window_tokens if context_window_tokens and context_window_tokens > 0 else DEFAULT_CONTEXT_TOKENS
    total = max(MIN_CONTEXT_TOKENS, min(MAX_CONTEXT_TOKENS, raw))

    response_reserve = int(total * RESPONSE_RESERVE_FRAC)
    index_budget = int(total * INDEX_FRAC)
    pages_budget = int(total * PAGES_FRAC)
    chat_history_budget = int(total * CHAT_HISTORY_FRAC)
    system_budget = total - response_reserve - index_budget - pages_budget - chat_history_budget

    max_page_size = min(
        pages_budget,
        max(PER_PAGE_FLOOR, int(pages_budget * PER_PAGE_FRAC)),
    )

    return ContextBudget(
        total=total,
        response_reserve=response_reserve,
        index_budget=index_budget,
        pages_budget=pages_budget,
        chat_history_budget=chat_history_budget,
        system_budget=system_budget,
        max_page_size=max_page_size,
    )

"""查询侧正文预算政策的唯一 owner。

本模块拥有查询响应使用的词元预算常量与公式。``wiki_get`` 的正文预算属于
字节预算域，由 ``content_catalog``（``DEFAULT_BODY_BUDGET`` /
``MAX_BODY_BUDGET``）负责；两个域不共享常量。
"""

from __future__ import annotations


PAGE_FILL_LIMIT = 500
PAGE_TOKEN_BUDGET = 2_400
PAGE_FULL_FILL_MIN_RATIO = 0.6
PAGE_WEAK_HIT_LIMIT = 3
BUDGET_PER_RESULT = 400

INTENT_TARGETS: dict[str, int] = {
    "exact_entity": 2_000,
    "concept": 4_000,
    "comparison": 8_000,
    "research": 16_000,
    "history": 4_000,
    "exact_evidence": 4_000,
}
DEFAULT_INTENT_TARGET = 4_000


def result_floor_budget(top_k: int, hard_budget_tokens: int) -> int:
    """Return the result-count floor constrained by the hard token budget."""

    return min(hard_budget_tokens, BUDGET_PER_RESULT * top_k)

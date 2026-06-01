from __future__ import annotations

from netsuite_llm_wiki_mcp.context_budget import compute_context_budget


def test_default_budget():
    b = compute_context_budget(None)
    assert b.total == 16_000
    assert b.response_reserve > 0
    assert b.pages_budget > b.index_budget
    assert b.max_page_size <= b.pages_budget


def test_custom_budget():
    b = compute_context_budget(100_000)
    assert b.total == 100_000
    assert b.response_reserve == 15_000
    assert b.pages_budget == 50_000


def test_min_clamp():
    b = compute_context_budget(100)
    assert b.total == 4_000


def test_max_clamp():
    b = compute_context_budget(10_000_000)
    assert b.total == 1_000_000


def test_per_page_floor():
    b = compute_context_budget(4_000)
    assert b.max_page_size >= 1_200

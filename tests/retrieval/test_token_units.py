from __future__ import annotations

from retrieval.context_packer import estimate_response_tokens
from retrieval.passage_chunker import estimate_passage_tokens, passage_token_units


def test_token_units_keep_response_and_passage_scales_explicit() -> None:
    cases = ["", "  ", "hello world", "中文检索", "mixed 中文 code_snake()", "```py\nvalue = 1\n```"]

    for text in cases:
        assert estimate_response_tokens(text) >= 0
        assert estimate_passage_tokens(text) == len(passage_token_units(text))

    assert estimate_response_tokens("hello   world") == 2
    assert estimate_passage_tokens("中文") == 2
    assert estimate_passage_tokens("code_snake()") > estimate_response_tokens("code_snake()")

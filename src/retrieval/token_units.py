"""Explicit token-unit primitives for response and passage boundaries."""

from __future__ import annotations

import re
from typing import Literal

TokenUnitMode = Literal["response", "passage"]

_RESPONSE_UNIT_RE = re.compile(r"\S+")
_PASSAGE_UNIT_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]|[^\s]")


def token_units(text: str, *, mode: TokenUnitMode) -> list[str]:
    """Return deterministic units for one explicitly named business scale."""

    if not text:
        return []
    pattern = _RESPONSE_UNIT_RE if mode == "response" else _PASSAGE_UNIT_RE
    return pattern.findall(text)


def count_response_tokens(text: str) -> int:
    """Count response-budget units without changing the packer scale."""

    return len(token_units(text, mode="response"))


def passage_token_units(text: str) -> list[str]:
    """Return chunk/index units, preserving CJK and code punctuation behavior."""

    return token_units(text, mode="passage")


def count_passage_tokens(text: str) -> int:
    """Count chunk/index units for ``PassageChunk.token_count``."""

    return len(passage_token_units(text))


__all__ = [
    "TokenUnitMode",
    "count_passage_tokens",
    "count_response_tokens",
    "passage_token_units",
    "token_units",
]

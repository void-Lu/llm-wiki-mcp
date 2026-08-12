"""Deterministic, explainable evidence fallback decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FallbackLevel = Literal["none", "raw"]


@dataclass(frozen=True)
class FallbackDecision:
    level: FallbackLevel
    reasons: tuple[str, ...] = ()
    allowed_source_paths: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "allowed_source_paths": list(self.allowed_source_paths),
        }

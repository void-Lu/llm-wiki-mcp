"""Deterministic, explainable evidence fallback decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


FallbackLevel = Literal["none", "capsule", "raw"]


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


def decide_fallback(
    *,
    intent: str,
    top_score: float,
    eligible_formal_count: int,
    citation_count: int,
    stale: bool = False,
    conflict: bool = False,
    source_paths: Iterable[str] = (),
) -> FallbackDecision:
    """Choose the minimum additional evidence needed for a query.

    Raw evidence is opt-in by this policy only.  Query V2 also has one narrow,
    observable exception for primary Wiki passages missing Latin-term
    coverage; that path uses its own ``wiki_primary_missing_latin_coverage``
    reason and never compares raw BM25 values with active ranking scores.
    History passages are a separate corpus choice and never pass through this
    function.
    """
    reasons: list[str] = []
    if intent == "exact_evidence":
        reasons.append("exact_evidence_requested")
    if eligible_formal_count == 0:
        reasons.append("formal_coverage_missing")
    if stale:
        reasons.append("concept_stale")
    if conflict:
        reasons.append("source_conflict")
    if citation_count == 0:
        reasons.append("citation_missing")
    if top_score <= 0:
        reasons.append("low_confidence")

    allowed = tuple(sorted({path for path in source_paths if path}))
    if any(reason in reasons for reason in ("exact_evidence_requested", "formal_coverage_missing", "concept_stale", "source_conflict")):
        return FallbackDecision("raw", tuple(reasons), allowed)
    if citation_count < 2 and allowed:
        return FallbackDecision("capsule", tuple(reasons), allowed)
    return FallbackDecision("none", tuple(reasons), ())

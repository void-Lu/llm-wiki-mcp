from common.fallback_policy import FallbackDecision


def test_raw_fallback_decision_serializes_stable_envelope() -> None:
    decision = FallbackDecision(
        "raw",
        ("wiki_zero_results",),
        ("raw/a.md",),
    )

    assert decision.as_dict() == {
        "level": "raw",
        "reasons": ["wiki_zero_results"],
        "allowed_source_paths": ["raw/a.md"],
    }


def test_none_fallback_decision_serializes_without_sources() -> None:
    assert FallbackDecision("none").as_dict() == {
        "level": "none",
        "reasons": [],
        "allowed_source_paths": [],
    }

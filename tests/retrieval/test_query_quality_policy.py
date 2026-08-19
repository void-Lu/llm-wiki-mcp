"""质量门禁纯策略的 decision-table 测试。"""

from __future__ import annotations

from dataclasses import fields

import pytest

from retrieval.query_quality_policy import (
    CandidateFeature,
    FAIL_OPEN_REASON_CODES,
    GATE_FAIL_OPEN_ERROR,
    GATE_FAIL_OPEN_LOW_SAMPLE,
    GATE_FAIL_OPEN_POLICY_MISSING,
    GATE_KEEP_DEFAULT,
    GATE_KEEP_EXACT_IDENTIFIER,
    GATE_KEEP_GRAPH_SUPPORTED,
    GATE_KEEP_MULTI_SIGNAL,
    GATE_KEEP_PHRASE,
    GATE_KEEP_RAW_COVERAGE,
    GATE_KEEP_TERM_COVERAGE,
    GATE_KEEP_TOP1_RESCUE,
    GATE_REASON_CODES,
    GATE_REJECT_GRAPH_ONLY_WEAK,
    GATE_REJECT_LOW_CONFIDENCE,
    GATE_REJECT_NO_INDEPENDENT_SIGNAL,
    GATE_REJECT_RAW_NO_COVERAGE,
    GATE_REJECT_SCORE_CLIFF,
    GATE_REJECT_SCORE_FLOOR,
    GATE_WOULD_SUPPRESS_ALL,
    KEEP_REASON_CODES,
    REJECT_REASON_CODES,
    SCORE_FAMILIES,
    _HIT_KEYS,
    build_candidate_features,
    derive_score_family,
    evaluate_quality_policy,
    extract_candidate_feature,
    is_gate_reason_code,
)
from retrieval.retrieval_index import PassageHit


def _hit(path: str, *, source_kind: str = "wiki", text: str = "invoice approval") -> PassageHit:
    return PassageHit(
        passage_id=path.replace("/", "-") + "-p1",
        page_path=path,
        title=path.rsplit("/", 1)[-1],
        heading_path=(),
        text=text,
        score=1.0,
        corpus="knowledge" if source_kind != "raw" else "raw",
        authority="high" if source_kind != "raw" else "low",
        source_kind=source_kind,
    )


def test_hit_key_vocabulary_follows_passage_hit_fields() -> None:
    assert _HIT_KEYS == frozenset(field.name for field in fields(PassageHit))


def test_score_family_derivation_covers_the_five_branches() -> None:
    cases = (
        (
            {"hit": _hit("wiki/main.md"), "score": 3.0, "fts_rank": 1, "rrf": 0.5},
            "main_rrf",
        ),
        (
            {"hit": _hit("wiki/relaxed.md"), "score": 2.0, "fts_rank": 1},
            "wiki_relaxed",
        ),
        (
            {"hit": _hit("raw/recovery.md", source_kind="raw"), "score": 2.0, "fts_rank": 1},
            "raw_recovery",
        ),
        (
            {
                "hit": _hit("raw/coverage.md", source_kind="raw"),
                "score": 1.0,
                "coverage_terms": ["rag"],
                "coverage_ratio": 1.0,
                "source_local_rank": 1,
                "fusion_source": "raw",
            },
            "coverage_fusion",
        ),
        (
            {"hit": _hit("wiki/graph.md"), "score": 0.4, "graph_score": 0.4},
            "graph_extension",
        ),
    )

    assert tuple(
        derive_score_family(candidate, branch="wiki_relaxed" if family == "wiki_relaxed" else None)
        for candidate, family in cases
    ) == SCORE_FAMILIES


def test_auto_scope_uses_internal_effective_scope() -> None:
    feature = extract_candidate_feature(
        {
            "hit": _hit("wiki/history.md"),
            "score": 1.0,
            "fts_rank": 1,
        },
        scope="auto",
        effective_scope="history",
    )

    assert feature.effective_scope == "history"
    assert feature.effective_scope != "auto"


def test_mapping_feature_extraction_ignores_legacy_candidate_aliases() -> None:
    legacy = extract_candidate_feature(
        {
            "hit": _hit("wiki/legacy.md"),
            "score": 1.0,
            "exact_match": True,
            "score_family": "graph_extension",
            "branch": "wiki_relaxed",
            "local_rank": 1,
            "fallback_reason": "raw recovery",
            "coverage": {"ratio": 1.0},
            "uncovered_terms": [],
        }
    )
    assert legacy.score_family == "main_rrf"
    assert legacy.exact_signal is False
    assert legacy.branch_rank is None
    assert legacy.fallback_level == "none"
    assert legacy.term_coverage == 0.0
    with pytest.raises(ValueError, match="page_path is required"):
        extract_candidate_feature({"hit": {"path": "wiki/legacy-path.md"}, "score": 1.0})

    canonical = extract_candidate_feature(
        {
            "hit": _hit("wiki/canonical.md"),
            "score": 1.0,
            "fts_rank": 1,
            "exact": True,
            "coverage_ratio": 0.5,
        }
    )
    assert canonical.score_family == "coverage_fusion"
    assert canonical.exact_signal is True
    assert canonical.term_coverage == 0.5


def test_branch_local_rank_margin_uses_normalized_path_for_ties() -> None:
    features = build_candidate_features(
        (
            {"hit": _hit("wiki/Z-page.md"), "score": 5.0},
            {"hit": _hit("wiki/a-page.md"), "score": 5.0},
            {"hit": _hit("wiki/tail.md"), "score": 3.0},
        ),
        branch="wiki_relaxed",
    )

    by_path = {feature.page_path: feature for feature in features}
    assert by_path["wiki/a-page.md"].branch_rank == 1
    assert by_path["wiki/Z-page.md"].branch_rank == 2
    assert by_path["wiki/a-page.md"].branch_margin == 0.0
    assert by_path["wiki/Z-page.md"].branch_margin == 2.0
    assert by_path["wiki/tail.md"].branch_margin is None


def test_keep_reason_decision_table_is_exhaustive_and_keep_all() -> None:
    cases = (
        (CandidateFeature("wiki/exact.md", "main_rrf", exact_signal=True), GATE_KEEP_EXACT_IDENTIFIER),
        (CandidateFeature("wiki/identifier.md", "main_rrf", identifier_signal=True), GATE_KEEP_EXACT_IDENTIFIER),
        (CandidateFeature("wiki/phrase.md", "wiki_relaxed", phrase_signal=True), GATE_KEEP_PHRASE),
        (CandidateFeature("wiki/coverage.md", "main_rrf", term_coverage=0.5), GATE_KEEP_TERM_COVERAGE),
        (
            CandidateFeature("raw/coverage.md", "coverage_fusion", term_coverage=1.0, source_kind="raw"),
            GATE_KEEP_RAW_COVERAGE,
        ),
        (
            CandidateFeature("wiki/multi.md", "main_rrf", independent_signal_count=2),
            GATE_KEEP_MULTI_SIGNAL,
        ),
        (CandidateFeature("wiki/graph.md", "graph_extension"), GATE_KEEP_GRAPH_SUPPORTED),
        (CandidateFeature("wiki/rescue.md", "main_rrf", top1_rescue=True), GATE_KEEP_TOP1_RESCUE),
        (CandidateFeature("wiki/default.md", "main_rrf"), GATE_KEEP_DEFAULT),
    )

    result = evaluate_quality_policy(tuple(feature for feature, _reason in cases))

    assert [decision.reason_code for decision in result.decisions] == [reason for _feature, reason in cases]
    assert all(decision.accepted for decision in result.decisions)
    assert result.rejected == ()
    assert result.summary.candidate_count == len(cases)
    assert result.summary.accepted_count == len(cases)
    assert result.summary.rejected_count == 0


def test_reason_priority_prefers_exact_then_phrase_then_coverage_then_signals() -> None:
    features = (
        CandidateFeature(
            "wiki/priority.md",
            "main_rrf",
            term_coverage=1.0,
            exact_signal=True,
            phrase_signal=True,
            independent_signal_count=3,
        ),
        CandidateFeature(
            "wiki/phrase-priority.md",
            "wiki_relaxed",
            term_coverage=1.0,
            phrase_signal=True,
            independent_signal_count=3,
        ),
        CandidateFeature(
            "wiki/coverage-priority.md",
            "main_rrf",
            term_coverage=1.0,
            independent_signal_count=3,
        ),
    )

    result = evaluate_quality_policy(features)

    assert [decision.reason_code for decision in result.decisions] == [
        GATE_KEEP_EXACT_IDENTIFIER,
        GATE_KEEP_PHRASE,
        GATE_KEEP_TERM_COVERAGE,
    ]


def test_summary_is_bounded_safe_and_repeatable() -> None:
    candidates = (
        {"hit": _hit("wiki/one.md"), "score": 2.0, "fts_rank": 1, "exact": True},
        {"hit": _hit("wiki/two.md"), "score": 1.0, "fts_rank": 2, "coverage_ratio": 1.0},
        {"hit": _hit("wiki/three.md"), "score": 0.5, "graph_score": 0.5},
    )
    features = build_candidate_features(candidates)

    first = evaluate_quality_policy(features)
    second = evaluate_quality_policy(features)

    assert first == second
    assert dict(first.summary.score_family_counts) == {
        "coverage_fusion": 1,
        "graph_extension": 1,
        "main_rrf": 1,
    }
    assert dict(first.summary.reason_counts) == {
        GATE_KEEP_EXACT_IDENTIFIER: 1,
        GATE_KEEP_GRAPH_SUPPORTED: 1,
        GATE_KEEP_TERM_COVERAGE: 1,
    }
    assert first.summary.low_sample_buckets == ()
    assert first.summary.fail_open is False
    assert all("/" not in code and "?" not in code for code in first.summary.reason_counts)


def test_reject_and_fail_open_reason_skeletons_are_frozen() -> None:
    assert REJECT_REASON_CODES == {
        GATE_REJECT_SCORE_FLOOR,
        GATE_REJECT_SCORE_CLIFF,
        GATE_REJECT_NO_INDEPENDENT_SIGNAL,
        GATE_REJECT_RAW_NO_COVERAGE,
        GATE_REJECT_GRAPH_ONLY_WEAK,
        GATE_REJECT_LOW_CONFIDENCE,
    }
    assert FAIL_OPEN_REASON_CODES == {
        GATE_FAIL_OPEN_LOW_SAMPLE,
        GATE_FAIL_OPEN_POLICY_MISSING,
        GATE_FAIL_OPEN_ERROR,
    }
    assert KEEP_REASON_CODES.isdisjoint(REJECT_REASON_CODES)
    assert GATE_REASON_CODES == {
        *KEEP_REASON_CODES,
        *REJECT_REASON_CODES,
        *FAIL_OPEN_REASON_CODES,
        GATE_WOULD_SUPPRESS_ALL,
    }
    assert all(is_gate_reason_code(code) for code in GATE_REASON_CODES)

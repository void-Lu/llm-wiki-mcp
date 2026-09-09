from retrieval.body_budget import (
    BUDGET_PER_RESULT,
    DEFAULT_INTENT_TARGET,
    INTENT_TARGETS,
    PAGE_FILL_LIMIT,
    PAGE_FULL_FILL_MIN_RATIO,
    PAGE_TOKEN_BUDGET,
    PAGE_WEAK_HIT_LIMIT,
    result_floor_budget,
)


def test_query_body_budget_policy_constants_are_explicit() -> None:
    assert PAGE_FILL_LIMIT == 500
    assert PAGE_TOKEN_BUDGET == 2_400
    assert PAGE_FULL_FILL_MIN_RATIO == 0.6
    assert PAGE_WEAK_HIT_LIMIT == 3
    assert BUDGET_PER_RESULT == 400
    assert DEFAULT_INTENT_TARGET == 4_000


def test_query_body_budget_policy_maps_each_intent() -> None:
    assert INTENT_TARGETS == {
        "exact_entity": 2_000,
        "concept": 4_000,
        "comparison": 8_000,
        "research": 16_000,
        "history": 4_000,
        "exact_evidence": 4_000,
    }


def test_result_floor_budget_uses_the_named_policy_coefficient() -> None:
    assert result_floor_budget(3, 10_000) == 1_200
    assert result_floor_budget(20, 5_000) == 5_000

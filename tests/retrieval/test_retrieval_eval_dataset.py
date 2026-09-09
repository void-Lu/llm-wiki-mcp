"""评测数据集 schema owner 的无 vault 纯函数测试。"""

from __future__ import annotations

import pytest

from retrieval.retrieval_eval_dataset import (
    EvaluationFilterContract,
    RetrievalEvalError,
    normalize_evaluation_filter_contract,
    parse_evaluation_case,
    parse_evaluation_manifest,
)


def test_dataset_schema_parser_builds_case_without_vault() -> None:
    manifest = parse_evaluation_manifest(
        {
            "schema_version": 1,
            "dataset_id": "unit",
            "revision": "rev-1",
            "abstention_threshold": 0.5,
        }
    )
    case = parse_evaluation_case(
        {
            "schema_version": 1,
            "id": "invoice",
            "query": "invoice approval",
            "relevant": [{"path": "wiki/concepts/invoice.md", "grade": 3}],
            "filters": {"type": "concept", "tags": ["finance"]},
            "answerable": True,
            "language": "en",
            "tags": ["smoke"],
            "notes": "pure parser",
        },
        1,
    )

    assert manifest.dataset_id == "unit"
    assert case.relevant[0].path == "wiki/concepts/invoice.md"
    assert case.filters == {"filter_type": "concept", "filter_tags": ["finance"]}


def test_dataset_filter_owner_rejects_conflicting_aliases() -> None:
    contract = normalize_evaluation_filter_contract(
        {"project": "Alpha", "filter_tags": ["finance"], "pathPrefix": "wiki/concepts/"},
        "aliases",
    )
    assert isinstance(contract, EvaluationFilterContract)
    assert dict(contract.public) == {"tags": ["finance"], "path_prefix": "wiki/concepts/"}
    assert dict(contract.matcher) == {
        "tags": ("finance",),
        "path_prefix": "wiki/concepts/",
        "project": "Alpha",
    }

    with pytest.raises(RetrievalEvalError, match="type and filter_type disagree"):
        normalize_evaluation_filter_contract({"type": "concept", "filter_type": "entity"}, "conflict")


def test_dataset_schema_parser_rejects_invalid_answerability_and_grade() -> None:
    base = {
        "schema_version": 1,
        "id": "case",
        "query": "query",
        "relevant": [{"path": "wiki/concepts/a.md", "grade": 3}],
        "answerable": False,
        "language": "en",
    }
    with pytest.raises(RetrievalEvalError) as no_answer_error:
        parse_evaluation_case(base, 1)
    assert no_answer_error.value.code == "invalid_no_answer_case"

    invalid_grade = {**base, "answerable": True, "relevant": [{"path": "wiki/concepts/a.md", "grade": 4}]}
    with pytest.raises(RetrievalEvalError) as grade_error:
        parse_evaluation_case(invalid_grade, 1)
    assert grade_error.value.code == "invalid_grade"

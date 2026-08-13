from __future__ import annotations

from app.cli import _build_parser


def test_repair_page_operation_parser_has_explicit_admin_plan_and_apply() -> None:
    parser = _build_parser()
    plan = parser.parse_args(["repair", "page-operation", "plan", "--vault", "C:/vault"])
    apply = parser.parse_args(["repair", "page-operation", "apply", "--vault", "C:/vault", "--operation-id", "operation-1"])

    assert plan.command == "repair"
    assert plan.page_operation_action == "plan"
    assert apply.page_operation_action == "apply"
    assert apply.operation_id == "operation-1"


def test_repair_codegraph_removal_parser_has_plan_and_apply() -> None:
    parser = _build_parser()
    plan = parser.parse_args(["repair", "codegraph-removal", "plan", "--vault", "C:/vault"])
    apply = parser.parse_args(["repair", "codegraph-removal", "apply", "--vault", "C:/vault"])

    assert plan.command == "repair"
    assert plan.codegraph_removal_action == "plan"
    assert apply.codegraph_removal_action == "apply"

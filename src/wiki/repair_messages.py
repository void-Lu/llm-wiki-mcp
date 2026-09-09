"""Stable CLI hints shared by projection repair boundaries."""

from __future__ import annotations


PAGE_OPERATION_APPLY_COMMAND = (
    "uv run llm-wiki-mcp repair page-operation apply "
    "--vault <vault> --operation-id <operation-id>"
)


def page_operation_repair_message(reason: str) -> str:
    """Point a committed page projection failure at the real admin repair path."""

    return f"{reason}; run '{PAGE_OPERATION_APPLY_COMMAND}'"


__all__ = ["PAGE_OPERATION_APPLY_COMMAND", "page_operation_repair_message"]

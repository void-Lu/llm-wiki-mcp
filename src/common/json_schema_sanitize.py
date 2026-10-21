"""Sanitize JSON Schema exported for MCP / OpenAI-compatible tool parameters."""

from __future__ import annotations

from typing import Any


def strip_json_schema_title_annotations(node: Any, *, under_properties: bool = False) -> Any:
    """Remove JSON Schema annotation ``title`` keys; keep real ``properties.title``.

    Pydantic ``model_json_schema`` emits human-readable ``title`` annotations on
    object/property schemas (for example root ``wiki_write_noteArguments`` or
    field ``Title``). Some OpenAI-compatible gateways (notably SuiteCloud
    Developer Assistant) reject tool schemas that include those annotations and
    respond with HTTP 204.

    A property *named* ``title`` under a ``properties`` map is a real parameter
    and must be kept. Annotation titles nested *inside* that property's schema
    are still removed. ``required`` and other keys are left unchanged.
    """

    if isinstance(node, list):
        return [strip_json_schema_title_annotations(item, under_properties=under_properties) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title":
            if under_properties:
                # Property key literally named title: keep it; strip annotations inside.
                out[key] = strip_json_schema_title_annotations(value, under_properties=False)
            continue
        out[key] = strip_json_schema_title_annotations(value, under_properties=(key == "properties"))
    return out

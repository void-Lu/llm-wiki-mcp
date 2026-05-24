from __future__ import annotations

import fnmatch
import json
from datetime import date, datetime
from typing import Any

from netsuite_rag_mcp.models import ARRAY_METADATA_FIELDS

INT_METADATA_FIELDS = {"line_start", "line_end"}

BOOL_METADATA_FIELDS = {"generated", "do_not_edit", "archived"}

GENERATED_WIKI_STRING_METADATA_FIELDS = {"archived_at", "archived_reason", "former_source_path"}

NEW_STRING_METADATA_FIELDS = {
    "source_kind", "source_name", "file_hash", "git_commit",
    "function_name", "repo_root", "repo_relative_path", "language",
}

KNOWN_METADATA_FIELDS = (
    ARRAY_METADATA_FIELDS
    | INT_METADATA_FIELDS
    | NEW_STRING_METADATA_FIELDS
    | BOOL_METADATA_FIELDS
    | GENERATED_WIKI_STRING_METADATA_FIELDS
)

WILDCARD_FILTER_FIELDS = {"function_name"}


def to_chroma_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    stored: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if key in ARRAY_METADATA_FIELDS:
            values = _as_string_list(value)
            stored[f"{key}_json"] = json.dumps(values, ensure_ascii=False)
            stored[f"{key}_text"] = "|" + "|".join(values) + "|" if values else ""
        elif key in INT_METADATA_FIELDS:
            stored[key] = str(value)
        elif isinstance(value, (str, int, float, bool)):
            stored[key] = value
        elif isinstance(value, (date, datetime)):
            stored[key] = value.isoformat()
        else:
            stored[key] = json.dumps(value, ensure_ascii=False, default=str)
    return stored


def from_chroma_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.endswith("_json"):
            base = key[: -len("_json")]
            try:
                loaded = json.loads(str(value))
            except json.JSONDecodeError:
                loaded = []
            restored[base] = loaded if isinstance(loaded, list) else []
        elif key.endswith("_text"):
            continue
        elif key in INT_METADATA_FIELDS:
            try:
                restored[key] = int(value)
            except (ValueError, TypeError):
                restored[key] = 0
        else:
            restored[key] = value
    for field in ARRAY_METADATA_FIELDS:
        restored.setdefault(field, [])
    for field in INT_METADATA_FIELDS:
        restored.setdefault(field, 0)
    for field in NEW_STRING_METADATA_FIELDS:
        restored.setdefault(field, "")
    for field in BOOL_METADATA_FIELDS:
        restored.setdefault(field, False)
    for field in GENERATED_WIKI_STRING_METADATA_FIELDS:
        restored.setdefault(field, "")
    return restored


def metadata_matches_filters(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        actual = metadata.get(key)
        if isinstance(actual, list):
            expected_values = _as_string_list(expected)
            if not all(value in actual for value in expected_values):
                return False
        elif key in WILDCARD_FILTER_FIELDS and "*" in str(expected):
            pattern = str(expected)
            if not fnmatch.fnmatch(str(actual) if actual is not None else "", pattern):
                return False
        elif actual != expected:
            return False
    return True


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value)]
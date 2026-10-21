from __future__ import annotations

from common.json_schema_sanitize import strip_json_schema_title_annotations


def test_strip_removes_root_and_field_annotation_titles_keeps_property_named_title() -> None:
    schema = {
        "type": "object",
        "title": "wiki_write_noteArguments",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "content": {"title": "Content", "type": "string"},
            "note_type": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Note Type",
            },
        },
        "required": ["title", "content"],
        "additionalProperties": False,
    }

    cleaned = strip_json_schema_title_annotations(schema)

    assert "title" not in cleaned
    assert cleaned["required"] == ["title", "content"]
    assert cleaned["additionalProperties"] is False
    assert set(cleaned["properties"]) == {"title", "content", "note_type"}
    assert cleaned["properties"]["title"] == {"type": "string"}
    assert cleaned["properties"]["content"] == {"type": "string"}
    assert cleaned["properties"]["note_type"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
    }
    # Original unchanged
    assert schema["title"] == "wiki_write_noteArguments"
    assert schema["properties"]["title"]["title"] == "Title"


def test_strip_recurses_into_defs_items_and_nested_objects() -> None:
    schema = {
        "title": "Root",
        "type": "object",
        "properties": {
            "items": {
                "title": "Items",
                "type": "array",
                "items": {
                    "title": "Item",
                    "type": "object",
                    "properties": {
                        "title": {"title": "Nested Title", "type": "string"},
                        "name": {"title": "Name", "type": "string"},
                    },
                    "required": ["title"],
                },
            }
        },
        "$defs": {
            "Blob": {"title": "Blob", "type": "object", "properties": {"x": {"title": "X", "type": "integer"}}},
        },
    }

    cleaned = strip_json_schema_title_annotations(schema)
    assert "title" not in cleaned
    assert "title" not in cleaned["properties"]["items"]
    item = cleaned["properties"]["items"]["items"]
    assert "title" not in item
    assert item["properties"]["title"] == {"type": "string"}
    assert item["required"] == ["title"]
    assert "title" not in item["properties"]["name"]
    assert "title" not in cleaned["$defs"]["Blob"]
    assert "title" not in cleaned["$defs"]["Blob"]["properties"]["x"]

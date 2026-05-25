import json
from datetime import date, datetime

from netsuite_rag_mcp.metadata import (
    KNOWN_METADATA_FIELDS,
    from_chroma_metadata,
    metadata_matches_filters,
    to_chroma_metadata,
)
from netsuite_rag_mcp.policy import build_answer_policy
from netsuite_rag_mcp.redaction import redact_sensitive_text


def test_metadata_round_trip_array_fields():
    original = {
        "type": "script",
        "script_type": "restlet",
        "related_objects": ["salesorder", "itemfulfillment"],
        "related_scripts": ["customscript_order_sync_mr"],
        "tags": ["netsuite", "restlet"],
    }

    stored = to_chroma_metadata(original)
    restored = from_chroma_metadata(stored)

    assert stored["related_objects_json"] == '["salesorder", "itemfulfillment"]'
    assert restored["related_objects"] == ["salesorder", "itemfulfillment"]
    assert restored["related_scripts"] == ["customscript_order_sync_mr"]
    assert metadata_matches_filters(restored, {"script_type": "restlet", "related_objects": "salesorder"})
    assert not metadata_matches_filters(restored, {"related_objects": "invoice"})


def test_to_chroma_metadata_serializes_date_and_datetime_as_iso_strings():
    stored = to_chroma_metadata(
        {
            "business_date": date(2026, 5, 13),
            "indexed_at": datetime(2026, 5, 13, 9, 30),
        }
    )

    assert stored["business_date"] == "2026-05-13"
    assert stored["indexed_at"] == "2026-05-13T09:30:00"


def test_to_chroma_metadata_serializes_nested_date_and_datetime_metadata():
    stored = to_chroma_metadata(
        {
            "complex": {
                "business_date": date(2026, 5, 13),
                "events": [{"indexed_at": datetime(2026, 5, 13, 9, 30)}],
            }
        }
    )

    complex_metadata = json.loads(stored["complex"])
    assert complex_metadata["business_date"] == "2026-05-13"
    assert complex_metadata["events"][0]["indexed_at"] == "2026-05-13 09:30:00"


# --- T02: New metadata field tests ---


def test_to_chroma_metadata_serializes_new_string_fields():
    original = {
        "source_kind": "code",
        "source_name": "suitecommerce-extension",
        "file_hash": "abc123def",
        "git_commit": "a1b2c3d4",
        "function_name": "afterSubmit",
        "repo_root": "/opt/repos/netsuite",
        "repo_relative_path": "src/OrderSync.js",
        "language": "javascript",
    }
    stored = to_chroma_metadata(original)

    assert stored["source_kind"] == "code"
    assert stored["source_name"] == "suitecommerce-extension"
    assert stored["file_hash"] == "abc123def"
    assert stored["git_commit"] == "a1b2c3d4"
    assert stored["function_name"] == "afterSubmit"
    assert stored["repo_root"] == "/opt/repos/netsuite"
    assert stored["repo_relative_path"] == "src/OrderSync.js"
    assert stored["language"] == "javascript"


def test_to_chroma_metadata_converts_int_fields_to_str():
    original = {
        "line_start": 42,
        "line_end": 87,
    }
    stored = to_chroma_metadata(original)

    assert isinstance(stored["line_start"], str)
    assert isinstance(stored["line_end"], str)
    assert stored["line_start"] == "42"
    assert stored["line_end"] == "87"


def test_from_chroma_metadata_restores_new_string_fields():
    stored = {
        "source_kind": "code",
        "source_name": "suitecommerce-extension",
        "file_hash": "abc123def",
        "git_commit": "a1b2c3d4",
        "function_name": "afterSubmit",
        "repo_root": "/opt/repos/netsuite",
        "repo_relative_path": "src/OrderSync.js",
        "language": "javascript",
        "line_start": "42",
        "line_end": "87",
    }
    restored = from_chroma_metadata(stored)

    assert restored["source_kind"] == "code"
    assert restored["source_name"] == "suitecommerce-extension"
    assert restored["file_hash"] == "abc123def"
    assert restored["git_commit"] == "a1b2c3d4"
    assert restored["function_name"] == "afterSubmit"
    assert restored["repo_root"] == "/opt/repos/netsuite"
    assert restored["repo_relative_path"] == "src/OrderSync.js"
    assert restored["language"] == "javascript"


def test_from_chroma_metadata_converts_line_fields_back_to_int():
    stored = {
        "line_start": "42",
        "line_end": "87",
    }
    restored = from_chroma_metadata(stored)

    assert isinstance(restored["line_start"], int)
    assert isinstance(restored["line_end"], int)
    assert restored["line_start"] == 42
    assert restored["line_end"] == 87


def test_from_chroma_metadata_int_fields_default_to_zero():
    restored = from_chroma_metadata({})

    assert restored["line_start"] == 0
    assert restored["line_end"] == 0


def test_from_chroma_metadata_string_fields_default_to_empty():
    restored = from_chroma_metadata({})

    for field in ("source_kind", "source_name", "file_hash", "git_commit",
                  "function_name", "repo_root", "repo_relative_path", "language"):
        assert restored.get(field, "") == ""


def test_new_fields_round_trip():
    original = {
        "source_kind": "code",
        "source_name": "ext",
        "file_hash": "deadbeef",
        "git_commit": "abc123",
        "function_name": "beforeSubmit",
        "line_start": 10,
        "line_end": 50,
        "repo_root": "/repo",
        "repo_relative_path": "lib/foo.js",
        "language": "javascript",
        "type": "script",
        "tags": ["netsuite"],
    }
    stored = to_chroma_metadata(original)
    restored = from_chroma_metadata(stored)

    assert restored["source_kind"] == "code"
    assert restored["source_name"] == "ext"
    assert restored["file_hash"] == "deadbeef"
    assert restored["git_commit"] == "abc123"
    assert restored["function_name"] == "beforeSubmit"
    assert restored["line_start"] == 10
    assert restored["line_end"] == 50
    assert restored["repo_root"] == "/repo"
    assert restored["repo_relative_path"] == "lib/foo.js"
    assert restored["language"] == "javascript"
    # Existing behavior preserved
    assert restored["type"] == "script"
    assert restored["tags"] == ["netsuite"]


def test_metadata_matches_filters_source_kind_exact_match():
    metadata = {"source_kind": "code", "type": "script"}
    assert metadata_matches_filters(metadata, {"source_kind": "code"})
    assert not metadata_matches_filters(metadata, {"source_kind": "note"})


def test_metadata_matches_filters_source_name_exact_match():
    metadata = {"source_name": "suitecommerce-extension"}
    assert metadata_matches_filters(metadata, {"source_name": "suitecommerce-extension"})
    assert not metadata_matches_filters(metadata, {"source_name": "other"})


def test_metadata_matches_filters_git_commit_exact_match():
    metadata = {"git_commit": "a1b2c3d4"}
    assert metadata_matches_filters(metadata, {"git_commit": "a1b2c3d4"})
    assert not metadata_matches_filters(metadata, {"git_commit": "ffff0000"})


def test_metadata_matches_filters_language_exact_match():
    metadata = {"language": "javascript"}
    assert metadata_matches_filters(metadata, {"language": "javascript"})
    assert not metadata_matches_filters(metadata, {"language": "python"})


def test_metadata_matches_filters_function_name_exact_match():
    metadata = {"function_name": "afterSubmit"}
    assert metadata_matches_filters(metadata, {"function_name": "afterSubmit"})
    assert not metadata_matches_filters(metadata, {"function_name": "beforeSubmit"})


def test_metadata_matches_filters_function_name_wildcard_prefix():
    metadata = {"function_name": "afterSubmit"}
    assert metadata_matches_filters(metadata, {"function_name": "after*"})
    assert not metadata_matches_filters(metadata, {"function_name": "before*"})


def test_metadata_matches_filters_function_name_wildcard_suffix():
    metadata = {"function_name": "afterSubmit"}
    assert metadata_matches_filters(metadata, {"function_name": "*Submit"})
    assert not metadata_matches_filters(metadata, {"function_name": "*Load"})


def test_metadata_matches_filters_function_name_wildcard_middle():
    metadata = {"function_name": "afterSubmit"}
    assert metadata_matches_filters(metadata, {"function_name": "after*Submit"})
    assert not metadata_matches_filters(metadata, {"function_name": "before*Submit"})


def test_known_metadata_fields_includes_new_fields():
    new_fields = {
        "source_kind", "source_name", "file_hash", "git_commit",
        "function_name", "line_start", "line_end", "repo_root",
        "repo_relative_path", "language",
    }
    for field in new_fields:
        assert field in KNOWN_METADATA_FIELDS


def test_redact_sensitive_text_masks_private_values():
    text = "手机号 13812345678, token sk-abc1234567890, email user@example.com"

    redacted = redact_sensitive_text(text)

    assert "13812345678" not in redacted


def test_from_chroma_metadata_defaults_generated_wiki_fields():
    restored = from_chroma_metadata({})

    assert restored["generated"] is False
    assert restored["do_not_edit"] is False
    assert restored["archived"] is False
    assert restored["archived_at"] == ""
    assert restored["archived_reason"] == ""
    assert restored["former_source_path"] == ""
from __future__ import annotations

from app.public_contracts import (
    PublicError,
    project_public_result,
    public_error,
    public_error_from_exception,
    unknown_arguments_error,
)


def test_unknown_exception_maps_to_stable_safe_error() -> None:
    result = public_error_from_exception(RuntimeError("C:\\Users\\alice\\vault\\secret.md"))

    assert result["ok"] is False
    assert result["code"] == "internal_error"
    assert result["message"] == "the operation could not be completed"
    assert result["error"] == result["message"]
    assert "alice" not in str(result)
    assert isinstance(result["correlation_id"], str)


def test_public_error_round_trip_keeps_correlation_id() -> None:
    original = PublicError("invalid_scope", "ignored", correlation_id="corr-123")
    payload = original.to_payload()

    restored = PublicError.from_payload(payload)

    assert restored.code == "invalid_scope"
    assert restored.correlation_id == "corr-123"
    assert restored.message == "the query scope is invalid"


def test_public_result_projection_is_the_single_safe_boundary() -> None:
    result = project_public_result(
        {
            "ok": True,
            "path": "wiki/concepts/example.md",
            "absolute_path": "C:/Users/alice/vault/wiki/concepts/example.md",
            "content_hash": "api_abc1234567890",
            "error": "C:/Users/alice/vault",
        },
        logical_vault="primary",
    )

    assert result["path"] == "wiki/concepts/example.md"
    assert result["content_hash"] == "api_abc1234567890"
    assert "absolute_path" not in result
    assert result["error"] == "The operation could not be completed."


def test_unknown_arguments_error_reports_sorted_safe_names_and_drops_malformed_keys() -> None:
    result = unknown_arguments_error(
        [
            "query",
            "pathPrefix",
            "path_prefix",
            "path_prefix",
            "C:\\Users\\alice\\secret",
            "../etc/passwd",
            "bad\nkey",
            "bad\rkey",
            "a" * 65,
            "has space",
            "1starts_digit",
            "api_key_1234567890",
            "secret_abcdefghijk",
            123,
        ]
    )

    assert result["ok"] is False
    assert result["code"] == "validation_error"
    assert result["retryable"] is False
    assert result["unknown_arguments"] == ["pathPrefix", "path_prefix", "query"]
    assert result["message"] == "unknown arguments: pathPrefix, path_prefix, query"
    assert result["error"] == result["message"]
    assert isinstance(result["correlation_id"], str)
    serialized = str(result)
    for forbidden in (
        "alice",
        "passwd",
        "bad",
        "a" * 65,
        "has space",
        "1starts_digit",
        "api_key_1234567890",
        "secret_abcdefghijk",
        "REDACTED_SECRET",
    ):
        assert forbidden not in serialized


def test_unknown_arguments_error_falls_back_when_no_key_is_safe() -> None:
    result = unknown_arguments_error(
        ["C:\\secret\\path", "../escape", "line1\nline2", "x" * 100, "api_key_1234567890"]
    )

    assert result["ok"] is False
    assert result["code"] == "validation_error"
    assert result["retryable"] is False
    assert result["message"] == "the request is invalid"
    assert result["error"] == "the request is invalid"
    assert "unknown_arguments" not in result
    assert "secret" not in str(result)
    assert "api_key" not in str(result)


def test_unknown_arguments_error_caps_reported_key_count() -> None:
    keys = [f"arg_{i:02d}" for i in range(15)]
    result = unknown_arguments_error(keys)

    expected_names = [f"arg_{i:02d}" for i in range(10)]
    assert result["unknown_arguments"] == expected_names
    assert result["message"] == f"unknown arguments: {', '.join(expected_names)}, ..."
    assert result["error"] == result["message"]


def test_public_error_rejects_carriage_return_in_custom_message() -> None:
    result = public_error("validation_error", message="bad\rmessage")

    assert result["code"] == "validation_error"
    assert result["message"] == "the request is invalid"
    assert result["error"] == "the request is invalid"

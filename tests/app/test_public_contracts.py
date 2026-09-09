from __future__ import annotations

from app.public_contracts import PublicError, public_error_from_exception, project_public_result


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

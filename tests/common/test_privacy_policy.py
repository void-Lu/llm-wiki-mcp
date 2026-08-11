from __future__ import annotations

from pathlib import Path

import pytest

from common.privacy_policy import (
    LocatorError,
    field_class,
    normalize_vault_relative,
    project_public_result,
    redact_storage_value,
)
from retrieval.retrieval_index import page_from_file


def test_field_taxonomy_keeps_locator_and_integrity_identity() -> None:
    assert field_class("title") == "display"
    assert field_class("sources") == "locator"
    assert field_class("source_hash") == "integrity"
    assert field_class("absolute_path") == "internal"

    value = {
        "title": "Contact a@example.com",
        "sources": ["raw\\sources\\reference.txt"],
        "source_hash": "api_abc1234567890",
    }
    projected = redact_storage_value(value)

    assert projected == {
        "title": "Contact [REDACTED_EMAIL]",
        "sources": ["raw/sources/reference.txt"],
        "source_hash": "api_abc1234567890",
    }


def test_sensitive_or_absolute_locators_are_rejected_not_rewritten() -> None:
    with pytest.raises(LocatorError) as sensitive:
        normalize_vault_relative("raw/sources/token-api_abc1234567890.md")
    assert sensitive.value.code == "sensitive_locator"

    with pytest.raises(LocatorError) as absolute:
        normalize_vault_relative(r"C:\Users\alice\vault\page.md")
    assert absolute.value.code == "absolute_path_forbidden"


def test_public_projection_removes_internal_fields_and_preserves_integrity(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    page = root / "wiki" / "concepts" / "page.md"
    payload = {
        "ok": True,
        "vault_root": str(root),
        "path": str(page),
        "title": "Call 13800138000",
        "source_hash": "api_abc1234567890",
        "config_path": str(tmp_path / "config.yaml"),
        "traceback": "secret traceback",
    }

    result = project_public_result(payload, logical_vault="primary", vault_root=root)

    assert result["ok"] is True
    assert result["vault"] == "primary"
    assert result["path"] == "wiki/concepts/page.md"
    assert result["path_scope"] == "vault_relative"
    assert result["source_hash"] == "api_abc1234567890"
    assert result["title"] == "Call [REDACTED_PHONE]"
    assert "absolute_path" not in result
    assert "vault_root" not in result
    assert "config_path" not in result
    assert "traceback" not in result
    assert "absolute_path_removed" in result["warnings"]

    leaked_vault = project_public_result({"ok": True, "vault": str(root)})
    assert str(root) not in str(leaked_vault)


def test_retrieval_projection_uses_display_redaction_without_corrupting_locators(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = root / "wiki" / "concepts" / "privacy.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\n"
        "title: Contact 13800138000\n"
        "summary: person@example.com\n"
        "sources:\n"
        "  - raw/sources/reference.txt\n"
        "source_hash: api_abc1234567890\n"
        "---\n\n"
        "Body token=secret-value\n",
        encoding="utf-8",
    )

    page = page_from_file(root, target, scope="active")

    assert page is not None
    assert page.frontmatter["title"] == "Contact [REDACTED_PHONE]"
    assert page.frontmatter["summary"] == "[REDACTED_EMAIL]"
    assert page.frontmatter["sources"] == ["raw/sources/reference.txt"]
    assert page.frontmatter["source_hash"] == "api_abc1234567890"
    assert "secret-value" not in page.body
    assert "[REDACTED_SECRET]" in page.body

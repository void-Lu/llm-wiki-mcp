from __future__ import annotations

from typing import Any

import pytest

from app.server import DETAIL_FIELDS, assemble_vault_status


def _status_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        {
            "ok": True,
            "vault_root": "C:/private/vault",
            "initialized": True,
            "missing_required_paths": [],
            "vector": {"state": "ready"},
            "retrieval": {"active": {}, "archive": {}, "raw": {}},
            "version": "1.2.3",
            "runtime": {"server_version": "1.2.3"},
        },
        {
            "logical_vault": "primary",
            "resolution_source": "config",
            "archive": {"archive_index_enabled": True},
            "retrieval": {"lexical_enabled": True},
        },
        {
            "ok": True,
            "state": "ready",
            "code": "ready",
            "operations": [{"operation_id": "operation-1"}],
            "tombstone_count": 0,
            "archive_index": {"ok": True, "state": "ready", "scope": "archive"},
        },
        {"active": 2, "pending": 1},
    )


@pytest.mark.parametrize("detail", sorted(DETAIL_FIELDS))
def test_assemble_vault_status_crops_each_detail(detail: str) -> None:
    base_status, config_status, archive_status, execution_status = _status_inputs()

    result = assemble_vault_status(base_status, config_status, archive_status, execution_status, detail)

    assert set(result) == DETAIL_FIELDS[detail]
    assert result["vault"] == "primary"
    assert "vault_root" not in result


def test_assemble_vault_status_projects_archive_ready_shape() -> None:
    base_status, config_status, archive_status, execution_status = _status_inputs()

    result = assemble_vault_status(base_status, config_status, archive_status, execution_status, "archive")

    assert result["archive_index"] == {"enabled": True, "ok": True, "state": "ready", "scope": "archive"}
    assert result["archive_operations"] == [{"operation_id": "operation-1"}]
    assert result["archive_state"] == {"state": "ready", "code": "ready"}


def test_assemble_vault_status_forwards_archive_schema_diagnostics() -> None:
    base_status, config_status, archive_status, execution_status = _status_inputs()
    archive_status.update(
        {
            "ok": False,
            "state": "incompatible",
            "code": "archive_state_incompatible",
            "missing_tables": ["archive_events"],
            "missing_columns": {"archive_plans": ["payload"]},
            "operations": [],
            "archive_index": {"ok": False, "state": "missing", "scope": "archive"},
        }
    )

    result = assemble_vault_status(base_status, config_status, archive_status, execution_status, "archive")

    assert result["archive_state"] == {
        "state": "incompatible",
        "code": "archive_state_incompatible",
        "missing_tables": ["archive_events"],
        "missing_columns": {"archive_plans": ["payload"]},
    }
    assert result["archive_operations"] == []
    assert result["archive_index"]["enabled"] is True

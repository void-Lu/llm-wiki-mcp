from __future__ import annotations

import pytest

from wiki import projection_profile
from wiki.page_mutation_adapters import default_write_adapters
from wiki.projection_profile import (
    FORMAL_PROJECTION_STAGES,
    INGEST_CHAT_PROJECTION_STAGES,
    INGEST_PROJECTION_STAGES,
    RETRIEVAL_ONLY_PROJECTION_STAGES,
    ProjectionProfileError,
    assert_operation_stage_parity,
    projection_profiles,
    projection_stages,
)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("formal", FORMAL_PROJECTION_STAGES),
        ("chat", FORMAL_PROJECTION_STAGES),
        ("ingest", INGEST_PROJECTION_STAGES),
        ("ingest_chat", INGEST_CHAT_PROJECTION_STAGES),
        ("archive", RETRIEVAL_ONLY_PROJECTION_STAGES),
        ("provenance", RETRIEVAL_ONLY_PROJECTION_STAGES),
        ("privacy", RETRIEVAL_ONLY_PROJECTION_STAGES),
    ],
)
def test_projection_profile_registry_locks_each_change_kind(kind: str, expected: tuple[str, ...]) -> None:
    assert projection_stages(kind) == expected


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("create", "formal"),
        ("note", "formal"),
        ("update", "formal"),
        ("chat_source", "chat"),
        ("ingest_text", "ingest"),
        ("ingest_chat_source", "ingest_chat"),
        ("provenance_migration", "provenance"),
        ("privacy_audit", "privacy"),
    ],
)
def test_projection_profile_aliases_use_the_same_registry(alias: str, canonical: str) -> None:
    assert projection_stages(alias) is projection_stages(canonical)


def test_projection_profile_registry_is_read_only_and_rejects_unknown_kind() -> None:
    profiles = projection_profiles()
    with pytest.raises(TypeError):
        profiles["formal"] = ()  # type: ignore[index]
    with pytest.raises(ProjectionProfileError) as error:
        projection_stages("unregistered")
    assert error.value.code == "unknown_projection_kind"


def _registered_write_operation_kinds() -> set[str]:
    return {
        operation_kind
        for adapter in default_write_adapters()
        for operation_kind in adapter._operation_kinds  # type: ignore[attr-defined]
    }


def test_write_adapter_operation_kinds_match_formal_journal_stages() -> None:
    assert_operation_stage_parity(_registered_write_operation_kinds())


def test_write_adapter_stage_parity_reports_chat_profile_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    operation_kinds = _registered_write_operation_kinds()
    with monkeypatch.context() as patch:
        patch.setattr(
            projection_profile,
            "PROJECTION_PROFILES",
            {**projection_profile.PROJECTION_PROFILES, "chat": ("retrieval",)},
        )

        with pytest.raises(AssertionError, match="chat_source.*expected formal PAGE_STAGES") as error:
            assert_operation_stage_parity(operation_kinds)

        assert "canonical 'chat'" in str(error.value)

    assert_operation_stage_parity(operation_kinds)

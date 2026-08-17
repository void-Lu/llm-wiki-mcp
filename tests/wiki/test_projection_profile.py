from __future__ import annotations

import pytest

from wiki.projection_profile import (
    FORMAL_PROJECTION_STAGES,
    INGEST_CHAT_PROJECTION_STAGES,
    INGEST_PROJECTION_STAGES,
    RETRIEVAL_ONLY_PROJECTION_STAGES,
    ProjectionProfileError,
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

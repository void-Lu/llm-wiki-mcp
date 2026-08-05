from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import runtime.runtime_provenance as runtime_provenance
from runtime.runtime_provenance import (
    BuildMetadata,
    create_runtime_provenance,
)


FIXED_TIME = datetime(2026, 7, 27, 8, 30, tzinfo=timezone.utc)
FULL_REVISION = "a" * 40


def test_build_metadata_is_preferred_and_serialized_without_sensitive_fields():
    provenance = create_runtime_provenance(
        package_version="0.9.0",
        build_metadata_loader=lambda: BuildMetadata(
            revision=FULL_REVISION,
            dirty=True,
        ),
        source_root=Path("/unused"),
        revision_getter=lambda _path: (_ for _ in ()).throw(
            AssertionError("Git fallback must not run for build metadata")
        ),
        dirty_getter=lambda _path: False,
        clock=lambda: FIXED_TIME,
    )

    assert provenance.revision_source == "build"
    assert provenance.server_version == "0.9.0+aaaaaaaaaaaa.dirty"
    assert provenance.to_public_dict() == {
        "package_version": "0.9.0",
        "revision": FULL_REVISION,
        "dirty": True,
        "revision_source": "build",
        "started_at": "2026-07-27T08:30:00Z",
        "provenance_incomplete": False,
        "warnings": [],
    }


def test_editable_checkout_uses_git_revision_and_dirty_state():
    source_root = Path("/repo")
    provenance = create_runtime_provenance(
        package_version="0.9.0",
        build_metadata_loader=lambda: None,
        source_root=source_root,
        revision_getter=lambda path: FULL_REVISION if path == source_root else "",
        dirty_getter=lambda path: path == source_root,
        clock=lambda: FIXED_TIME,
    )

    assert provenance.revision == FULL_REVISION
    assert provenance.dirty is True
    assert provenance.revision_source == "editable"
    assert provenance.server_version == "0.9.0+aaaaaaaaaaaa.dirty"


def test_unknown_revision_is_non_blocking_and_structurally_warned():
    provenance = create_runtime_provenance(
        package_version="0.9.0",
        build_metadata_loader=lambda: None,
        source_root=None,
        revision_getter=lambda _path: "",
        dirty_getter=lambda _path: None,
        clock=lambda: FIXED_TIME,
    )

    assert provenance.revision is None
    assert provenance.dirty is None
    assert provenance.revision_source == "unknown"
    assert provenance.server_version == "0.9.0"
    assert provenance.to_public_dict()["revision"] == "unknown"
    assert provenance.to_public_dict()["provenance_incomplete"] is True
    assert "provenance_incomplete" in provenance.warnings


def test_invalid_build_metadata_does_not_fall_back_to_git():
    def invalid_build_metadata() -> BuildMetadata | None:
        raise ValueError("invalid build metadata")

    provenance = create_runtime_provenance(
        package_version="0.9.0",
        build_metadata_loader=invalid_build_metadata,
        source_root=Path("/repo"),
        revision_getter=lambda _path: (_ for _ in ()).throw(
            AssertionError("Corrupt build metadata must not be hidden by Git fallback")
        ),
        dirty_getter=lambda _path: False,
        clock=lambda: FIXED_TIME,
    )

    assert provenance.revision_source == "unknown"
    assert provenance.to_public_dict()["revision"] == "unknown"
    assert provenance.warnings == (
        "invalid_build_info",
        "provenance_incomplete",
    )


def test_broken_build_module_does_not_block_startup(monkeypatch):
    def broken_import(_module_name: str):
        raise SyntaxError("broken generated module")

    monkeypatch.setattr(runtime_provenance, "import_module", broken_import)

    provenance = create_runtime_provenance(
        package_version="0.9.0",
        build_metadata_loader=runtime_provenance._load_build_metadata,
        source_root=None,
        revision_getter=lambda _path: "",
        dirty_getter=lambda _path: None,
        clock=lambda: FIXED_TIME,
    )

    assert provenance.revision_source == "unknown"
    assert provenance.to_public_dict()["revision"] == "unknown"
    assert "invalid_build_info" in provenance.warnings


def test_started_at_is_stable_for_repeated_serialization():
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return FIXED_TIME

    provenance = create_runtime_provenance(
        package_version="0.9.0",
        build_metadata_loader=lambda: BuildMetadata(
            revision=FULL_REVISION,
            dirty=False,
        ),
        source_root=None,
        revision_getter=lambda _path: "",
        dirty_getter=lambda _path: None,
        clock=clock,
    )

    first = provenance.to_public_dict()
    second = provenance.to_public_dict()

    assert first["started_at"] == second["started_at"]
    assert clock_calls == 1

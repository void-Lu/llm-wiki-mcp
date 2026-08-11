from __future__ import annotations

from pathlib import Path

import pytest

from wiki.atomic_file import AtomicFileError, atomic_write_text


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_atomic_write_fault_before_replace_keeps_old_bytes(tmp_path: Path, stage: str) -> None:
    target = tmp_path / "page.md"
    target.write_bytes(b"old\r\nbytes")

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with pytest.raises(AtomicFileError) as error:
        atomic_write_text(target, "new\nbytes", fault=fault)

    assert error.value.code == "atomic_write_failed"
    assert target.read_bytes() == b"old\r\nbytes"
    assert list(tmp_path.glob(".page.md.*.tmp")) == []


def test_atomic_write_uses_same_directory_temp_and_reports_hash(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    observed: list[Path] = []

    def fault(stage: str) -> None:
        if stage == "temp_write":
            observed.extend(tmp_path.glob(".page.md.*.tmp"))
            raise RuntimeError("injected")

    with pytest.raises(AtomicFileError):
        atomic_write_text(target, "new", fault=fault)

    assert observed and all(path.parent == target.parent for path in observed)
    assert not target.exists()


def test_post_replace_fault_leaves_complete_new_file(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_text("old", encoding="utf-8")

    def fault(stage: str) -> None:
        if stage == "post_replace":
            raise RuntimeError("crash after replace")

    with pytest.raises(AtomicFileError):
        atomic_write_text(target, "new", fault=fault)

    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".page.md.*.tmp")) == []

from __future__ import annotations

from pathlib import Path

import pytest

from wiki.atomic_file import AtomicFileError, atomic_write_text, current_fault, fault_barrier, fault_context


@pytest.mark.parametrize("stage", ["temp_write", "flush", "replace"])
def test_atomic_write_fault_before_replace_keeps_old_bytes(tmp_path: Path, stage: str) -> None:
    target = tmp_path / "page.md"
    target.write_bytes(b"old\r\nbytes")

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with fault_context(fault):
        with pytest.raises(AtomicFileError) as error:
            atomic_write_text(target, "new\nbytes")

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

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            atomic_write_text(target, "new")

    assert observed and all(path.parent == target.parent for path in observed)
    assert not target.exists()


def test_post_replace_fault_leaves_complete_new_file(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    target.write_text("old", encoding="utf-8")

    def fault(stage: str) -> None:
        if stage == "post_replace":
            raise RuntimeError("crash after replace")

    with fault_context(fault):
        with pytest.raises(AtomicFileError):
            atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".page.md.*.tmp")) == []


def test_fault_context_nested_scope_restores_outer_barrier() -> None:
    observed: list[str] = []

    def outer(stage: str) -> None:
        observed.append(f"outer:{stage}")

    def inner(stage: str) -> None:
        observed.append(f"inner:{stage}")

    with fault_context(outer):
        assert current_fault() is outer
        current_fault()("before")
        with fault_context(inner):
            assert current_fault() is inner
            current_fault()("inside")
        assert current_fault() is outer
        current_fault()("after")

    assert current_fault() is fault_barrier
    assert observed == ["outer:before", "inner:inside", "outer:after"]


def test_atomic_write_without_fault_context_uses_noop_barrier(tmp_path: Path) -> None:
    target = tmp_path / "page.md"

    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"

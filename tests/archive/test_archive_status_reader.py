from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from archive.archive_service import ArchiveService
from archive.archive_status_reader import ArchiveStatusReader


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_missing_state_is_reported_without_creating_vault_files(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    before = _tree_snapshot(root)

    result = ArchiveStatusReader(root).status()

    assert result["ok"] is True
    assert result["state"] == "missing"
    assert result["code"] == "archive_state_missing"
    assert _tree_snapshot(root) == before
    assert not (root / ".llm-wiki" / "state.sqlite3").exists()


def test_existing_state_is_read_only_and_preserves_file_tree(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    ArchiveService(root)
    state_path = root / ".llm-wiki" / "state.sqlite3"
    before = _tree_snapshot(root)
    real_connect = sqlite3.connect
    calls: list[tuple[object, object]] = []

    def spy_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        calls.append((args[0] if args else None, kwargs.get("uri")))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("archive.archive_status_reader.sqlite3.connect", spy_connect)

    result = ArchiveStatusReader(root).status()

    assert result["ok"] is True
    assert result["state"] == "ready"
    assert result["code"] == "ready"
    assert result["operations"] == []
    assert result["tombstone_count"] == 0
    assert _tree_snapshot(root) == before
    assert calls and "mode=ro" in str(calls[0][0]) and calls[0][1] is True
    assert not (state_path.with_name("state.sqlite3-wal")).exists()
    assert not (state_path.with_name("state.sqlite3-shm")).exists()


def test_missing_schema_is_incompatible_without_migration(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    state_path = root / ".llm-wiki" / "state.sqlite3"
    state_path.parent.mkdir(parents=True)
    with sqlite3.connect(state_path) as connection:
        connection.execute("CREATE TABLE archive_operations(operation_id TEXT)")

    before = _tree_snapshot(root)
    result = ArchiveStatusReader(root).status()

    assert result["ok"] is False
    assert result["state"] == "incompatible"
    assert result["code"] == "archive_state_incompatible"
    assert "archive_plans" in result["missing_tables"]
    assert _tree_snapshot(root) == before


def test_corrupt_state_is_unavailable_or_incompatible_without_repair(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    state_path = root / ".llm-wiki" / "state.sqlite3"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"not a sqlite database")
    before = _tree_snapshot(root)

    result = ArchiveStatusReader(root).status()

    assert result["ok"] is False
    assert result["code"] in {"archive_state_unavailable", "archive_state_incompatible"}
    assert _tree_snapshot(root) == before

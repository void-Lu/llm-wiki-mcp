from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from wiki.ingest_service import ingest_file
from wiki.ingest_snapshot import IngestSnapshotError, IngestSnapshotter


def test_snapshot_opens_the_source_once_and_commits_exact_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"one\ntwo\n")
    target = tmp_path / "vault" / "raw" / "sources" / "file" / "source.md"
    original_open = Path.open
    opened: list[Path] = []

    def counted_open(path: Path, *args: Any, **kwargs: Any):
        if path == source:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    snapshot = IngestSnapshotter().snapshot(source)
    try:
        snapshot.commit_to(target)
    finally:
        snapshot.cleanup()

    assert opened == [source]
    assert target.read_bytes() == source.read_bytes()
    assert snapshot.content_hash == __import__("hashlib").sha256(target.read_bytes()).hexdigest()


def test_snapshot_rejects_replacement_without_leaving_a_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"old content")
    target = tmp_path / "vault" / "raw" / "sources" / "file" / "source.md"
    original_open = Path.open
    mutated = False

    def replacing_open(path: Path, *args: Any, **kwargs: Any):
        handle = original_open(path, *args, **kwargs)
        if path != source:
            return handle

        class Reader:
            def read(self, size: int = -1) -> bytes:
                nonlocal mutated
                data = handle.read(size)
                if data and not mutated:
                    mutated = True
                    fd = os.open(source, os.O_WRONLY | os.O_TRUNC)
                    try:
                        os.write(fd, b"new content")
                    finally:
                        os.close(fd)
                return data

            def fileno(self) -> int:
                return handle.fileno()

            def close(self) -> None:
                handle.close()

        return Reader()

    monkeypatch.setattr(Path, "open", replacing_open)
    with pytest.raises(IngestSnapshotError) as error:
        IngestSnapshotter().snapshot(source)

    assert error.value.code == "source_changed_during_read"
    assert not target.exists()


def test_ingest_source_delete_during_read_is_zero_target_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"content")
    original_open = Path.open
    deleted = False

    def deleting_open(path: Path, *args: Any, **kwargs: Any):
        handle = original_open(path, *args, **kwargs)
        if path != source:
            return handle

        class Reader:
            def read(self, size: int = -1) -> bytes:
                nonlocal deleted
                data = handle.read(size)
                if data and not deleted:
                    deleted = True
                return data

            def fileno(self) -> int:
                return handle.fileno()

            def close(self) -> None:
                handle.close()
                source.unlink(missing_ok=True)

        return Reader()

    monkeypatch.setattr(Path, "open", deleting_open)
    result = ingest_file(
        vault_root=tmp_path / "vault",
        source_path=source,
        source_name="source",
    )

    assert result["ok"] is False
    assert result["code"] == "source_changed_during_read"
    assert not (tmp_path / "vault").exists()

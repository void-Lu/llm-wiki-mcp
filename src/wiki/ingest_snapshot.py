"""Single-handle source snapshots for raw ingest and provenance checks."""

from __future__ import annotations

import codecs
import os
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import tempfile


TEXT_SOURCE_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
    }
)


class IngestSnapshotError(ValueError):
    """A stable failure from the source snapshot boundary."""

    def __init__(self, code: str, message: str = "source snapshot could not be completed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileIdentity:
    """Metadata used to detect replacement or mutation during one read."""

    device: int
    inode: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(getattr(value, "st_dev", 0) or 0),
            inode=int(getattr(value, "st_ino", 0) or 0),
            size_bytes=int(value.st_size),
            mtime_ns=int(getattr(value, "st_mtime_ns", 0) or 0),
            ctime_ns=int(getattr(value, "st_ctime_ns", 0) or 0),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }


@dataclass
class IngestSnapshot:
    """A verified source copy that is ready for an atomic target commit."""

    temp_path: Path
    content_hash: str
    size_bytes: int
    is_text: bool
    identity: FileIdentity
    _owned_temp: bool = True

    @property
    def sha256(self) -> str:
        """Alias used by provenance callers."""

        return self.content_hash

    @property
    def file_identity(self) -> FileIdentity:
        """Design-document spelling for the source identity."""

        return self.identity

    def commit_to(self, target: str | Path) -> None:
        """Atomically replace *target* with the verified temporary copy."""

        if not self._owned_temp or not self.temp_path.exists():
            raise IngestSnapshotError("snapshot_already_committed")
        destination = Path(target)
        target_temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=".llm-wiki-target-", suffix=".tmp", dir=destination.parent)
            target_temporary = Path(temporary_name)
            with self.temp_path.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(target_temporary, destination)
        except OSError as exc:
            _unlink_quietly(target_temporary)
            self.cleanup()
            raise IngestSnapshotError("snapshot_write_failed") from exc
        except Exception:
            _unlink_quietly(target_temporary)
            self.cleanup()
            raise IngestSnapshotError("snapshot_write_failed")
        _unlink_quietly(self.temp_path)
        self._owned_temp = False

    def cleanup(self) -> None:
        if not self._owned_temp:
            return
        try:
            self.temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._owned_temp = False


class IngestSnapshotter:
    """Read a source once and produce a verified, atomically committable copy.

    The source path is opened exactly once.  Type detection, byte count, hash
    and the temporary copy all consume that same handle.  ``stat``/``fstat``
    calls validate the handle and the path without reopening the source.
    """

    chunk_size = 1024 * 1024

    def snapshot(self, source: str | Path) -> IngestSnapshot:
        source_path = Path(source).expanduser()
        temporary_path: Path | None = None
        try:
            initial_stat = source_path.stat()
        except FileNotFoundError as exc:
            raise IngestSnapshotError("source_not_file") from exc
        except OSError as exc:
            raise IngestSnapshotError("source_read_failed") from exc
        if not _is_regular_file(initial_stat):
            raise IngestSnapshotError("source_not_file")
        initial_identity = FileIdentity.from_stat(initial_stat)

        try:
            source_handle = source_path.open("rb")
        except FileNotFoundError as exc:
            raise IngestSnapshotError("source_not_file") from exc
        except OSError as exc:
            raise IngestSnapshotError("source_read_failed") from exc

        try:
            try:
                opened_identity = FileIdentity.from_stat(os.fstat(source_handle.fileno()))
            except (OSError, ValueError) as exc:
                raise IngestSnapshotError("source_read_failed") from exc
            if not _same_identity(initial_identity, opened_identity):
                raise IngestSnapshotError("source_changed_during_read")

            fd, temporary_name = tempfile.mkstemp(prefix=".llm-wiki-ingest-", suffix=".tmp")
            temporary_path = Path(temporary_name)
            digest = sha256()
            size_bytes = 0
            text_candidate = source_path.suffix.casefold() in TEXT_SOURCE_SUFFIXES
            utf8_decoder = codecs.getincrementaldecoder("utf-8")() if text_candidate else None
            text_valid = text_candidate

            with os.fdopen(fd, "wb") as temporary_handle:
                for block in iter(lambda: source_handle.read(self.chunk_size), b""):
                    temporary_handle.write(block)
                    digest.update(block)
                    size_bytes += len(block)
                    if text_valid and utf8_decoder is not None:
                        if b"\x00" in block:
                            text_valid = False
                        else:
                            try:
                                utf8_decoder.decode(block, final=False)
                            except UnicodeDecodeError:
                                text_valid = False
                if text_valid and utf8_decoder is not None:
                    try:
                        utf8_decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        text_valid = False
                temporary_handle.flush()
                os.fsync(temporary_handle.fileno())

            try:
                final_handle_identity = FileIdentity.from_stat(os.fstat(source_handle.fileno()))
            except (OSError, ValueError) as exc:
                raise IngestSnapshotError("source_changed_during_read") from exc
            if not _same_identity(initial_identity, final_handle_identity) or final_handle_identity.size_bytes != size_bytes:
                raise IngestSnapshotError("source_changed_during_read")
        except IngestSnapshotError:
            _unlink_quietly(temporary_path)
            raise
        except OSError as exc:
            _unlink_quietly(temporary_path)
            raise IngestSnapshotError("source_read_failed") from exc
        finally:
            source_handle.close()

        try:
            current_stat = source_path.stat()
        except FileNotFoundError as exc:
            _unlink_quietly(temporary_path)
            raise IngestSnapshotError("source_changed_during_read") from exc
        except OSError as exc:
            _unlink_quietly(temporary_path)
            raise IngestSnapshotError("source_read_failed") from exc
        current_identity = FileIdentity.from_stat(current_stat)
        if not _same_identity(initial_identity, current_identity) or current_identity.size_bytes != size_bytes:
            _unlink_quietly(temporary_path)
            raise IngestSnapshotError("source_changed_during_read")
        assert temporary_path is not None
        return IngestSnapshot(
            temp_path=temporary_path,
            content_hash=digest.hexdigest(),
            size_bytes=size_bytes,
            is_text=text_valid,
            identity=current_identity,
        )


def _is_regular_file(value: os.stat_result) -> bool:
    import stat

    return stat.S_ISREG(value.st_mode)


def _same_identity(left: FileIdentity, right: FileIdentity) -> bool:
    if left.device and right.device and left.device != right.device:
        return False
    if left.inode and right.inode and left.inode != right.inode:
        return False
    return (
        left.size_bytes == right.size_bytes
        and left.mtime_ns == right.mtime_ns
    )


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "FileIdentity",
    "IngestSnapshot",
    "IngestSnapshotError",
    "IngestSnapshotter",
    "TEXT_SOURCE_SUFFIXES",
]

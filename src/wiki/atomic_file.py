"""Crash-safe same-directory file replacement primitives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Callable


FaultBarrier = Callable[[str], None]


class AtomicFileError(ValueError):
    """A stable failure from the atomic page-file boundary."""

    def __init__(self, code: str, message: str = "atomic file write failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AtomicWriteResult:
    path: str
    content_hash: str
    size_bytes: int
    replaced: bool = True


def fault_barrier(stage: str) -> None:
    """Default no-op barrier that deterministic tests may monkeypatch."""

    del stage


def atomic_write_text(
    target: str | Path,
    text: str,
    *,
    fault: FaultBarrier | None = None,
) -> AtomicWriteResult:
    return atomic_write_bytes(target, text.encode("utf-8"), fault=fault)


def atomic_write_bytes(
    target: str | Path,
    content: bytes,
    *,
    fault: FaultBarrier | None = None,
) -> AtomicWriteResult:
    """Write bytes to a same-directory temp file and atomically replace target."""

    destination = Path(target)
    temporary: Path | None = None
    digest = hashlib.sha256(content).hexdigest()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            _invoke_fault("temp_write", fault)
            handle.flush()
            _invoke_fault("flush", fault)
            os.fsync(handle.fileno())
        _invoke_fault("replace", fault)
        os.replace(temporary, destination)
        temporary = None
        _invoke_fault("post_replace", fault)
    except AtomicFileError:
        _unlink_quietly(temporary)
        raise
    except OSError as exc:
        _unlink_quietly(temporary)
        raise AtomicFileError("atomic_write_failed") from exc
    except Exception as exc:
        _unlink_quietly(temporary)
        raise AtomicFileError("atomic_write_failed") from exc
    return AtomicWriteResult(
        path=destination.as_posix(),
        content_hash=digest,
        size_bytes=len(content),
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _invoke_fault(stage: str, fault: FaultBarrier | None) -> None:
    (fault or fault_barrier)(stage)


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "AtomicFileError",
    "AtomicWriteResult",
    "FaultBarrier",
    "atomic_write_bytes",
    "atomic_write_text",
    "fault_barrier",
    "sha256_file",
]

"""Crash-safe same-directory file replacement primitives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator


FaultBarrier = Callable[[str], None]
_FAULT_CONTEXT: ContextVar[FaultBarrier | None] = ContextVar("fault_context", default=None)


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


@contextmanager
def fault_context(fault: FaultBarrier | None) -> Iterator[None]:
    """Temporarily install the fault barrier for the current execution context.

    The context-local value is restored with its token on exit, so nested
    injections override only their inner scope and never leak into a later
    request.  Production write/projection code reads the barrier through
    :func:`current_fault` instead of carrying it through every signature.
    """

    token = _FAULT_CONTEXT.set(fault)
    try:
        yield
    finally:
        _FAULT_CONTEXT.reset(token)


def current_fault() -> FaultBarrier:
    """Return the active barrier, or the stable no-op default when unset."""

    barrier = _FAULT_CONTEXT.get()
    return fault_barrier if barrier is None else barrier


def atomic_write_text(target: str | Path, text: str) -> AtomicWriteResult:
    return atomic_write_bytes(target, text.encode("utf-8"))


def atomic_write_bytes(target: str | Path, content: bytes) -> AtomicWriteResult:
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
            _invoke_fault("temp_write")
            handle.flush()
            _invoke_fault("flush")
            os.fsync(handle.fileno())
        _invoke_fault("replace")
        os.replace(temporary, destination)
        temporary = None
        _invoke_fault("post_replace")
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


def _invoke_fault(stage: str) -> None:
    current_fault()(stage)


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
    "current_fault",
    "fault_barrier",
    "fault_context",
    "sha256_file",
]

"""Strict, hash-backed provenance resolution for formal Wiki pages."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Mapping

from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki.ingest_snapshot import FileIdentity, IngestSnapshotError, IngestSnapshotter
from wiki.wiki_paths import WikiPathError, safe_segment, translate_path_error


class SourceProvenanceError(ValueError):
    """A stable failure while resolving a formal page source."""

    def __init__(self, code: str, message: str = "source provenance could not be resolved") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedRawSource:
    """The server-owned identity of one raw source snapshot."""

    relative_path: str
    path_key: str
    sha256: str
    size_bytes: int
    file_identity: FileIdentity

    @property
    def path(self) -> str:
        return self.relative_path

    @property
    def source_hash(self) -> str:
        return self.sha256

    @property
    def content_hash(self) -> str:
        return self.sha256

    @property
    def identity(self) -> FileIdentity:
        return self.file_identity

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "path_key": self.path_key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "file_identity": self.file_identity.as_dict(),
        }


class SourceProvenanceResolver:
    """Resolve only concrete files below ``raw/sources`` in one vault."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.snapshotter = IngestSnapshotter()

    def resolve(self, value: object) -> ResolvedRawSource:
        relative = _normalize_source_locator(value)
        relative_path = Path(*relative.split("/"))
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise SourceProvenanceError("source_path_escape")
        resolved_relative = candidate.relative_to(self.root).as_posix().split("/")
        if len(resolved_relative) < 3 or resolved_relative[0].casefold() != "raw" or resolved_relative[1].casefold() != "sources":
            raise SourceProvenanceError("source_path_not_allowed")
        try:
            stat_value = candidate.stat()
        except FileNotFoundError as exc:
            raise SourceProvenanceError("source_not_found") from exc
        except OSError as exc:
            raise SourceProvenanceError("source_read_failed") from exc
        if not _is_regular_file(stat_value):
            raise SourceProvenanceError("source_not_file")
        try:
            snapshot = self.snapshotter.snapshot(candidate)
        except IngestSnapshotError as exc:
            raise SourceProvenanceError(exc.code) from exc
        try:
            return ResolvedRawSource(
                relative_path=relative,
                path_key=source_path_key(relative),
                sha256=snapshot.content_hash,
                size_bytes=snapshot.size_bytes,
                file_identity=snapshot.identity,
            )
        finally:
            snapshot.cleanup()

    def resolve_many(self, values: object) -> list[ResolvedRawSource]:
        if isinstance(values, (str, Path)):
            items: list[object] = [values]
        elif isinstance(values, Iterable) and not isinstance(values, Mapping):
            items = list(values)
        else:
            raise SourceProvenanceError("invalid_sources")
        if not items:
            raise SourceProvenanceError("source_required")

        resolved: list[ResolvedRawSource] = []
        seen: set[str] = set()
        for item in items:
            current = self.resolve(item)
            if current.path_key in seen:
                continue
            seen.add(current.path_key)
            resolved.append(current)
        return resolved

    def verify(self, sources: Iterable[ResolvedRawSource]) -> list[ResolvedRawSource]:
        """Re-read and compare a preflight result immediately before a write."""

        expected = list(sources)
        if not expected:
            return []
        try:
            current = self.resolve_many([item.relative_path for item in expected])
        except SourceProvenanceError as exc:
            if exc.code in {"source_not_found", "source_not_file", "source_read_failed", "source_changed_during_read", "source_path_escape"}:
                raise SourceProvenanceError("source_changed") from exc
            raise
        current_by_key = {item.path_key: item for item in current}
        for item in expected:
            candidate = current_by_key.get(item.path_key)
            if candidate is None or not _same_file_snapshot(item, candidate):
                raise SourceProvenanceError("source_changed")
        return current

def source_hash_map(sources: Iterable[ResolvedRawSource]) -> dict[str, str]:
    return {source.relative_path: source.sha256 for source in sources}


def source_path_key(relative_path: str | Path) -> str:
    """Return the platform-normalized key used by dependency edges."""

    normalized = normalize_vault_relative(relative_path)
    return os.path.normcase(os.fspath(Path(*normalized.split("/"))))


def _normalize_source_locator(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise SourceProvenanceError("source_path_not_allowed")
    text = str(value).replace("\\", "/")
    if not text:
        raise SourceProvenanceError("source_required")
    if _is_absolute(text):
        raise SourceProvenanceError("source_path_not_allowed")
    parts = text.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise SourceProvenanceError("path_escape")
    try:
        normalized = normalize_vault_relative(text)
    except LocatorError as exc:
        raise SourceProvenanceError(exc.code) from exc
    normalized_parts = normalized.split("/")
    if len(normalized_parts) < 3 or normalized_parts[0].casefold() != "raw" or normalized_parts[1].casefold() != "sources":
        raise SourceProvenanceError("source_path_not_allowed")
    try:
        for part in normalized_parts:
            safe_segment(part)
    except WikiPathError as exc:
        raise SourceProvenanceError(translate_path_error(exc.code, "provenance")) from exc
    return normalized


def _is_absolute(value: str) -> bool:
    return value.startswith(("/", "\\")) or (len(value) >= 2 and value[1] == ":")


def _is_regular_file(value: os.stat_result) -> bool:
    import stat

    return stat.S_ISREG(value.st_mode)


def _same_file_snapshot(left: ResolvedRawSource, right: ResolvedRawSource) -> bool:
    return (
        left.path_key == right.path_key
        and left.sha256 == right.sha256
        and left.size_bytes == right.size_bytes
        and left.file_identity.device == right.file_identity.device
        and left.file_identity.inode == right.file_identity.inode
        and left.file_identity.mtime_ns == right.file_identity.mtime_ns
    )


__all__ = [
    "ResolvedRawSource",
    "SourceProvenanceError",
    "SourceProvenanceResolver",
    "source_hash_map",
    "source_path_key",
]

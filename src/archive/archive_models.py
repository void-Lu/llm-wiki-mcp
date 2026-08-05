"""Typed, serialisable contracts for the immutable archive lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypeGuard


ArchiveReason = Literal["superseded", "deprecated", "retention", "migration", "manual"]
ARCHIVE_REASONS = frozenset({"superseded", "deprecated", "retention", "migration", "manual"})
OperationState = Literal["planned", "staged", "pending", "detaching", "committed", "rolling_back", "rolled_back", "failed_recoverable"]


def is_archive_reason(value: object) -> TypeGuard[ArchiveReason]:
    return isinstance(value, str) and value in ARCHIVE_REASONS


class ArchiveError(RuntimeError):
    """A stable domain error suitable for CLI and MCP responses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArchiveItem:
    original_path: str
    archive_path: str
    content_hash: str
    kind: Literal["knowledge", "raw"]
    dependencies: tuple[str, ...] = ()
    passage_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["dependencies"] = list(self.dependencies)
        result["passage_ids"] = list(self.passage_ids)
        return result


@dataclass(frozen=True)
class ArchiveAttachment:
    """An immutable, non-searchable file carried by an archive bundle."""

    archive_path: str
    content_hash: str
    content: str | None = None

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "archive_path": self.archive_path,
            "content_hash": self.content_hash,
        }
        if include_content and self.content is not None:
            result["content"] = self.content
        return result


@dataclass(frozen=True)
class ArchiveManifest:
    archive_id: str
    operation_id: str
    reason: ArchiveReason
    archived_at: str
    items: tuple[ArchiveItem, ...]
    actor: str = "unknown"
    replaced_by: str | None = None
    restorable: bool = True
    schema_version: int = 1
    dependencies: tuple[str, ...] = ()
    passage_ids: tuple[str, ...] = ()
    attachments: tuple[ArchiveAttachment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "archive_id": self.archive_id,
            "operation_id": self.operation_id,
            "reason": self.reason,
            "archived_at": self.archived_at,
            "replaced_by": self.replaced_by,
            "items": [item.to_dict() for item in sorted(self.items, key=lambda value: value.original_path)],
            "dependencies": list(self.dependencies),
            "passage_ids": list(self.passage_ids),
            "actor": self.actor,
            "restorable": self.restorable,
            "attachments": [attachment.to_dict() for attachment in sorted(self.attachments, key=lambda value: value.archive_path)],
        }


@dataclass(frozen=True)
class ArchivePlan:
    plan_id: str
    operation_type: Literal["archive", "restore"]
    archive_id: str | None
    created_at: str
    expires_at: str
    items: tuple[ArchiveItem, ...]
    plan_hash: str
    reason: ArchiveReason | None = None
    blockers: tuple[dict[str, Any], ...] = ()
    cascade: bool = False
    force_namespace: str | None = None
    restorable: bool = True
    attachments: tuple[ArchiveAttachment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.blockers,
            "plan_id": self.plan_id,
            "operation_type": self.operation_type,
            "archive_id": self.archive_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "items": [item.to_dict() for item in self.items],
            "plan_hash": self.plan_hash,
            "blockers": list(self.blockers),
            "cascade": self.cascade,
            "force_namespace": self.force_namespace,
            "restorable": self.restorable,
            "attachments": [attachment.to_dict(include_content=True) for attachment in sorted(self.attachments, key=lambda value: value.archive_path)],
        }


@dataclass(frozen=True)
class Tombstone:
    archive_id: str
    purged_at: str
    reason: str
    path_hashes: tuple[str, ...] = field(default_factory=tuple)
    forget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"archive_id": self.archive_id, "purged_at": self.purged_at, "reason": self.reason, "path_hashes": list(self.path_hashes), "forget": self.forget}

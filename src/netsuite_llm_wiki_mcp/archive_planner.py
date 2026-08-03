"""Eligibility, dependency checks and deterministic archive/restore plans."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping
from uuid import uuid4

from netsuite_llm_wiki_mcp.archive_manifest import content_hash, load_manifest, vault_relative
from netsuite_llm_wiki_mcp.archive_models import ArchiveAttachment, ArchiveError, ArchiveItem, ArchivePlan, ArchiveReason, is_archive_reason
from netsuite_llm_wiki_mcp.knowledge_dependencies import KnowledgeDependencies
from netsuite_llm_wiki_mcp.wiki_io import split_frontmatter


class ArchivePlanner:
    def __init__(self, vault_root: str | Path, *, clock: Callable[[], datetime] | None = None, plan_ttl: timedelta = timedelta(minutes=15)) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.plan_ttl = plan_ttl
        self.dependencies = KnowledgeDependencies(self.root)

    def archive_plan(
        self,
        targets: str | Iterable[str],
        *,
        reason: str = "manual",
        cascade: bool = False,
        actor: str = "unknown",
        explicit: bool = True,
        force_namespace: str | None = None,
        restorable: bool = True,
        attachments: Mapping[str, str] | None = None,
    ) -> ArchivePlan:
        del actor
        if not is_archive_reason(reason):
            return self._plan("archive", (), blockers=[{"code": "invalid_archive_reason", "reason": reason}])
        namespace = force_namespace.replace("\\", "/").rstrip("/") if force_namespace else None
        blockers: list[dict[str, object]] = []
        if namespace and (not namespace.startswith("wiki/") or namespace in {"wiki", "wiki/"}):
            blockers.append({"code": "invalid_force_namespace", "namespace": namespace})
            namespace = None
        archive_attachments, attachment_blockers = self._attachments(attachments)
        blockers.extend(attachment_blockers)
        values = [targets] if isinstance(targets, str) else list(targets)
        selected: dict[str, ArchiveItem] = {}
        for target in sorted(set(values)):
            try:
                path, rel = self._active_path(target)
            except ArchiveError as exc:
                blockers.append({"code": exc.code, "target": target}); continue
            if not path.is_file():
                blockers.append({"code": "archive_target_missing", "target": rel}); continue
            kind = "raw" if rel.startswith("raw/") else "knowledge"
            if kind == "knowledge":
                blockers.extend(self._knowledge_blockers(path, rel, explicit=explicit, force_namespace=namespace))
            else:
                dependents = self.dependencies.dependents(rel)
                if dependents and not cascade:
                    blockers.append({"code": "archive_dependency_blocked", "target": rel, "dependents": dependents})
                if cascade:
                    for dependent in dependents:
                        try:
                            page, page_rel = self._active_path(dependent)
                            blockers.extend(self._knowledge_blockers(page, page_rel, explicit=explicit, force_namespace=namespace))
                            if page.is_file(): selected[page_rel] = self._item(page, page_rel, "knowledge")
                        except ArchiveError as exc:
                            blockers.append({"code": exc.code, "target": dependent})
            selected[rel] = self._item(path, rel, kind)
        selected_paths = set(selected)
        for attachment in archive_attachments:
            if attachment.archive_path in selected_paths:
                blockers.append({"code": "invalid_archive_attachment", "path": attachment.archive_path})
        return self._plan(
            "archive",
            tuple(selected.values()),
            reason=reason,
            blockers=blockers,
            cascade=cascade,
            force_namespace=namespace,
            restorable=restorable,
            attachments=archive_attachments,
        )

    def restore_plan(self, archive_id: str, *, targets: Iterable[str] | None = None) -> ArchivePlan:
        bundle = self._bundle(archive_id)
        blockers: list[dict[str, object]] = []
        try:
            manifest = load_manifest(bundle)
        except ArchiveError as exc:
            return self._plan("restore", (), archive_id=archive_id, blockers=[{"code": exc.code}])
        wanted = set(targets or [item.original_path for item in manifest.items])
        items = tuple(item for item in manifest.items if item.original_path in wanted)
        if not items:
            blockers.append({"code": "restore_target_missing", "archive_id": archive_id})
        if not manifest.restorable:
            blockers.append({"code": "archive_not_restorable", "archive_id": archive_id})
        for item in items:
            destination = self.root / item.original_path
            if destination.exists() and destination.is_file() and content_hash(destination) != item.content_hash:
                blockers.append({"code": "restore_target_conflict", "target": item.original_path})
        return self._plan(
            "restore",
            items,
            archive_id=archive_id,
            blockers=blockers,
            restorable=manifest.restorable,
            attachments=manifest.attachments,
        )

    def _knowledge_blockers(self, path: Path, rel: str, *, explicit: bool, force_namespace: str | None = None) -> list[dict[str, object]]:
        if force_namespace and (rel == force_namespace or rel.startswith(force_namespace + "/")):
            return []
        try:
            frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return [{"code": "archive_content_unreadable", "target": rel}]
        generated = frontmatter.get("generated") is True
        lifecycle = str(frontmatter.get("lifecycle") or frontmatter.get("status") or "")
        if not generated and not explicit:
            return [{"code": "manual_page_protected", "target": rel}]
        if lifecycle in {"stale", "review_required"}:
            return [{"code": "archive_lifecycle_ineligible", "target": rel, "lifecycle": lifecycle}]
        if generated and lifecycle not in {"superseded", "deprecated", "archive-ready"}:
            return [{"code": "archive_lifecycle_ineligible", "target": rel, "lifecycle": lifecycle or "active"}]
        if lifecycle == "superseded" and not frontmatter.get("replaced_by"):
            return [{"code": "replaced_by_required", "target": rel}]
        return []

    def _active_path(self, target: str) -> tuple[Path, str]:
        candidate = self.root / target.replace("\\", "/")
        rel = vault_relative(self.root, candidate)
        if not (rel.startswith("wiki/") or rel.startswith("raw/")):
            raise ArchiveError("invalid_archive_target", "only active wiki or raw paths may be archived")
        if rel in {"wiki/index.md", "wiki/log.md", "wiki/overview.md"}:
            raise ArchiveError("structural_page_protected", "structural pages cannot be archived")
        return candidate, rel

    def _bundle(self, archive_id: str) -> Path:
        matches = sorted((self.root / "archives" / "bundles").glob(f"*/*/{archive_id}"))
        if len(matches) != 1:
            raise ArchiveError("archive_not_found", "archive id was not found")
        return matches[0]

    @staticmethod
    def _item(path: Path, rel: str, kind: str) -> ArchiveItem:
        return ArchiveItem(rel, rel, content_hash(path), kind)  # type: ignore[arg-type]

    @staticmethod
    def _attachments(values: Mapping[str, str] | None) -> tuple[tuple[ArchiveAttachment, ...], list[dict[str, object]]]:
        if not values:
            return (), []
        attachments: list[ArchiveAttachment] = []
        blockers: list[dict[str, object]] = []
        for raw_path, content in sorted(values.items()):
            path = str(raw_path).replace("\\", "/")
            path_value = Path(path)
            if (
                not path
                or path == "manifest.yaml"
                or path_value.is_absolute()
                or "\\" in str(raw_path)
                or any(part in {"", ".", ".."} for part in path_value.parts)
            ):
                blockers.append({"code": "invalid_archive_attachment", "path": path})
                continue
            if not isinstance(content, str):
                blockers.append({"code": "invalid_archive_attachment", "path": path})
                continue
            digest = "sha256:" + sha256(content.encode("utf-8")).hexdigest()
            attachments.append(ArchiveAttachment(path, digest, content))
        return tuple(attachments), blockers

    def _plan(
        self,
        operation_type: Literal["archive", "restore"],
        items: tuple[ArchiveItem, ...],
        *,
        reason: ArchiveReason | None = None,
        archive_id: str | None = None,
        blockers: list[dict[str, object]] | None = None,
        cascade: bool = False,
        force_namespace: str | None = None,
        restorable: bool = True,
        attachments: tuple[ArchiveAttachment, ...] = (),
    ) -> ArchivePlan:
        created = self.clock().astimezone(UTC)
        stable = {
            "operation_type": operation_type,
            "archive_id": archive_id,
            "reason": reason,
            "items": [item.to_dict() for item in sorted(items, key=lambda value: value.original_path)],
            "cascade": cascade,
            "force_namespace": force_namespace,
            "restorable": restorable,
            "attachments": [attachment.to_dict() for attachment in sorted(attachments, key=lambda value: value.archive_path)],
        }
        digest = "sha256:" + sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ArchivePlan(
            plan_id=uuid4().hex,
            operation_type=operation_type,
            archive_id=archive_id,
            created_at=created.isoformat(),
            expires_at=(created + self.plan_ttl).isoformat(),
            items=tuple(sorted(items, key=lambda value: value.original_path)),
            plan_hash=digest,
            reason=reason,
            blockers=tuple(blockers or ()),
            cascade=cascade,
            force_namespace=force_namespace,
            restorable=restorable,
            attachments=tuple(sorted(attachments, key=lambda value: value.archive_path)),
        )

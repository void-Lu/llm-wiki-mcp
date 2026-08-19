"""Administrative privacy audit plans for historical Wiki pages.

The service reports display redaction and locator impact without putting page
body text in a plan.  Applying a plan is an explicit CAS operation.  Locator
changes (renames and wikilinks) are refused unless the administrator opts in;
there is no implicit history or journal rewrite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Iterable, Mapping

from common.privacy_policy import LocatorError, PrivacyPolicy, field_class, redact_storage_value
from common.redaction import count_redaction_categories, redact_sensitive_text
from retrieval.retrieval_index import RetrievalIndexStore
from wiki.atomic_file import AtomicFileError, atomic_write_bytes, atomic_write_text, sha256_file
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import derive_page_policy
from wiki.projection_profile import projection_stages
from wiki.repair_plan import (
    RepairPageContext,
    RepairPlanHooks,
    RepairPlanOwner,
    iter_admin_page_files,
    projection_warning,
    safe_file_hash,
    string_map,
)
from wiki.wiki_io import render_page, split_frontmatter
from wiki.wiki_paths import KNOWLEDGE_DEPENDENCIES_DB, PRIVACY_AUDIT_DIR, admin_wiki_page_file


class PrivacyAuditError(ValueError):
    """Stable administrative privacy audit failure."""

    def __init__(self, code: str, message: str = "privacy audit failed") -> None:
        super().__init__(message)
        self.code = code


_WIKILINK = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")


class PrivacyAuditService:
    """Plan/apply display redaction while preserving identity fields."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.policy = PrivacyPolicy()
        self.plan_owner = RepairPlanOwner(
            self.root,
            kind="privacy_audit",
            plan_prefix="privacy-audit-",
            audit_dir=PRIVACY_AUDIT_DIR,
            error_type=PrivacyAuditError,
        )

    def plan(self) -> dict[str, object]:
        raw_pages: dict[str, dict[str, object]] = {}
        for path in self._iter_page_files():
            page_path = path.relative_to(self.root).as_posix()
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                frontmatter, body = split_frontmatter(text)
            except (OSError, UnicodeDecodeError):
                raw_pages[page_path] = {
                    "page_path": page_path,
                    "expected_page_hash": safe_file_hash(path),
                    "issues": ["page_read_failed"],
                    "hit_fields": [],
                    "redaction_categories": {},
                    "body_redaction_count": 0,
                    "filename_change": None,
                    "wikilink_changes": [],
                    "locator_review_required": True,
                    "has_frontmatter": False,
                }
                continue
            try:
                projected = redact_storage_value(frontmatter, policy=self.policy)
                projected_frontmatter = projected if isinstance(projected, dict) else {}
                issues: list[str] = []
            except LocatorError as exc:
                projected_frontmatter = frontmatter
                issues = [exc.code]
            redacted_body = redact_sensitive_text(body)
            hit_fields = _changed_fields(frontmatter, projected_frontmatter)
            if redacted_body != body:
                hit_fields.append("body")
            filename_change = _filename_change(page_path, hashlib.sha256(raw).hexdigest())
            raw_pages[page_path] = {
                "page_path": page_path,
                "expected_page_hash": hashlib.sha256(raw).hexdigest(),
                "hit_fields": sorted(set(hit_fields)),
                "redaction_categories": count_redaction_categories(body, redacted_body),
                "body_redaction_count": sum(count_redaction_categories(body, redacted_body).values()),
                "filename_change": filename_change,
                "wikilink_changes": [],
                "locator_review_required": bool(issues or filename_change),
                "issues": sorted(set(issues)),
                "has_frontmatter": bool(frontmatter),
            }

        filename_map: dict[str, str] = {}
        for old, entry in raw_pages.items():
            filename_change = entry.get("filename_change")
            if not isinstance(filename_change, Mapping):
                continue
            new_page_path = filename_change.get("new_page_path")
            if new_page_path:
                filename_map[old] = str(new_page_path)
        for page_path, entry in raw_pages.items():
            try:
                text = (self.root / Path(*page_path.split("/"))).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            changes = _wikilink_changes(text, filename_map)
            if changes:
                entry["wikilink_changes"] = changes
                entry["locator_review_required"] = True

        entries = [
            entry
            for entry in raw_pages.values()
            if entry.get("hit_fields") or entry.get("filename_change") or entry.get("wikilink_changes") or entry.get("issues")
        ]
        plan = self.plan_owner.create_plan(
            {
                "filename_changes": [{"old_page_path": old, "new_page_path": new} for old, new in sorted(filename_map.items())],
                "entries": entries,
                "summary": _summary(entries),
            }
        )
        return self.plan_owner.plan_response(plan)

    def apply(self, plan_id: str, *, allow_locator_changes: bool = False) -> dict[str, object]:
        plan = self.plan_owner.read_plan(plan_id)
        if self.plan_owner.already_applied(plan_id):
            return {"ok": True, "already_applied": True, "plan_id": plan_id, "audit_state": "applied"}

        raw_entries = plan.get("entries", [])
        entries = [entry for entry in raw_entries if isinstance(entry, Mapping)] if isinstance(raw_entries, list) else []
        locator_entries = [entry for entry in entries if _has_locator_change(entry)]
        if locator_entries and not allow_locator_changes:
            return {
                "ok": False,
                "code": "privacy_locator_review_required",
                "plan_id": plan_id,
                "writes": 0,
                "locator_changes": len(locator_entries),
            }

        raw_filename_changes = plan.get("filename_changes", [])
        filename_changes = raw_filename_changes if isinstance(raw_filename_changes, list) else []
        filename_map = {
            str(item["old_page_path"]): str(item["new_page_path"])
            for item in filename_changes
            if isinstance(item, Mapping) and item.get("old_page_path") and item.get("new_page_path")
        }
        old_paths = {str(entry.get("page_path", "")) for entry in entries}
        for old, new in filename_map.items():
            if new in old_paths and new != old:
                return {"ok": False, "code": "privacy_locator_rename_collision", "plan_id": plan_id, "writes": 0}

        dependency: KnowledgeDependencies | None = None
        db_path = self.root / KNOWLEDGE_DEPENDENCIES_DB
        if db_path.is_file():
            dependency = KnowledgeDependencies(self.root)
        def preflight(items: list[Mapping[str, object]]) -> Mapping[str, object] | None:
            for item in items:
                page_path = str(item.get("page_path", ""))
                source = self.plan_owner.page_file(page_path)
                target = self.plan_owner.page_file(filename_map.get(page_path, page_path), allow_missing=True)
                if target != source and target.exists():
                    return {"ok": False, "code": "privacy_locator_rename_collision", "writes": 0}
            return None

        hooks = RepairPlanHooks(
            prepare=lambda context: self._prepare_page(context, filename_map, dependency),
            apply=lambda context: self._apply_page(context, filename_map, dependency),
            rollback=lambda context: self._rollback_page(context, dependency),
        )
        return self.plan_owner.apply(
            plan_id,
            plan=plan,
            hooks=hooks,
            entries=entries,
            preflight=preflight,
            cas_code="privacy_audit_cas_mismatch",
            rollback_code="privacy_audit_rolled_back",
            rollback_failed_code="privacy_audit_rollback_failed",
            response_fields={"history_written": False},
            audit_fields={"allow_locator_changes": allow_locator_changes, "history_written": False},
        )

    def _prepare_page(
        self,
        context: RepairPageContext,
        filename_map: Mapping[str, str],
        dependency: KnowledgeDependencies | None,
    ) -> None:
        target = admin_wiki_page_file(
            self.root,
            filename_map.get(context.page_path, context.page_path),
            allow_missing=True,
        )
        context.metadata["target"] = target
        if dependency is not None:
            context.metadata["projection"] = KnowledgeDependencies.read_page_projection(self.root, context.page_path)

    def _apply_page(
        self,
        context: RepairPageContext,
        filename_map: Mapping[str, str],
        dependency: KnowledgeDependencies | None,
    ) -> dict[str, object]:
        target = context.metadata["target"]
        if not isinstance(target, Path):
            raise PrivacyAuditError("page_not_found")
        transformed = _transform_text(context.original.decode("utf-8"), filename_map)
        renamed = target != context.source
        if transformed.encode("utf-8") != context.original or renamed:
            atomic_write_bytes(target, transformed.encode("utf-8"))
            context.wrote = True
        after_hash = sha256_file(target)
        if dependency is not None:
            frontmatter, _ = split_frontmatter(transformed)
            desired_hashes = string_map(frontmatter.get("source_hashes"))
            policy = derive_page_policy(frontmatter)
            dependency.update_page(
                target.relative_to(self.root).as_posix(),
                after_hash,
                desired_hashes,
                policy=policy,
            )
            if target != context.source:
                dependency.remove_page(context.page_path)
        if renamed:
            context.source.unlink()
        warning = self._refresh_retrieval(target, context.page_path, old_page_path=context.page_path if renamed else None)
        if warning is not None:
            context.add_warning(warning)
        return {
            "page_path": context.page_path,
            "after_page_path": target.relative_to(self.root).as_posix(),
            "before_page_hash": hashlib.sha256(context.original).hexdigest(),
            "after_page_hash": after_hash,
            "hit_fields": context.entry.get("hit_fields", []),
            "filename_change": context.entry.get("filename_change"),
            "wikilink_changes": context.entry.get("wikilink_changes", []),
        }

    def _rollback_page(self, context: RepairPageContext, dependency: KnowledgeDependencies | None) -> None:
        target = context.metadata.get("target")
        if isinstance(target, Path) and target != context.source:
            target.unlink(missing_ok=True)
        if context.wrote:
            atomic_write_bytes(context.source, context.original)
        projection = context.metadata.get("projection")
        if dependency is not None and isinstance(projection, Mapping):
            _restore_projection(self.root, dependency, context.page_path, projection)

    def _refresh_retrieval(
        self,
        target: Path,
        page_path: str,
        *,
        old_page_path: str | None = None,
    ) -> dict[str, str] | None:
        """按 admin profile 刷新 active retrieval，失败只留下审计 warning。"""

        for stage in projection_stages("privacy"):
            if stage != "retrieval":
                continue
            store = RetrievalIndexStore(self.root, scope="active")
            try:
                result = store.update_page_from_file(target)
            except Exception as exc:
                return projection_warning(page_path, exc)
            if result.get("ok") is not True or result.get("state") == "rebuild_required":
                return projection_warning(page_path, result)
            if old_page_path is not None:
                try:
                    deleted = store.delete_page(old_page_path)
                except Exception as exc:
                    return projection_warning(page_path, exc)
                if deleted.get("ok") is False and deleted.get("code") != "index_missing":
                    return projection_warning(page_path, deleted)
        return None

    def _iter_page_files(self) -> Iterable[Path]:
        return iter_admin_page_files(self.root)

    def _page_file(self, value: str, *, allow_missing: bool = False) -> Path:
        return self.plan_owner.page_file(value, allow_missing=allow_missing)


def _changed_fields(before: object, after: object, prefix: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[str] = []
        for key in set(before) | set(after):
            name = f"{prefix}.{key}" if prefix else str(key)
            if field_class(str(key)) in {"locator", "integrity", "internal"}:
                continue
            changes.extend(_changed_fields(before.get(key), after.get(key), name))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes: list[str] = []
        for index, (left, right) in enumerate(zip(before, after, strict=False)):
            changes.extend(_changed_fields(left, right, f"{prefix}[{index}]"))
        return changes
    return [prefix] if before != after and prefix else []


def _filename_change(page_path: str, page_hash: str) -> dict[str, str] | None:
    if redact_sensitive_text(page_path) == page_path:
        return None
    parent, _, name = page_path.rpartition("/")
    suffix = Path(name).suffix
    replacement = f"privacy-redacted-{page_hash[:12]}{suffix}"
    return {"old_page_path": page_path, "new_page_path": f"{parent}/{replacement}" if parent else replacement}


def _wikilink_changes(text: str, filename_map: Mapping[str, str]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for match in _WIKILINK.finditer(text):
        target = match.group(1).replace("\\", "/")
        for old, new in filename_map.items():
            old_no_ext = old[:-3] if old.casefold().endswith(".md") else old
            if target not in {old, old_no_ext}:
                continue
            new_target = new[:-3] if target == old_no_ext and new.casefold().endswith(".md") else new
            changes.append({"old": target, "new": new_target})
            break
    return changes


def _transform_text(text: str, filename_map: Mapping[str, str]) -> str:
    # Replace locator tokens before display redaction so an email-like legacy
    # filename inside ``[[...]]`` is not turned into a broken wikilink.
    locator_safe_text = _replace_wikilinks(text, filename_map)
    frontmatter, body = split_frontmatter(locator_safe_text)
    try:
        projected = redact_storage_value(frontmatter)
        safe_frontmatter = projected if isinstance(projected, dict) else {}
    except LocatorError as exc:
        raise PrivacyAuditError(exc.code) from exc
    if frontmatter or text.lstrip().startswith("---"):
        redacted_body = redact_sensitive_text(body)
        transformed = (
            render_page(safe_frontmatter, redacted_body)
            if safe_frontmatter != frontmatter or redacted_body != body
            else locator_safe_text
        )
    else:
        transformed = redact_sensitive_text(locator_safe_text)
    return transformed


def _replace_wikilinks(text: str, filename_map: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        target = match.group(1).replace("\\", "/")
        for old, new in filename_map.items():
            old_no_ext = old[:-3] if old.casefold().endswith(".md") else old
            if target == old:
                target = new
            elif target == old_no_ext:
                target = new[:-3] if new.casefold().endswith(".md") else new
            else:
                continue
            return f"[[{target}{match.group(2) or ''}{match.group(3) or ''}]]"
        return match.group(0)

    return _WIKILINK.sub(replace, text)


def _has_locator_change(entry: Mapping[str, object]) -> bool:
    return bool(entry.get("filename_change") or entry.get("wikilink_changes"))


def _restore_projection(
    root: Path,
    dependency: KnowledgeDependencies,
    page_path: str,
    projection: Mapping[str, object],
) -> None:
    if projection.get("state") == "ready":
        policy = derive_page_policy(projection)
        dependency.update_page(
            page_path,
            str(projection.get("page_hash", "")),
            string_map(projection.get("edges")),
            policy=policy,
        )
    else:
        dependency.remove_page(page_path)


def _summary(entries: Iterable[Mapping[str, object]]) -> dict[str, int]:
    result = {"pages": 0, "display_hits": 0, "locator_changes": 0}
    for entry in entries:
        result["pages"] += 1
        if entry.get("hit_fields"):
            result["display_hits"] += 1
        if _has_locator_change(entry):
            result["locator_changes"] += 1
    return result


__all__ = ["AtomicFileError", "PrivacyAuditError", "PrivacyAuditService", "atomic_write_text"]
